"""Tests for :mod:`propfarm.sim.fill_engine` (Task 7.2, Wave 6c).

The fill engine is the single chokepoint through which every Phase-1+
strategy submits an order during backtest. It is also the comparison
target for Gate 2B against the user's recorded FTMO demo fills.

These tests lock the contract on seven axes:

1. **Schema identity** with ``scripts/record_fills.FillRecord`` — same
   field set, same field types. Any drift breaks Gate 2B.
2. **Determinism** — same inputs → bit-identical output; seeded rng
   replays bit-identical.
3. **Order-type routing** — market/limit/stop behave per the spec.
4. **Configurable latency** — ``broker_fill_time_utc`` shifts with
   ``execution_latency_ms``.
5. **Sign convention** — slippage is adverse-positive for both sides.
6. **Pip conversion** — per-symbol pip sizes match the recording
   script's formula.
7. **Closed-market handling** — Saturday → MARKET_CLOSED retcode.
8. **Canonical MarketState import** — engine reads from
   ``propfarm.sim.market``; ``stress_mode`` propagates through.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import typing
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Final, get_args, get_origin

import numpy as np
import pytest

from propfarm.sim.fill_engine import (
    DEFAULT_EXECUTION_LATENCY_MS,
    RETCODE_DONE,
    RETCODE_MARKET_CLOSED,
    RETCODE_REJECT,
    FillRequest,
    FillResult,
    simulate_fill,
)
from propfarm.sim.market import MarketState

# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #
#: Mid-session EURUSD timestamp: 2024-06-12 10:00 UTC. London/NY overlap,
#: no NY-close window, market open.
_MID_SESSION_TS: Final[datetime] = datetime(2024, 6, 12, 10, 0, tzinfo=UTC)

#: Saturday 12:00 UTC — FX market closed.
_SATURDAY_TS: Final[datetime] = datetime(2024, 6, 8, 12, 0, tzinfo=UTC)

_REPO_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[2]


def _load_record_fills_module() -> ModuleType:
    """Load ``scripts/record_fills.py`` via importlib (not a package).

    The scripts/ directory is not a Python package, so we cannot
    ``from scripts.record_fills import FillRecord``. We use the
    ``importlib.util.spec_from_file_location`` recipe instead.

    The module is registered in ``sys.modules`` before ``exec_module`` runs
    because record_fills.py contains a ``@dataclass`` whose machinery
    inspects ``sys.modules[cls.__module__]`` during class construction;
    a missing entry there triggers ``AttributeError: 'NoneType' object has
    no attribute '__dict__'`` from ``dataclasses._is_type``.
    """
    import sys

    module_name = "propfarm_test_record_fills"
    path = _REPO_ROOT / "scripts" / "record_fills.py"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _make_market_state(
    *,
    symbol: str = "EURUSD",
    ts_utc: datetime = _MID_SESSION_TS,
    vol: float | None = 0.10,
    news: bool = False,
    stress: bool = False,
) -> MarketState:
    return MarketState(
        symbol=symbol,
        ts_utc=ts_utc,
        realized_vol_5m=vol,
        news_window=news,
        stress_mode=stress,
    )


def _make_request(
    *,
    symbol: str = "EURUSD",
    order_type: typing.Literal["market", "limit", "stop"] = "market",
    side: typing.Literal["buy", "sell"] = "buy",
    volume_lots: float = 0.01,
    requested_price: float = 1.10000,
    ts: datetime = _MID_SESSION_TS,
    run_id: str = "run-test",
    comment: str = "",
) -> FillRequest:
    return FillRequest(
        run_id=run_id,
        symbol=symbol,
        order_type=order_type,
        side=side,
        volume_lots=volume_lots,
        requested_price=requested_price,
        request_time_utc=ts,
        comment=comment,
    )


# --------------------------------------------------------------------------- #
# Schema lock — the user-mandated identity test
# --------------------------------------------------------------------------- #
def test_fill_result_schema_matches_fill_record() -> None:
    """``FillResult`` field-set + types == ``FillRecord`` field-set + types.

    This is the contract that lets Gate 2B compare sim and live row-by-row
    without adapter code. Any drift here breaks the gate.
    """
    rf = _load_record_fills_module()
    record_fields = rf.FillRecord.model_fields
    result_fields = FillResult.model_fields

    assert set(result_fields.keys()) == set(record_fields.keys()), (
        f"FillResult vs FillRecord field-set mismatch: "
        f"only-in-FillResult={set(result_fields) - set(record_fields)}, "
        f"only-in-FillRecord={set(record_fields) - set(result_fields)}"
    )

    for name in record_fields:
        rec_ann = record_fields[name].annotation
        res_ann = result_fields[name].annotation
        # Type aliases (e.g. ``OrderType = Literal[...]``) resolve to the
        # underlying Literal at model_fields time, so direct equality holds.
        # For ``Literal[...]`` we additionally verify the argument set as a
        # belt-and-braces guard against argument-ordering surprises.
        assert rec_ann == res_ann, (
            f"FillResult.{name} annotation {res_ann!r} != FillRecord.{name} annotation {rec_ann!r}"
        )
        if get_origin(rec_ann) is typing.Literal:
            assert set(get_args(rec_ann)) == set(get_args(res_ann)), (
                f"{name}: Literal args differ: {get_args(rec_ann)} vs {get_args(res_ann)}"
            )


def test_fill_result_round_trip_with_synthetic_record() -> None:
    """A synthetic ``FillResult`` dict round-trips into ``FillRecord``.

    The dict produced by ``FillResult.model_dump()`` must validate cleanly
    as ``FillRecord``. When ``data/fills_capture_001.parquet`` lands during
    Gate 2B, this assertion swaps to a real captured row → ``FillRecord``
    → re-cast into the simulator via ``FillResult.model_validate``.
    """
    rf = _load_record_fills_module()
    req = _make_request()
    state = _make_market_state()
    result = simulate_fill(req, state)
    dumped = result.model_dump()
    rebuilt = rf.FillRecord.model_validate(dumped)
    # Field-for-field equality (model_dump returns native types so direct
    # comparison works for the primitive fields and tz-aware datetimes).
    for name in FillResult.model_fields:
        assert getattr(rebuilt, name) == getattr(result, name) or (
            # NaN is its own equality island
            isinstance(getattr(rebuilt, name), float)
            and math.isnan(getattr(rebuilt, name))
            and math.isnan(getattr(result, name))
        )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_simulate_fill_deterministic_no_rng() -> None:
    """100 calls with ``rng=None`` produce byte-identical results."""
    req = _make_request()
    state = _make_market_state()
    first = simulate_fill(req, state)
    for _ in range(99):
        again = simulate_fill(req, state)
        assert again.model_dump() == first.model_dump()


def test_simulate_fill_deterministic_seeded() -> None:
    """Same seed → identical FillResult; different seeds → distinguishable."""
    req = _make_request()
    state = _make_market_state()
    rng_a1 = np.random.default_rng(42)
    rng_a2 = np.random.default_rng(42)
    rng_b = np.random.default_rng(99)
    r_a1 = simulate_fill(req, state, rng=rng_a1)
    r_a2 = simulate_fill(req, state, rng=rng_a2)
    r_b = simulate_fill(req, state, rng=rng_b)
    assert r_a1.model_dump() == r_a2.model_dump()
    # Slippage noise differs across seeds → different fill_price.
    assert r_a1.fill_price != r_b.fill_price


# --------------------------------------------------------------------------- #
# Order-type routing
# --------------------------------------------------------------------------- #
def test_market_order_takes_full_slippage() -> None:
    """0.01-lot EURUSD market buy → fill > requested, slippage > 0."""
    req = _make_request(order_type="market", side="buy", requested_price=1.10000)
    state = _make_market_state()
    res = simulate_fill(req, state)
    assert res.retcode == RETCODE_DONE
    assert res.fill_price > req.requested_price
    assert res.slippage_observed_pips > 0.0


def test_limit_order_inside_spread_fills_clean() -> None:
    """A limit order that's accepted fills at requested_price, 0 slip."""
    # rng=None means deterministic accept (probability < 1.0 always fills).
    req = _make_request(order_type="limit", side="buy", requested_price=1.09995)
    state = _make_market_state()
    res = simulate_fill(req, state)
    assert res.retcode == RETCODE_DONE
    assert res.fill_price == req.requested_price
    assert res.slippage_observed_pips == 0.0


