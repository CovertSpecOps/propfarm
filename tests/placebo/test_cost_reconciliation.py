"""Gate 1 cost-reconciliation sister test (Task 13.1b).

This module is the second adversarial-acceptance artefact for Phase 0 Gate 1.
It complements the residual-bootstrap acceptance test in
``tests/placebo/test_placebo_gate.py`` by exercising a category of bug the
bootstrap cannot catch: a systematic cost-arithmetic error in which the
modelled cost equals the applied cost but both diverge from analytic
ground truth.

The reconciliation is **deterministic** — no RNG, no bootstrap, no
resampling. Pass criterion: aggregate applied cost agrees with aggregate
analytic cost to within **0.01 bps relative error** across a 10_112-trade
matrix that exercises every (symbol, direction, volume, nights_held)
dimension of the cost pipeline.

If this test fails, the cost pipeline is producing results that differ
from a closed-form independent recomputation; the test's diagnostics
(per-symbol breakdown on the result object) tell the reviewer which
symbol's arithmetic diverged. The tolerance is not negotiable: a real
divergence is a real bug.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Final

import pytest

from propfarm.placebo.cost_reconciliation import (
    DEFAULT_TOLERANCE_BPS,
    CostReconciliationResult,
    SyntheticTrade,
    applied_cost,
    generate_synthetic_trades,
    run_cost_reconciliation,
)
from propfarm.sim.commission import FTMO_MT5_COMMISSION
from propfarm.sim.fill_engine import RETCODE_DONE, FillRequest, simulate_fill
from propfarm.sim.market import MarketState
from propfarm.sim.swap import FTMO_MT5_SWAP

# --------------------------------------------------------------------------- #
# Module-level constants asserted by the tests
# --------------------------------------------------------------------------- #
#: Minimum trade count required by the Phase-0 Gate-1b brief. The shipped
#: enumeration emits 10_112; this floor catches any accidental shrink.
_MIN_TRADES: Final[int] = 10_000

#: Symbols the synthetic matrix must exercise (requirement #4).
_REQUIRED_SYMBOLS: Final[frozenset[str]] = frozenset({"EURUSD", "GBPUSD", "USDJPY", "XAUUSD"})

#: Nights-held cases the matrix must hit (requirement #4).
_REQUIRED_NIGHTS_VALUES: Final[frozenset[int]] = frozenset({0, 1, 2, 3})

#: Volume tiers the matrix must hit (requirement #4).
_REQUIRED_VOLUMES: Final[frozenset[float]] = frozenset({0.01, 0.10, 0.50, 1.00})


# --------------------------------------------------------------------------- #
# Shared fixture: run the reconciliation once per module
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def reconciliation_result() -> CostReconciliationResult:
    """Run the full reconciliation once and share across tests.

    Determinism: same call → bit-identical result, so re-running per-test
    would be wasteful (each call enumerates 10_112 trades and prices each
    one twice).
    """
    return run_cost_reconciliation()


# --------------------------------------------------------------------------- #
# 1. THE acceptance test — the headline assertion
# --------------------------------------------------------------------------- #
def test_cost_reconciliation_passes(
    reconciliation_result: CostReconciliationResult,
) -> None:
    """Aggregate applied cost matches aggregate analytic cost within 0.01 bps.

    This is THE acceptance test. If it fails, either the pipeline's cost
    arithmetic is wrong or the analytic recomputation in
    ``cost_reconciliation.py`` is wrong. Either way, do NOT widen the
    tolerance: the bug is real, and the per-symbol breakdown on the
    result object surfaces which component diverged.
    """
    result = reconciliation_result
    assert result.verdict == "pass", (
        f"Cost reconciliation FAILED. "
        f"relative_error_bps={result.relative_error_bps} "
        f"tolerance_bps={result.tolerance_bps} "
        f"applied={result.applied_total_usd} known={result.known_total_usd}. "
        f"Per-symbol breakdown: {result.per_symbol_breakdown}"
    )
    assert result.relative_error_bps < DEFAULT_TOLERANCE_BPS, (
        f"relative_error_bps={result.relative_error_bps} "
        f">= tolerance_bps={DEFAULT_TOLERANCE_BPS}. "
        f"Per-symbol breakdown: {result.per_symbol_breakdown}"
    )


# --------------------------------------------------------------------------- #
# 2. Coverage requirements (requirement #4)
# --------------------------------------------------------------------------- #
def test_min_trade_count() -> None:
    """The synthetic matrix must emit at least 10_000 trades."""
    trades = generate_synthetic_trades()
    assert len(trades) >= _MIN_TRADES, (
        f"generate_synthetic_trades() returned {len(trades)} trades, below the {_MIN_TRADES} floor."
    )


def test_symbol_coverage() -> None:
    """At least four symbols from the commission table must appear."""
    trades = generate_synthetic_trades()
    symbols = {t.symbol for t in trades}
    missing = _REQUIRED_SYMBOLS - symbols
    assert not missing, f"missing required symbols: {sorted(missing)}"
    # Every emitted symbol must also be in the commission table — otherwise
    # the applied path would raise.
    for symbol in symbols:
        assert symbol in FTMO_MT5_COMMISSION.per_round_trip_usd, (
            f"symbol {symbol!r} not in FTMO_MT5_COMMISSION.per_round_trip_usd"
        )


def test_direction_coverage() -> None:
    """Both 'long' and 'short' must appear."""
    trades = generate_synthetic_trades()
    directions = {t.direction for t in trades}
    assert directions == {"long", "short"}, f"direction coverage incomplete: {directions}"


def test_volume_coverage() -> None:
    """Multiple distinct volume_lots must appear, including all required tiers."""
    trades = generate_synthetic_trades()
    volumes = {t.volume_lots for t in trades}
    missing = _REQUIRED_VOLUMES - volumes
    assert not missing, f"missing required volumes: {sorted(missing)}"


def test_nights_held_cases_all_hit() -> None:
    """Each of the four nights_held cases (0, 1, 2, 3) must be hit by >= 1 trade.

    This is the explicit requirement-#4 check: 0 nights (intraday), 1
    night (single rollover), 2 nights (Mon-Wed), 3 nights (triple-Wed
    edge case).
    """
    trades = generate_synthetic_trades()
    counts = Counter(t.expected_nights_held for t in trades)
    missing = _REQUIRED_NIGHTS_VALUES - set(counts.keys())
    assert not missing, (
        f"missing required nights_held values: {sorted(missing)}; distribution: {dict(counts)}"
    )
    for n in _REQUIRED_NIGHTS_VALUES:
        assert counts[n] >= 1, (
            f"nights_held={n} hit by 0 trades; required at least 1. distribution: {dict(counts)}"
        )


def test_triple_wednesday_case_actually_triples() -> None:
    """The 3-nights case must span a Wednesday NY-rollover that the swap module triples.

    Independent of the construction-time expectation, we verify that the
    pipeline's nights_held returns exactly 3 for at least one
    triple-Wed trade — i.e. the Wednesday rollover IS being recognised
    as the triple-day. If a future config flipped the
    ``triple_rollover_weekday`` to None or another weekday, this test
    catches it.
    """
    from propfarm.sim.swap import nights_held

    trades = generate_synthetic_trades()
    triple_trades = [t for t in trades if t.expected_nights_held == 3]
    assert triple_trades, "no 3-night trades emitted"
    sample = triple_trades[0]
    pipeline_nights = nights_held(
        open_ts_utc=sample.open_ts_utc,
        close_ts_utc=sample.close_ts_utc,
        triple_rollover_weekday=FTMO_MT5_SWAP.triple_rollover_weekday,
    )
    assert pipeline_nights == 3, (
        f"triple-Wed case: pipeline nights_held={pipeline_nights} (expected 3). "
        f"Either the trade timestamps don't actually span a Wednesday rollover, "
        f"or the swap module's triple-Wed detection has regressed."
    )


# --------------------------------------------------------------------------- #
# 3. Order-type / RNG hygiene (requirement #1, #2)
# --------------------------------------------------------------------------- #
def test_all_trades_are_limit_orders() -> None:
    """Every synthetic trade must use order_type='limit'.

    Limit orders are the zero-slippage path: ``simulate_fill`` returns
    ``slippage_observed_pips=0.0`` and ``fill_price == requested_price``.
    A market order would consume the slippage formula and the noise
    channel; the reconciliation cannot be exact if any trade slips.
    """
    trades = generate_synthetic_trades()
    non_limit = [t for t in trades if t.order_type != "limit"]
    assert not non_limit, (
        f"{len(non_limit)} trades use non-limit order_type; "
        "this breaks the zero-slippage invariant."
    )


def test_no_rng_imports_in_module() -> None:
    """The cost_reconciliation module must NOT import random / numpy.random.

    The brief is explicit: requirement #1 forbids RNG. Importing
    ``random`` or ``numpy.random.default_rng`` in the module would
    indicate either an accidental dependency or a regression toward
    a bootstrap-based path. AST-level inspection (vs. runtime
    monkey-patch) keeps the check robust to lazy imports.
    """
    import ast
    import pathlib

    src_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "propfarm"
        / "placebo"
        / "cost_reconciliation.py"
    )
    source = src_path.read_text()
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "random" or alias.name.startswith("random."):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "random" or module == "numpy.random":
                offenders.append(f"from {module}")
            for alias in node.names:
                # Catch e.g. `from numpy import random` or `from numpy.random import default_rng`
                if alias.name == "random" and module == "numpy":
                    offenders.append("from numpy import random")
    assert not offenders, (
        f"cost_reconciliation.py imports forbidden RNG modules: {offenders}. "
        "Requirement #1: no RNG allowed."
    )


def test_simulate_fill_called_with_rng_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The applied path must call simulate_fill with rng=None.

    Wrap ``simulate_fill`` and assert every call has ``rng`` either
    omitted or explicitly None. This locks the deterministic-pipeline
    invariant — even with limit orders, a non-None rng would consume
    a Bernoulli draw against ``reject_probability`` and could (with
    bad luck) reject a fill, breaking the reconciliation.
    """
    import numpy as np

    from propfarm.placebo import cost_reconciliation as cr
    from propfarm.sim.fill_engine import FillResult

    seen_rngs: list[np.random.Generator | None] = []
    # ``simulate_fill`` is imported into the cost_reconciliation module's
    # namespace but not re-exported via ``__all__``. Direct attribute
    # access is what ruff prefers; the ``type: ignore`` silences the
    # attr-defined check since mypy can't see through the import without
    # an ``__all__`` entry, which would widen the public API just to
    # support a test.
    original = cr.simulate_fill  # type: ignore[attr-defined]

    def spy(
        request: FillRequest,
        market_state: MarketState,
        *,
        execution_latency_ms: float = 150.0,
        rng: np.random.Generator | None = None,
    ) -> FillResult:
        seen_rngs.append(rng)
        return original(
            request,
            market_state,
            execution_latency_ms=execution_latency_ms,
            rng=rng,
        )

    monkeypatch.setattr(cr, "simulate_fill", spy)
    # Price a couple of trades — enough to exercise both legs.
    trades = generate_synthetic_trades()[:4]
    for trade in trades:
        applied_cost(trade)

    assert seen_rngs, "simulate_fill was never called"
    non_none = [r for r in seen_rngs if r is not None]
    assert not non_none, (
        f"simulate_fill called with rng != None in {len(non_none)} of "
        f"{len(seen_rngs)} cases. Requirement #2: deterministic limit-order path."
    )


