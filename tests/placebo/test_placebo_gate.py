"""Gate 1 (Task 13.1) — placebo acceptance gate tests.

Every test in this module is part of the certification contract. In
particular ``test_placebo_gate_passes_on_choppy_canonical_fixture`` is THE
acceptance test for Phase 0 Gate 1 — if it fails on the canonical fixture,
the simulator is leaking and forward work stops.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from propfarm.placebo.gate import (
    EPSILON_NOISE_FLOOR_MULTIPLIER,
    EXPECTED_FIXTURE_SHA256,
    PlaceboGateResult,
    _aggregate_and_judge,
    run_placebo_gate,
)
from propfarm.placebo.random_strategy import (
    PlaceboTrade,
    generate_random_trades,
    unpack_trade_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def choppy_returns_and_ts() -> tuple[np.ndarray, np.ndarray]:
    """Load the choppy regime's returns + timestamps once per module."""
    df = pl.read_parquet(FIXTURE_PATH)
    sub = df.filter(pl.col("regime") == "choppy").sort("ts")
    return sub["ret"].to_numpy(), sub["ts"].to_numpy()


@pytest.fixture(scope="module")
def canonical_gate_result() -> PlaceboGateResult:
    """Run the canonical gate once; share across the tests that need it.

    n_bootstrap_paths is kept at the production value (10_000) because the
    headline acceptance test must reflect the actual gate parameterization.
    """
    return run_placebo_gate(n_trades=2000, n_bootstrap_paths=10_000)


# --------------------------------------------------------------------------- #
# 1. Fixture pin
# --------------------------------------------------------------------------- #
def test_fixture_sha256_pinned() -> None:
    """The on-disk parquet bytes must match the pinned canonical hash."""
    assert FIXTURE_PATH.exists(), f"canonical fixture missing at {FIXTURE_PATH}"
    h = hashlib.sha256()
    with FIXTURE_PATH.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    assert h.hexdigest() == EXPECTED_FIXTURE_SHA256, (
        "fixture sha256 drift. Gate 1 will not run against a tampered substrate."
    )


# --------------------------------------------------------------------------- #
# 2. Random strategy properties
# --------------------------------------------------------------------------- #
def test_random_strategy_zero_mean_directional(
    choppy_returns_and_ts: tuple[np.ndarray, np.ndarray],
) -> None:
    """~50/50 buy/sell over 2000 trades; mean direction within ±5%."""
    returns, timestamps = choppy_returns_and_ts
    spec = generate_random_trades(
        returns=returns,
        timestamps=timestamps,
        symbol="EURUSD",
        n_trades=2000,
        hold_bars=5,
        rng_seed=20260513,
    )
    unpacked = unpack_trade_spec(spec)
    n_buy = sum(1 for _, side, _, _, _, _ in unpacked if side == "buy")
    n = len(unpacked)
    buy_frac = n_buy / n
    assert 0.45 <= buy_frac <= 0.55, (
        f"buy fraction {buy_frac:.3f} out of [0.45, 0.55]; strategy is not symmetric."
    )


def test_vol_targeted_size_within_band(
    choppy_returns_and_ts: tuple[np.ndarray, np.ndarray],
) -> None:
    """Across the choppy regime, vol-targeted sizes' CV < 0.5."""
    returns, timestamps = choppy_returns_and_ts
    spec = generate_random_trades(
        returns=returns,
        timestamps=timestamps,
        symbol="EURUSD",
        n_trades=2000,
        hold_bars=5,
        rng_seed=20260513,
    )
    unpacked = unpack_trade_spec(spec)
    sizes = np.array([s for _, _, s, _, _, _ in unpacked])
    mean = sizes.mean()
    cv = sizes.std() / mean
    assert cv < 0.5, f"vol-targeted size CV={cv:.3f} > 0.5; sizing is not stable enough."
    assert mean > 0.0


# --------------------------------------------------------------------------- #
# 3. THE acceptance test
# --------------------------------------------------------------------------- #
def test_placebo_gate_passes_on_choppy_canonical_fixture(
    canonical_gate_result: PlaceboGateResult,
) -> None:
    """The canonical Gate 1 verdict on the canonical fixture must be PASS.

    If this fails, the simulator is leaking. Investigate the leak before
    widening ε or changing the strategy. The brief makes this explicit:
    "Investigating a leak is the whole point of this gate."
    """
    result = canonical_gate_result
    assert result.verdict == "pass", (
        f"Gate 1 FAILED on the canonical choppy fixture. "
        f"residual={result.residual_usd:.4f} ε={result.epsilon_usd:.4f} "
        f"reason={result.failure_reason!r}"
    )


