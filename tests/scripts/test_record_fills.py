"""Pure-helper tests for ``scripts/record_fills.py``.

The recording script defers ``import MetaTrader5`` into ``main()`` so the
module loads on macOS/Linux for testing. These tests exercise:

* The deterministic sampling schedule (size, session-open coverage,
  order-type mix, determinism under repeated calls).
* The order-request builder for market / limit-inside / limit-outside / stop.
* The fill-result parser: slippage sign convention, broker latency,
  rejected-fill handling.

We load the script as a module via ``importlib.util`` because ``scripts/`` is
not a package (mirrors the pattern in ``tests/scripts/test_spike_mt5.py``).
No live MT5 connection is made; ``symbol_info_tick`` and
``OrderSendResult`` are stubbed with ``types.SimpleNamespace``.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "record_fills.py"


def _load_module() -> ModuleType:
    """Load scripts/record_fills.py as a fresh module without running main().

    Registers the module in ``sys.modules`` before ``exec_module`` because the
    ``@dataclass`` decorator uses string-annotation resolution which queries
    ``sys.modules[cls.__module__].__dict__`` — without registration the
    decorator raises ``AttributeError: 'NoneType' object has no attribute
    '__dict__'``.
    """
    spec = importlib.util.spec_from_file_location("record_fills", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_fills"] = module
    spec.loader.exec_module(module)
    return module


# Mock MT5 constants. Real MetaTrader5 module assigns these as small ints;
# the exact values don't matter as long as build_order_request round-trips
# them faithfully into the request dict.
_MT5_CONSTANTS: dict[str, int] = {
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_PENDING": 5,
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2,
    "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_TYPE_BUY_STOP": 4,
    "ORDER_TYPE_SELL_STOP": 5,
}


def _template() -> dict[str, Any]:
    """A request template the recording script would build per iteration."""
    return {
        "symbol": "EURUSD",
        "volume": 0.01,
        "deviation": 10,
        "type_filling": 2,  # ORDER_FILLING_IOC stand-in
    }


def _tick(bid: float, ask: float, ts: int = 1715600000) -> SimpleNamespace:
    """A stub for ``mt5.symbol_info_tick`` return."""
    return SimpleNamespace(bid=bid, ask=ask, time=ts, last=0.0, volume=0)


# --------------------------------------------------------------------------- #
# Schedule tests
# --------------------------------------------------------------------------- #
def test_build_default_schedule_yields_n_samples() -> None:
    rf = _load_module()
    start = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    sched = rf.build_default_schedule(start, duration_hours=24.0, n_samples=200)
    assert len(sched) == 200
    assert len(sched.targets) == 200
    assert len(sched.symbols) == 200
    assert len(sched.order_types) == 200
    assert len(sched.sides) == 200


def test_schedule_covers_all_session_opens() -> None:
    """At least one sample within 30 min of each of London / NY / Tokyo opens."""
    rf = _load_module()
    start = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    sched = rf.build_default_schedule(start, duration_hours=24.0, n_samples=200)

    def _within_30min(target: datetime, anchor_minute_of_day: int) -> bool:
        anchor = start.replace(hour=0, minute=0) + timedelta(minutes=anchor_minute_of_day)
        return abs((target - anchor).total_seconds()) <= 30 * 60

    london_open = 7 * 60
    ny_am_open = 12 * 60
    tokyo_open = 23 * 60

    assert any(_within_30min(t, london_open) for t in sched.targets), "no sample near London open"
    assert any(_within_30min(t, ny_am_open) for t in sched.targets), "no sample near NY open"
    assert any(_within_30min(t, tokyo_open) for t in sched.targets), "no sample near Tokyo open"


def test_schedule_order_type_distribution() -> None:
    """Target mix ~60/25/15 with ±10pp tolerance."""
    rf = _load_module()
    start = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    sched = rf.build_default_schedule(start, duration_hours=24.0, n_samples=200)
    counts = Counter(sched.order_types)
    n = len(sched)
    market_frac = counts["market"] / n
    limit_frac = counts["limit"] / n
    stop_frac = counts["stop"] / n
    assert 0.50 <= market_frac <= 0.70, f"market fraction {market_frac} off-target"
    assert 0.15 <= limit_frac <= 0.35, f"limit fraction {limit_frac} off-target"
    assert 0.05 <= stop_frac <= 0.25, f"stop fraction {stop_frac} off-target"


def test_schedule_is_deterministic() -> None:
    """Same seed + same args → byte-identical schedule."""
    rf = _load_module()
    start = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    a = rf.build_default_schedule(start, duration_hours=24.0, n_samples=50, seed=42)
    b = rf.build_default_schedule(start, duration_hours=24.0, n_samples=50, seed=42)
    assert a.targets == b.targets
    assert a.symbols == b.symbols
    assert a.order_types == b.order_types
    assert a.sides == b.sides

    # Different seed → different schedule (sanity — not all four arrays identical).
    c = rf.build_default_schedule(start, duration_hours=24.0, n_samples=50, seed=99)
    assert (a.targets, a.symbols, a.order_types, a.sides) != (
        c.targets,
        c.symbols,
        c.order_types,
        c.sides,
    )


def test_schedule_symbols_cover_at_least_two() -> None:
    """User-mandated coverage: EURUSD plus at least one other major."""
    rf = _load_module()
    start = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    sched = rf.build_default_schedule(start, duration_hours=24.0, n_samples=100)
    seen = set(sched.symbols)
    assert "EURUSD" in seen
    assert len(seen) >= 2, f"only one symbol used: {seen}"


# --------------------------------------------------------------------------- #
# build_order_request tests
# --------------------------------------------------------------------------- #
def test_build_order_request_market_buy() -> None:
    rf = _load_module()
    tick = _tick(bid=1.10000, ask=1.10003)
    req = rf.build_order_request(
        _template(),
        order_type="market",
        symbol_info_tick=tick,
        side="buy",
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["action"] == _MT5_CONSTANTS["TRADE_ACTION_DEAL"]
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_BUY"]
    # Buy market → price = ask.
    assert math.isclose(req["price"], 1.10003, abs_tol=1e-9)
    assert req["sl"] == 0.0
    assert req["tp"] == 0.0
    assert req["volume"] == 0.01
    assert req["symbol"] == "EURUSD"


def test_build_order_request_market_sell() -> None:
    rf = _load_module()
    tick = _tick(bid=1.10000, ask=1.10003)
    req = rf.build_order_request(
        _template(),
        order_type="market",
        symbol_info_tick=tick,
        side="sell",
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_SELL"]
    # Sell market → price = bid.
    assert math.isclose(req["price"], 1.10000, abs_tol=1e-9)


def test_build_order_request_limit_inside_spread() -> None:
    """Buy limit inside spread → 5 pips below mid, action=PENDING, type=BUY_LIMIT."""
    rf = _load_module()
    bid, ask = 1.10000, 1.10003
    mid = (bid + ask) / 2
    tick = _tick(bid=bid, ask=ask)
    req = rf.build_order_request(
        _template(),
        order_type="limit",
        symbol_info_tick=tick,
        side="buy",
        inside_spread=True,
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["action"] == _MT5_CONSTANTS["TRADE_ACTION_PENDING"]
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_BUY_LIMIT"]
    expected = round(mid - 5 * 0.0001 / 2, 5)
    assert math.isclose(req["price"], expected, abs_tol=1e-9), (
        f"expected {expected} got {req['price']}"
    )
    # Inside-spread buy limit price should be below the mid by a half-pip-distance.
    assert req["price"] < mid


def test_build_order_request_limit_outside_spread() -> None:
    """Buy limit outside spread → 5 pips below bid (well below current market)."""
    rf = _load_module()
    bid, ask = 1.10000, 1.10003
    tick = _tick(bid=bid, ask=ask)
    req = rf.build_order_request(
        _template(),
        order_type="limit",
        symbol_info_tick=tick,
        side="buy",
        inside_spread=False,
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["action"] == _MT5_CONSTANTS["TRADE_ACTION_PENDING"]
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_BUY_LIMIT"]
    expected = round(bid - 5 * 0.0001, 5)
    assert math.isclose(req["price"], expected, abs_tol=1e-9), (
        f"expected {expected} got {req['price']}"
    )
    # Outside-spread sits well below the bid.
    assert req["price"] < bid


def test_build_order_request_stop_buy() -> None:
    """Buy stop → price 5 pips ABOVE ask (triggers when market rallies)."""
    rf = _load_module()
    bid, ask = 1.10000, 1.10003
    tick = _tick(bid=bid, ask=ask)
    req = rf.build_order_request(
        _template(),
        order_type="stop",
        symbol_info_tick=tick,
        side="buy",
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["action"] == _MT5_CONSTANTS["TRADE_ACTION_PENDING"]
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_BUY_STOP"]
    expected = round(ask + 5 * 0.0001, 5)
    assert math.isclose(req["price"], expected, abs_tol=1e-9)
    assert req["price"] > ask


def test_build_order_request_stop_sell() -> None:
    """Sell stop → price 5 pips BELOW bid (triggers when market drops)."""
    rf = _load_module()
    bid, ask = 1.10000, 1.10003
    tick = _tick(bid=bid, ask=ask)
    req = rf.build_order_request(
        _template(),
        order_type="stop",
        symbol_info_tick=tick,
        side="sell",
        mt5_constants=_MT5_CONSTANTS,
    )
    assert req["action"] == _MT5_CONSTANTS["TRADE_ACTION_PENDING"]
    assert req["type"] == _MT5_CONSTANTS["ORDER_TYPE_SELL_STOP"]
    expected = round(bid - 5 * 0.0001, 5)
    assert math.isclose(req["price"], expected, abs_tol=1e-9)
    assert req["price"] < bid


def test_build_order_request_rejects_missing_constants() -> None:
    rf = _load_module()
    tick = _tick(bid=1.10000, ask=1.10003)
    incomplete = {k: v for k, v in _MT5_CONSTANTS.items() if k != "TRADE_ACTION_DEAL"}
    try:
        rf.build_order_request(
            _template(),
            order_type="market",
            symbol_info_tick=tick,
            side="buy",
            mt5_constants=incomplete,
        )
    except ValueError as e:
        assert "TRADE_ACTION_DEAL" in str(e)
    else:
        raise AssertionError("expected ValueError for missing constant")


# --------------------------------------------------------------------------- #
# parse_fill_into_record tests
# --------------------------------------------------------------------------- #
def test_parse_fill_into_record_market_buy_adverse_slippage() -> None:
    """Buy filled higher than requested → slippage is positive (adverse)."""
    rf = _load_module()
    request_time = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=150)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_BUY"],
        "price": 1.10003,  # requested ask
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    result = SimpleNamespace(
        retcode=10009, price=1.10005, comment="filled", time=int(after_send.timestamp())
    )
    rec = rf.parse_fill_into_record(
        run_id="abc",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
    )
    # (1.10005 - 1.10003) / 0.0001 = 0.2 pips
    assert math.isclose(rec["slippage_observed_pips"], 0.2, abs_tol=1e-6)
    assert rec["slippage_observed_pips"] > 0  # adverse


def test_parse_fill_into_record_sell_slippage_signed_correctly() -> None:
    """Sell filled below requested → slippage is positive (adverse)."""
    rf = _load_module()
    request_time = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=100)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_SELL"],
        "price": 1.10000,  # requested bid
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    result = SimpleNamespace(
        retcode=10009, price=1.09998, comment="filled", time=int(after_send.timestamp())
    )
    rec = rf.parse_fill_into_record(
        run_id="abc",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="sell",
    )
    # (1.10000 - 1.09998) / 0.0001 = 0.2 pips
    assert math.isclose(rec["slippage_observed_pips"], 0.2, abs_tol=1e-6)
    assert rec["slippage_observed_pips"] > 0  # adverse for the trader


def test_parse_fill_records_broker_latency() -> None:
    rf = _load_module()
    request_time = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=150)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": 0,
        "price": 1.10003,
        "action": 1,
    }
    result = SimpleNamespace(
        retcode=10009, price=1.10003, comment="", time=int(after_send.timestamp())
    )
    rec = rf.parse_fill_into_record(
        run_id="abc",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
    )
    assert math.isclose(rec["broker_latency_ms"], 150.0, abs_tol=1e-6)


def test_parse_fill_handles_retcode_failure() -> None:
    """Non-DONE retcode → fill_price NaN, slippage NaN, retcode preserved."""
    rf = _load_module()
    request_time = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=200)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": 0,
        "price": 1.10003,
        "action": 1,
    }
    # 10018 = TRADE_RETCODE_MARKET_CLOSED (a realistic reject reason).
    result = SimpleNamespace(retcode=10018, price=0.0, comment="Market closed", time=0)
    rec = rf.parse_fill_into_record(
        run_id="abc",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
    )
    assert rec["retcode"] == 10018
    assert math.isnan(rec["fill_price"])
    assert math.isnan(rec["slippage_observed_pips"])
    assert rec["comment"] == "Market closed"
    # Latency is still recorded — useful for diagnosing slow rejects.
    assert math.isclose(rec["broker_latency_ms"], 200.0, abs_tol=1e-6)


def test_parse_fill_records_spread_at_request_pips() -> None:
    """Spread captured from the tick at request time, in pips."""
    rf = _load_module()
    request_time = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=100)
    # 0.3 pip spread.
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {"symbol": "EURUSD", "volume": 0.01, "type": 0, "price": 1.10003, "action": 1}
    result = SimpleNamespace(
        retcode=10009, price=1.10003, comment="", time=int(after_send.timestamp())
    )
    rec = rf.parse_fill_into_record(
        run_id="abc",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
    )
    assert math.isclose(rec["spread_at_request_pips"], 0.3, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# Parquet IO smoke test — uses tmp_path so no real data/ directory is touched.
# --------------------------------------------------------------------------- #
def test_write_recording_round_trip(tmp_path: Path) -> None:
    """write_recording writes parquet + manifest, append-mode concatenates."""
    rf = _load_module()
    import polars as pl  # local import keeps module top-level test-deterministic

    start = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    rows = [
        {
            "run_id": "test123",
            "request_time_utc": start,
            "broker_fill_time_utc": start + timedelta(milliseconds=120),
            "symbol": "EURUSD",
            "order_type": "market",
            "side": "buy",
            "volume_lots": 0.01,
            "requested_price": 1.10003,
            "fill_price": 1.10005,
            "spread_at_request_pips": 0.3,
            "slippage_observed_pips": 0.2,
            "broker_latency_ms": 120.0,
            "retcode": 10009,
            "comment": "filled",
        }
    ]
    pq, mf = rf.write_recording(
        rows,
        run_id="test123",
        start_utc=start,
        end_utc=start + timedelta(minutes=5),
        root=tmp_path,
    )
    assert pq.exists()
    assert mf.exists()
    df = pl.read_parquet(pq)
    assert df.height == 1
    assert df["symbol"][0] == "EURUSD"

    # Append-mode: second call concatenates.
    rows2 = [
        {
            "run_id": "test123",
            "request_time_utc": start + timedelta(minutes=1),
            "broker_fill_time_utc": start + timedelta(minutes=1, milliseconds=110),
            "symbol": "GBPUSD",
            "order_type": "limit",
            "side": "sell",
            "volume_lots": 0.01,
            "requested_price": 1.2500,
            "fill_price": math.nan,
            "spread_at_request_pips": 0.5,
            "slippage_observed_pips": math.nan,
            "broker_latency_ms": 130.0,
            "retcode": 10004,
            "comment": "requote",
        }
    ]
    pq2, mf2 = rf.write_recording(
        rows2,
        run_id="test123",
        start_utc=start,
        end_utc=start + timedelta(minutes=10),
        root=tmp_path,
    )
    df2 = pl.read_parquet(pq2)
    assert df2.height == 2  # appended, not overwritten

    import json as _json

    manifest = _json.loads(mf2.read_text())
    assert manifest["run_id"] == "test123"
    assert manifest["n_attempted"] == 2
    assert manifest["n_filled"] == 1
    assert manifest["n_rejected"] == 1
    assert manifest["schema_version"] == "1.0"
    assert manifest["vps_host_redacted"] is True