# --------------------------------------------------------------------------- #
# 4. Determinism (requirement #7)
# --------------------------------------------------------------------------- #
def test_generate_synthetic_trades_is_deterministic() -> None:
    """Two calls must return bit-identical lists."""
    trades_a = generate_synthetic_trades()
    trades_b = generate_synthetic_trades()
    assert len(trades_a) == len(trades_b)
    for a, b in zip(trades_a, trades_b, strict=True):
        assert a == b, f"trade mismatch: {a!r} != {b!r}"


def test_run_cost_reconciliation_is_bit_identical(
    reconciliation_result: CostReconciliationResult,
) -> None:
    """Repeated invocation yields the same totals to the last float bit.

    Determinism contract (requirement #7): bit-identical across runs and
    machines. We compare the raw float bytes (struct-pack) to lock the
    invariant beyond "approximately equal".
    """
    import struct

    second = run_cost_reconciliation()
    assert reconciliation_result.n_trades == second.n_trades
    # Compare via byte pattern: struct.pack('d', x) produces the exact
    # IEEE-754 representation, so byte equality is strict bit equality.
    assert struct.pack("d", reconciliation_result.known_total_usd) == struct.pack(
        "d", second.known_total_usd
    )
    assert struct.pack("d", reconciliation_result.applied_total_usd) == struct.pack(
        "d", second.applied_total_usd
    )
    assert struct.pack("d", reconciliation_result.relative_error_bps) == struct.pack(
        "d", second.relative_error_bps
    )