def test_limit_order_outside_spread_can_reject() -> None:
    """With rng+forced reject, retcode=10031, fill_price=0, slip=NaN."""
    # Strategy: stub a calibration with reject_probability=1.0 by passing
    # an rng whose draw is small enough to lose against the baseline 0.02
    # probability. We use ``np.random.default_rng(0)`` whose first
    # ``rng.random()`` after one consumed ``uniform()`` (from slippage) is
    # < 0.02 with reasonable probability. To make it deterministic we
    # build the test using a generator that yields a specific sequence.
    req = _make_request(order_type="limit", side="buy", requested_price=1.09995)
    state = _make_market_state()

    # Find a seed that rejects. We loop over a small range; pytest will
    # cap at a few iterations.
    accepted_or_rejected_seed = None
    for s in range(1000):
        rng = np.random.default_rng(s)
        # Mirror the engine's rng order: slippage uniform() consumed first,
        # then reject draw via rng.random().
        rng.uniform(-0.05, 0.05)  # mirror SlippageResult noise draw
        if rng.random() < 0.02:  # EURUSD baseline reject probability
            accepted_or_rejected_seed = s
            break
    assert accepted_or_rejected_seed is not None, "no rejecting seed in [0, 1000)"

    rng = np.random.default_rng(accepted_or_rejected_seed)
    res = simulate_fill(req, state, rng=rng)
    assert res.retcode == RETCODE_REJECT
    assert res.fill_price == 0.0
    assert math.isnan(res.slippage_observed_pips)