# --------------------------------------------------------------------------- #
# 4. Bootstrap / ε contracts
# --------------------------------------------------------------------------- #
def test_empirical_noise_floor_nonzero(
    canonical_gate_result: PlaceboGateResult,
) -> None:
    """SEM must be strictly positive — a zero noise floor would let any
    residual fail the gate trivially and signals a degenerate bootstrap."""
    assert canonical_gate_result.empirical_noise_floor_usd > 0.0


def test_epsilon_is_3_sem(canonical_gate_result: PlaceboGateResult) -> None:
    """epsilon must equal EXACTLY 3 * empirical_noise_floor.

    Reviewer rejects if epsilon_ratio diverges from 3.0. This is the
    "no hand-tuning of tolerance" guard.
    """
    expected_epsilon = (
        EPSILON_NOISE_FLOOR_MULTIPLIER * canonical_gate_result.empirical_noise_floor_usd
    )
    assert math.isclose(
        canonical_gate_result.epsilon_usd,
        expected_epsilon,
        rel_tol=1e-12,
    ), f"ε={canonical_gate_result.epsilon_usd} != 3*SEM={expected_epsilon}; hand-tuning detected."
    assert math.isclose(
        canonical_gate_result.epsilon_ratio,
        EPSILON_NOISE_FLOOR_MULTIPLIER,
        rel_tol=1e-12,
    )


# --------------------------------------------------------------------------- #
# 5. Positive-bias failure attribution
# --------------------------------------------------------------------------- #
def test_positive_expectancy_fails_with_attribution(
    canonical_gate_result: PlaceboGateResult,
) -> None:
    """Inject a positive bias into the trade ledger and re-judge.

    Each trade has its realized_pnl_usd bumped by a large constant relative
    to the noise floor. The verdict must flip to "fail" and the
    failure_reason must call out the alpha-leak / look-ahead suspects.
    """
    # Use the canonical result to confirm the baseline is a pass; injection
    # then guarantees the failure path is exercised on the same parameters.
    assert canonical_gate_result.verdict == "pass"
    # Reconstruct a clean trade list deterministically.
    from propfarm.data.quality import is_market_open
    from propfarm.placebo.gate import (
        DEFAULT_FIXTURE_PATH,
        _load_regime_returns,
        _simulate_trade,
    )
    from propfarm.placebo.random_strategy import to_utc_datetime
    from propfarm.sim.commission import FTMO_MT5_COMMISSION
    from propfarm.sim.swap import FTMO_MT5_SWAP

    returns, timestamps = _load_regime_returns(DEFAULT_FIXTURE_PATH, "choppy")
    spec = generate_random_trades(
        returns=returns,
        timestamps=timestamps,
        symbol="EURUSD",
        n_trades=2000,
        hold_bars=5,
        rng_seed=20260513,
    )
    unpacked = unpack_trade_spec(spec)
    fill_rng = np.random.default_rng(20260513 + 0xF11_E61E)
    trades: list[PlaceboTrade] = []
    for open_idx, side, vol, op, cidx, cp in unpacked:
        open_ts = to_utc_datetime(timestamps[open_idx])
        close_ts = to_utc_datetime(timestamps[cidx])
        if not is_market_open("EURUSD", open_ts) or not is_market_open("EURUSD", close_ts):
            continue
        t = _simulate_trade(
            symbol="EURUSD",
            open_idx=open_idx,
            side=side,
            volume_lots=vol,
            open_price=op,
            close_price=cp,
            open_ts_utc=open_ts,
            close_ts_utc=close_ts,
            realized_vol_5m=0.10,
            commission_table=FTMO_MT5_COMMISSION,
            swap_table=FTMO_MT5_SWAP,
            rng=fill_rng,
        )
        if t is None:
            continue
        trades.append(t)
    # Inject a +$100/trade alpha leak: far above 3 sigma on the residual.
    biased = [
        PlaceboTrade(
            **{
                **t.model_dump(),
                "realized_pnl_usd": t.realized_pnl_usd + 100.0,
            }
        )
        for t in trades
    ]
    biased_result = _aggregate_and_judge(biased, n_bootstrap_paths=10_000, rng_seed=20260513)
    assert biased_result.verdict == "fail"
    assert biased_result.failure_reason is not None
    # Positive residual → alpha-leak rubric must appear.
    assert "alpha_leak" in biased_result.failure_reason or "lookahead" in (
        biased_result.failure_reason
    ), f"unexpected attribution: {biased_result.failure_reason}"