def test_no_module_level_global_state_mutation() -> None:
    """Calling the public functions must not mutate module-level state.

    This is a smoke check for the pure-function contract of
    ``generate_synthetic_trades``: it should not append to or rebind
    any module-level mutable container.
    """
    from propfarm.placebo import cost_reconciliation as cr

    snapshot_before = set(dir(cr))
    _ = generate_synthetic_trades()
    _ = generate_synthetic_trades()
    snapshot_after = set(dir(cr))
    assert snapshot_before == snapshot_after, (
        f"module symbol-set changed after calls: "
        f"added={snapshot_after - snapshot_before} "
        f"removed={snapshot_before - snapshot_after}"
    )


# --------------------------------------------------------------------------- #
# 5. Schema and frozen-ness invariants
# --------------------------------------------------------------------------- #
def test_synthetic_trade_is_frozen() -> None:
    """SyntheticTrade must be immutable (pydantic frozen=True)."""
    trade = SyntheticTrade(
        symbol="EURUSD",
        direction="long",
        volume_lots=0.1,
        open_ts_utc=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
        close_ts_utc=datetime(2026, 5, 5, 15, 0, tzinfo=UTC),
        order_type="limit",
        expected_nights_held=0,
    )
    # Pydantic v2 raises ValidationError on assignment to frozen models.
    with pytest.raises((TypeError, ValueError)):
        trade.volume_lots = 1.0