def test_stop_order_treated_as_market() -> None:
    """A stop order, once the caller signals trigger, fills like a market.

    The engine documents that the caller is responsible for deciding stop
    trigger (because :class:`MarketState` carries no OHLC bar). This test
    asserts the routing: a stop request through ``simulate_fill`` produces
    a non-zero slippage and a 10009 retcode, matching the market path.
    """
    req = _make_request(order_type="stop", side="buy", requested_price=1.10050)
    state = _make_market_state()
    res = simulate_fill(req, state)
    assert res.retcode == RETCODE_DONE
    assert res.fill_price > req.requested_price
    assert res.slippage_observed_pips > 0.0


# --------------------------------------------------------------------------- #
# Latency
# --------------------------------------------------------------------------- #
def test_broker_fill_time_offset_by_execution_latency() -> None:
    """``broker_fill_time_utc - request_time_utc`` == default latency."""
    req = _make_request()
    state = _make_market_state()
    res = simulate_fill(req, state)
    delta_ms = (res.broker_fill_time_utc - res.request_time_utc).total_seconds() * 1000.0
    assert delta_ms == pytest.approx(DEFAULT_EXECUTION_LATENCY_MS)
    assert res.broker_latency_ms == pytest.approx(DEFAULT_EXECUTION_LATENCY_MS)


def test_execution_latency_configurable() -> None:
    """``execution_latency_ms=300`` → fill time shifts by 300ms."""
    req = _make_request()
    state = _make_market_state()
    res = simulate_fill(req, state, execution_latency_ms=300.0)
    delta_ms = (res.broker_fill_time_utc - res.request_time_utc).total_seconds() * 1000.0
    assert delta_ms == pytest.approx(300.0)
    assert res.broker_latency_ms == pytest.approx(300.0)
    # And the tz-aware difference equals 300ms exactly.
    assert res.broker_fill_time_utc == res.request_time_utc + timedelta(milliseconds=300.0)


# --------------------------------------------------------------------------- #
# Sign convention — adverse-positive for both sides
# --------------------------------------------------------------------------- #
def test_buy_slippage_adverse_positive() -> None:
    """Buy: fill > requested → slippage_observed_pips > 0."""
    req = _make_request(side="buy", requested_price=1.10000)
    state = _make_market_state()
    res = simulate_fill(req, state)
    assert res.fill_price > req.requested_price
    assert res.slippage_observed_pips > 0.0
    # Numerical check: (fill - requested) / pip == slippage_pips.
    pip = 0.0001
    assert (res.fill_price - res.requested_price) / pip == pytest.approx(res.slippage_observed_pips)


def test_sell_slippage_adverse_positive() -> None:
    """Sell: fill < requested → slippage_observed_pips > 0."""
    req = _make_request(side="sell", requested_price=1.10000)
    state = _make_market_state()
    res = simulate_fill(req, state)
    assert res.fill_price < req.requested_price
    assert res.slippage_observed_pips > 0.0
    pip = 0.0001
    assert (res.requested_price - res.fill_price) / pip == pytest.approx(res.slippage_observed_pips)