# --------------------------------------------------------------------------- #
# 6. Pipeline composition check
# --------------------------------------------------------------------------- #
def test_full_pipeline_consumes_canonical_modules() -> None:
    """Assert the gate's cost computation routes through the canonical
    sim modules — no backdoor cost code path in the placebo package."""
    import propfarm.placebo.gate as gate_mod

    # The functions we expect to find imported.
    src = Path(gate_mod.__file__).read_text()
    assert "from propfarm.sim.fill_engine import" in src
    assert "simulate_fill" in src
    assert "from propfarm.sim.commission import" in src
    assert "commission_for_trade" in src
    assert "from propfarm.sim.swap import" in src
    assert "swap_for_position" in src
    # MarketState must come from the canonical home, not be redefined.
    assert "from propfarm.sim.market import MarketState" in src


# --------------------------------------------------------------------------- #
# 7. Market-hours respect
# --------------------------------------------------------------------------- #
def test_market_open_respected(
    choppy_returns_and_ts: tuple[np.ndarray, np.ndarray],
) -> None:
    """Trades opened or closed on closed-market timestamps are skipped.

    The canonical fixture is all business days, but ``is_market_open`` also
    excludes the three full-close FX holidays (Jan 1, Dec 25, Dec 26).
    Business-day timestamps that land on those dates are correctly skipped
    by the gate at trade-generation time. We assert two invariants:

    1. The gate's realized ``n_trades`` (post-filter) is lower than the
       generated count by exactly the number of holiday-collision trades.
    2. ``is_market_open`` returns False on Saturday (sanity check on the
       quality predicate this gate trusts).
    """
    from propfarm.data.quality import is_market_open
    from propfarm.placebo.random_strategy import to_utc_datetime

    returns, timestamps = choppy_returns_and_ts
    spec = generate_random_trades(
        returns=returns,
        timestamps=timestamps,
        symbol="EURUSD",
        n_trades=2000,
        hold_bars=5,
        rng_seed=20260513,
    )
    unpacked = unpack_trade_spec(spec)
    # Count holiday collisions on either leg.
    holiday_trade_count = 0
    for open_idx, _, _, _, close_idx, _ in unpacked:
        open_ts = to_utc_datetime(timestamps[open_idx])
        close_ts = to_utc_datetime(timestamps[close_idx])
        if not is_market_open("EURUSD", open_ts) or not is_market_open("EURUSD", close_ts):
            holiday_trade_count += 1
    # There should be a handful (the fixture has 20 years x ~3 holidays/year
    # x small random hit probability). Just check it's small but non-zero
    # so we know the predicate is wired in.
    assert 0 < holiday_trade_count < 50, (
        f"holiday-collision trade count {holiday_trade_count} out of band "
        "(expected a small handful)"
    )

    # The gate post-filter must skip those trades — realized n_trades
    # comes in at a deterministic value below 2000.
    result = run_placebo_gate(n_trades=2000, n_bootstrap_paths=500)
    assert result.n_trades < 2000
    assert result.n_trades > 2000 - 100, (
        f"too many trades skipped: {2000 - result.n_trades}; "
        "the holiday filter should drop only a small fraction."
    )

    # Spot-check: a Saturday timestamp returns False from is_market_open.
    sat = datetime(2024, 1, 6, 12, 0, tzinfo=UTC)  # Saturday
    assert not is_market_open("EURUSD", sat)


# --------------------------------------------------------------------------- #
# 8. Determinism
# --------------------------------------------------------------------------- #
def test_deterministic_seed() -> None:
    """Two runs with identical args produce identical PlaceboGateResult."""
    r1 = run_placebo_gate(n_trades=300, n_bootstrap_paths=500)
    r2 = run_placebo_gate(n_trades=300, n_bootstrap_paths=500)
    assert r1.n_trades == r2.n_trades
    assert math.isclose(r1.mean_pnl_usd, r2.mean_pnl_usd, rel_tol=0.0, abs_tol=0.0)
    assert math.isclose(r1.residual_usd, r2.residual_usd, rel_tol=0.0, abs_tol=0.0)
    assert math.isclose(
        r1.empirical_noise_floor_usd, r2.empirical_noise_floor_usd, rel_tol=0.0, abs_tol=0.0
    )
    assert math.isclose(r1.epsilon_usd, r2.epsilon_usd, rel_tol=0.0, abs_tol=0.0)
    assert r1.verdict == r2.verdict