def test_cost_reconciliation_result_is_frozen(
    reconciliation_result: CostReconciliationResult,
) -> None:
    """CostReconciliationResult must be immutable."""
    with pytest.raises((TypeError, ValueError)):
        reconciliation_result.verdict = "fail"


def test_all_timestamps_are_utc_tz_aware() -> None:
    """Every emitted trade's timestamps must be tz-aware UTC."""
    from datetime import timedelta

    zero_offset = timedelta(0)
    trades = generate_synthetic_trades()
    for t in trades:
        assert t.open_ts_utc.tzinfo is not None, f"naive open_ts on {t!r}"
        assert t.close_ts_utc.tzinfo is not None, f"naive close_ts on {t!r}"
        assert t.open_ts_utc.utcoffset() == zero_offset, (
            f"non-UTC open_ts on {t!r}: offset={t.open_ts_utc.utcoffset()}"
        )
        assert t.close_ts_utc.utcoffset() == zero_offset, (
            f"non-UTC close_ts on {t!r}: offset={t.close_ts_utc.utcoffset()}"
        )
        assert t.close_ts_utc > t.open_ts_utc, f"close_ts <= open_ts on {t!r}"


# --------------------------------------------------------------------------- #
# 6. Per-component sanity (catches a wrong sign or unit on one leg)
# --------------------------------------------------------------------------- #
def test_per_symbol_breakdown_all_pass(
    reconciliation_result: CostReconciliationResult,
) -> None:
    """Each per-symbol residual must independently be below tolerance.

    If only the *aggregate* relative error is bounded, two large opposing
    per-symbol errors could net out to zero and let a bug through. The
    per-symbol pass ensures no single symbol's arithmetic is broken.
    """
    for symbol, brk in reconciliation_result.per_symbol_breakdown.items():
        assert brk["relative_error_bps"] < DEFAULT_TOLERANCE_BPS, (
            f"symbol {symbol}: relative_error_bps={brk['relative_error_bps']} "
            f">= tolerance={DEFAULT_TOLERANCE_BPS}. breakdown={brk}"
        )