# --------------------------------------------------------------------------- #
# Pip conversion — must match scripts/record_fills.parse_fill_into_record
# --------------------------------------------------------------------------- #
#: 2024-06-12 14:00 UTC (a Wednesday) — late enough that the NY cash
#: session for US100 has opened (13:30 UTC during EDT) and well before
#: GER40's 20:00 UTC summer close. FX and XAU are also open throughout.
_ALL_SYMBOLS_OPEN_TS: Final[datetime] = datetime(2024, 6, 12, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "symbol,requested_price,expected_pip",
    [
        ("EURUSD", 1.10000, 0.0001),
        ("GBPUSD", 1.27000, 0.0001),
        ("USDJPY", 150.000, 0.01),
        ("XAUUSD", 3500.00, 0.01),
        ("GER40", 20000.0, 1.0),
        ("US100", 18000.0, 1.0),
    ],
)
def test_pip_size_per_symbol(symbol: str, requested_price: float, expected_pip: float) -> None:
    """Per-symbol pip size matches the recording-script convention.

    The convention is ``pip = 10 ** -(digits - 1)`` where ``digits`` is the
    broker quote precision — same as
    ``scripts/record_fills.parse_fill_into_record``. We verify by computing
    ``(fill - requested) / expected_pip`` against the reported pip count.

    Uses a timestamp where all six covered symbols are simultaneously open
    (14:00 UTC weekday = NY cash session live; GER40 summer hours still
    open; FX/XAU 24/5).
    """
    req = _make_request(
        symbol=symbol,
        side="buy",
        requested_price=requested_price,
        ts=_ALL_SYMBOLS_OPEN_TS,
    )
    state = _make_market_state(symbol=symbol, ts_utc=_ALL_SYMBOLS_OPEN_TS)
    res = simulate_fill(req, state)
    assert res.retcode == RETCODE_DONE, (
        f"market should be open for {symbol} at {_ALL_SYMBOLS_OPEN_TS}, got retcode={res.retcode}"
    )
    derived_pips = (res.fill_price - res.requested_price) / expected_pip
    assert derived_pips == pytest.approx(res.slippage_observed_pips, abs=1e-9)


# --------------------------------------------------------------------------- #
# Closed-market handling
# --------------------------------------------------------------------------- #
def test_market_order_when_market_closed() -> None:
    """Saturday EURUSD market order → retcode=10018, fill=0, slip=NaN."""
    req = _make_request(ts=_SATURDAY_TS, side="buy", requested_price=1.10000)
    state = _make_market_state(ts_utc=_SATURDAY_TS)
    res = simulate_fill(req, state)
    assert res.retcode == RETCODE_MARKET_CLOSED
    assert res.fill_price == 0.0
    assert math.isnan(res.slippage_observed_pips)
    assert math.isnan(res.spread_at_request_pips)
    # Latency still applies — broker_fill_time still shifts forward by
    # the latency offset, since the engine reports what *would* have been
    # the fill instant.
    assert res.broker_fill_time_utc > res.request_time_utc


# --------------------------------------------------------------------------- #
# MarketState input — canonical import
# --------------------------------------------------------------------------- #
def test_imports_canonical_market_state() -> None:
    """The engine reads ``MarketState`` from ``propfarm.sim.market``.

    Builds a fresh canonical MarketState (with ``stress_mode``) and pipes
    it through ``simulate_fill``. No AttributeError should fire on
    ``stress_mode``.
    """
    state = MarketState(
        symbol="EURUSD",
        ts_utc=_MID_SESSION_TS,
        realized_vol_5m=0.10,
        news_window=False,
        stress_mode=True,  # specifically test the field exists on the canonical model
    )
    req = _make_request()
    res = simulate_fill(req, state)
    # Sanity: result is well-formed; no exception was raised.
    assert isinstance(res, FillResult)
    assert res.symbol == "EURUSD"


# --------------------------------------------------------------------------- #
# Stress-mode integration
# --------------------------------------------------------------------------- #
def test_stress_mode_amplifies_slippage() -> None:
    """``market_state.stress_mode=True`` → materially larger slip."""
    req = _make_request(order_type="market", side="buy", volume_lots=0.01)
    baseline_state = _make_market_state(stress=False)
    stress_state = _make_market_state(stress=True)
    baseline = simulate_fill(req, baseline_state)
    stressed = simulate_fill(req, stress_state)
    # EURUSD stress_multiplier is 15.0; expect at least 5x slip even
    # after the noise + size terms.
    assert stressed.slippage_observed_pips > 5.0 * baseline.slippage_observed_pips
    assert stressed.fill_price > baseline.fill_price