def test_known_total_is_positive(
    reconciliation_result: CostReconciliationResult,
) -> None:
    """Aggregate cost must be > 0.

    Commission alone is > 0 for every trade in the enumeration, and the
    aggregate analytic spread is also strictly positive. A non-positive
    known_total signals a sign-convention flip that nets to ~zero, which
    would also let the relative-error check trivially "pass" while the
    components silently disagree.
    """
    assert reconciliation_result.known_total_usd > 0.0, (
        f"known_total_usd={reconciliation_result.known_total_usd} not > 0; "
        "likely a sign-convention regression."
    )


def test_intraday_zero_nights_trade_has_zero_swap_component() -> None:
    """A 0-night trade contributes zero to the swap leg.

    Picks the first 0-night trade in the enumeration, calls the analytic
    swap helper directly, and confirms the result is exactly 0.0. This
    is the cleanest unit-style probe of "no rollover => no swap charge".
    """
    from propfarm.placebo.cost_reconciliation import _analytic_swap_usd

    trades = generate_synthetic_trades()
    intraday = next(t for t in trades if t.expected_nights_held == 0)
    swap = _analytic_swap_usd(trade=intraday, swap_table=FTMO_MT5_SWAP)
    assert swap == 0.0, f"intraday trade has nonzero analytic swap: {swap}"


def test_limit_fill_returns_zero_slippage_for_every_symbol() -> None:
    """``simulate_fill`` with order_type='limit', rng=None returns slippage=0
    for every symbol the matrix exercises.

    This is the load-bearing invariant for requirement #2: if any
    symbol's limit path produced nonzero slippage, the analytic side
    (which assumes zero slippage) would diverge from the applied side.
    """
    for symbol in _REQUIRED_SYMBOLS:
        ms = MarketState(
            symbol=symbol,
            ts_utc=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            realized_vol_5m=0.10,
        )
        req = FillRequest(
            run_id="cost_reconciliation_check",
            symbol=symbol,
            order_type="limit",
            side="buy",
            volume_lots=0.1,
            requested_price=1.0,
            request_time_utc=ms.ts_utc,
            comment="zero_slip_probe",
        )
        result = simulate_fill(req, ms, rng=None)
        assert result.retcode == RETCODE_DONE, (
            f"{symbol}: limit fill returned retcode {result.retcode}, "
            "not DONE; the probe timestamp is wrong for this symbol."
        )
        assert result.slippage_observed_pips == 0.0, (
            f"{symbol}: limit fill returned nonzero slippage {result.slippage_observed_pips}"
        )
        assert result.fill_price == 1.0, (
            f"{symbol}: limit fill_price {result.fill_price} != requested 1.0"
        )


# --------------------------------------------------------------------------- #
# 7. Performance hygiene
# --------------------------------------------------------------------------- #
def test_reconciliation_runs_in_under_30_seconds() -> None:
    """The full reconciliation must complete in well under 30s (brief req)."""
    import time

    t0 = time.perf_counter()
    _ = run_cost_reconciliation()
    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0, (
        f"run_cost_reconciliation took {elapsed:.2f}s; budget is 30s. "
        f"Check for accidental quadratic loops in the enumeration."
    )


# --------------------------------------------------------------------------- #
# 8. Symbol-table coverage of FTMO_MT5_COMMISSION.per_round_trip_usd
# --------------------------------------------------------------------------- #
def test_at_least_four_commission_symbols_hit() -> None:
    """At least four symbols from FTMO_MT5_COMMISSION.per_round_trip_usd
    must appear in the matrix (explicit brief requirement #4).
    """
    trades = generate_synthetic_trades()
    seen = {t.symbol for t in trades}
    in_commission = seen & set(FTMO_MT5_COMMISSION.per_round_trip_usd.keys())
    assert len(in_commission) >= 4, (
        f"only {len(in_commission)} commission-table symbols exercised: "
        f"{sorted(in_commission)}; brief requires >=4."
    )
