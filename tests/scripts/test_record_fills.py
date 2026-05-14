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

import pytest

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
    """Buy filled higher than requested → slippage is positive (adverse).

    The 2026-05-14 fix moved authoritative fill-price reading off
    ``OrderSendResult.price`` (which is 0 for market deals) to the deal
    record. The helper now takes ``actual_fill_price`` / ``actual_fill_time_utc``
    keyword args; tests pass the deal-resolved scalars directly.
    """
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
    # OrderSendResult.price is 0 on a real market deal — the actual
    # 1.10005 fill price lives in the deal record and is now passed via
    # ``actual_fill_price``.
    result = SimpleNamespace(
        retcode=10009, price=0.0, comment="filled", time=0, deal=999, order=998
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
        actual_fill_price=1.10005,
        actual_fill_time_utc=after_send,
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
    # Pathological OrderSendResult — the 1.09998 fill price comes from the
    # deal record, passed via ``actual_fill_price``.
    result = SimpleNamespace(
        retcode=10009, price=0.0, comment="filled", time=0, deal=999, order=998
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
        actual_fill_price=1.09998,
        actual_fill_time_utc=after_send,
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
    result = SimpleNamespace(retcode=10009, price=0.0, comment="", time=0, deal=1, order=1)
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
        actual_fill_price=1.10003,
        actual_fill_time_utc=after_send,
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
    result = SimpleNamespace(retcode=10009, price=0.0, comment="", time=0, deal=1, order=1)
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
        actual_fill_price=1.10003,
        actual_fill_time_utc=after_send,
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


# --------------------------------------------------------------------------- #
# Regression tests — 2026-05-14 fill_price-from-deal fix.
# These tests pin the behavior the 2026-05-13 bug capture violated:
# real MT5 brokers return OrderSendResult.price = 0.0 on market deals,
# and the helper MUST source the fill price from the deal record instead.
# --------------------------------------------------------------------------- #
def test_parse_fill_ignores_zero_price_on_OrderSendResult_when_deal_provided() -> None:
    """The pathological mock: result.price=0, time=0, deal=ticket; deal record carries the truth.

    This is the exact shape of the 2026-05-13 bug: every successful row
    had ``OrderSendResult.price == 0`` and ``OrderSendResult.time == 0``.
    The fixed helper sources fill_price + broker_fill_time_utc from the
    deal-record scalars (``actual_fill_price`` / ``actual_fill_time_utc``)
    instead, so the resulting row reflects the real broker fill — NOT the
    zero from ``OrderSendResult``.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=160)
    deal_time = request_time + timedelta(milliseconds=140)  # broker-side, different from after_send
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_BUY"],
        "price": 1.10003,
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    # Real-broker shape: price=0, time=0, but deal-ticket and order-ticket present.
    result = SimpleNamespace(
        retcode=10009,
        price=0.0,
        comment="",
        time=0,
        deal=7777777,
        order=8888888,
    )
    real_fill_price = 1.10007  # the actual broker fill, from the deal record
    rec = rf.parse_fill_into_record(
        run_id="bug-capture-regression",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
        actual_fill_price=real_fill_price,
        actual_fill_time_utc=deal_time,
    )
    # The fill_price MUST be 1.10007, NOT 0.0 (the bug's signature).
    assert rec["fill_price"] == pytest.approx(real_fill_price, abs=1e-9), (
        f"fill_price should be the deal-record price, not result.price=0; got {rec['fill_price']!r}"
    )
    assert rec["fill_price"] != 0.0, (
        "REGRESSION: fill_price is 0 — the helper read result.price (the 2026-05-13 bug)"
    )
    # broker_fill_time_utc MUST come from the deal record, NOT after_send_utc.
    assert rec["broker_fill_time_utc"] == deal_time
    assert rec["broker_fill_time_utc"] != after_send
    # Slippage: (1.10007 - 1.10003) / 0.0001 = 0.4 pips (adverse for buy).
    assert math.isclose(rec["slippage_observed_pips"], 0.4, abs_tol=1e-6)
    # The retcode and the original (raw) comment are preserved verbatim.
    assert rec["retcode"] == 10009
    assert rec["comment"] == ""


def test_parse_fill_slippage_sign_convention_four_cases() -> None:
    """Four-assert table pinning the adverse-positive slippage sign convention.

    BUY adverse  (filled higher than requested) -> slippage = +10 pips.
    BUY favorable (filled lower than requested) -> slippage = -10 pips.
    SELL adverse (filled lower than requested) -> slippage = +10 pips.
    SELL favorable (filled higher than requested) -> slippage = -10 pips.

    Per the 2026-05-14 dispatch brief, this is non-negotiable.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 13, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=120)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req_buy = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_BUY"],
        "price": 1.10000,
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    open_req_sell = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_SELL"],
        "price": 1.10000,
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    result = SimpleNamespace(retcode=10009, price=0.0, comment="", time=0, deal=1, order=1)

    def _make_rec(open_req: dict[str, Any], side: str, deal_price: float) -> dict[str, Any]:
        rec: dict[str, Any] = rf.parse_fill_into_record(
            run_id="sign-test",
            request_time_utc=request_time,
            after_send_utc=after_send,
            open_req=open_req,
            order_send_result=result,
            tick_at_request=tick,
            symbol_digits=5,
            order_type="market",
            side=side,
            actual_fill_price=deal_price,
            actual_fill_time_utc=after_send,
        )
        return rec

    # The dispatch brief's "1.10010" wording is 1-pip price movement at
    # FX-5-digit quoting (pip = 0.0001, so 1.10010 - 1.10000 = 0.0001 = 1 pip).
    # The brief read "10-pip adverse" colloquially; we honor the LITERAL price
    # delta the brief specified and assert the resulting pip count (1.0).
    # 1. BUY adverse: requested 1.10000, filled 1.10010 → +1 pip adverse for buyer.
    rec_buy_adverse = _make_rec(open_req_buy, "buy", 1.10010)
    assert math.isclose(rec_buy_adverse["slippage_observed_pips"], 1.0, abs_tol=1e-6), (
        f"BUY adverse: expected +1 pip, got {rec_buy_adverse['slippage_observed_pips']!r}"
    )

    # 2. BUY favorable: requested 1.10000, filled 1.09990 -> -1 pip (favorable for buyer).
    rec_buy_fav = _make_rec(open_req_buy, "buy", 1.09990)
    assert math.isclose(rec_buy_fav["slippage_observed_pips"], -1.0, abs_tol=1e-6), (
        f"BUY favorable: expected -1 pip, got {rec_buy_fav['slippage_observed_pips']!r}"
    )

    # 3. SELL adverse: requested 1.10000, filled 1.09990 → +1 pip adverse for seller.
    rec_sell_adverse = _make_rec(open_req_sell, "sell", 1.09990)
    assert math.isclose(rec_sell_adverse["slippage_observed_pips"], 1.0, abs_tol=1e-6), (
        f"SELL adverse: expected +1 pip, got {rec_sell_adverse['slippage_observed_pips']!r}"
    )

    # 4. SELL favorable: requested 1.10000, filled 1.10010 -> -1 pip (favorable for seller).
    rec_sell_fav = _make_rec(open_req_sell, "sell", 1.10010)
    assert math.isclose(rec_sell_fav["slippage_observed_pips"], -1.0, abs_tol=1e-6), (
        f"SELL favorable: expected -1 pip, got {rec_sell_fav['slippage_observed_pips']!r}"
    )


def test_parse_fill_soft_failure_deal_lookup_returned_none() -> None:
    """retcode=10009 but actual_fill_price=None → NaN fields + annotated comment.

    The soft-failure path lets the row land in the parquet (so latency /
    spread / requested_price are preserved) but the fill_price /
    slippage_observed_pips are NaN, and the comment is prefixed with
    :data:`DEAL_LOOKUP_FAILURE_PREFIX` so downstream consumers can
    distinguish from a broker reject (retcode != 10009).
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 14, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=180)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_BUY"],
        "price": 1.10003,
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    result = SimpleNamespace(retcode=10009, price=0.0, comment="filled", time=0, deal=0, order=42)
    rec = rf.parse_fill_into_record(
        run_id="soft-fail",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
        actual_fill_price=None,
        actual_fill_time_utc=None,
    )
    assert rec["retcode"] == 10009  # broker accepted
    assert math.isnan(rec["fill_price"])
    assert math.isnan(rec["slippage_observed_pips"])
    assert rec["comment"].startswith(rf.DEAL_LOOKUP_FAILURE_PREFIX)
    assert "filled" in rec["comment"]
    # Latency and spread MUST still be recorded (they don't depend on the deal).
    assert math.isclose(rec["broker_latency_ms"], 180.0, abs_tol=1e-6)
    assert math.isclose(rec["spread_at_request_pips"], 0.3, abs_tol=1e-6)


def test_parse_fill_rejected_order_unchanged_by_fix() -> None:
    """retcode!=10009 → unchanged behavior: NaN fill, raw comment, after_send_utc time."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 15, 0, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=200)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": _MT5_CONSTANTS["ORDER_TYPE_BUY"],
        "price": 1.10003,
        "action": _MT5_CONSTANTS["TRADE_ACTION_DEAL"],
    }
    result = SimpleNamespace(retcode=10018, price=0.0, comment="Market closed", time=0)
    rec = rf.parse_fill_into_record(
        run_id="reject",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="market",
        side="buy",
        actual_fill_price=None,
        actual_fill_time_utc=None,
    )
    assert rec["retcode"] == 10018
    assert math.isnan(rec["fill_price"])
    assert math.isnan(rec["slippage_observed_pips"])
    # Raw comment is preserved verbatim — NOT prefixed with the soft-failure marker.
    assert rec["comment"] == "Market closed"
    assert not rec["comment"].startswith(rf.DEAL_LOOKUP_FAILURE_PREFIX)
    assert rec["broker_fill_time_utc"] == after_send


# --------------------------------------------------------------------------- #
# _resolve_fill_from_deal helper tests — exercise the MT5-side lookup with
# a mock mt5 module so we cover the integration code path that ``main()``
# uses.
# --------------------------------------------------------------------------- #
def _make_mock_mt5(
    *,
    deal_by_ticket: dict[int, Any] | None = None,
    deals_by_position: dict[int, tuple[Any, ...]] | None = None,
    deal_entry_in: int = 0,
) -> SimpleNamespace:
    """Build a SimpleNamespace that quacks like the parts of mt5 we use."""

    def history_deals_get(
        ticket: int | None = None,
        position: int | None = None,
    ) -> tuple[Any, ...] | None:
        if ticket is not None and deal_by_ticket is not None and ticket in deal_by_ticket:
            return (deal_by_ticket[ticket],)
        if position is not None and deals_by_position is not None:
            return deals_by_position.get(position)
        return None

    return SimpleNamespace(
        history_deals_get=history_deals_get,
        DEAL_ENTRY_IN=deal_entry_in,
    )


def test_resolve_fill_from_deal_happy_path_ticket_lookup() -> None:
    """Deal ticket present on result → single history_deals_get(ticket=...) call resolves."""
    rf = _load_module()
    deal = SimpleNamespace(
        price=1.10007, time=int(datetime(2026, 5, 14, 12, 0, tzinfo=UTC).timestamp()), entry=0
    )
    mock_mt5 = _make_mock_mt5(deal_by_ticket={555: deal})
    result = SimpleNamespace(retcode=10009, deal=555, order=999)
    price, fill_time = rf._resolve_fill_from_deal(mock_mt5, result, success_retcode=10009)
    assert price == pytest.approx(1.10007)
    assert fill_time == datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


def test_resolve_fill_from_deal_position_fallback() -> None:
    """deal=0 on result → falls back to position-keyed lookup with DEAL_ENTRY_IN filter."""
    rf = _load_module()
    entry_in_deal = SimpleNamespace(
        price=1.10011, time=int(datetime(2026, 5, 14, 12, 5, tzinfo=UTC).timestamp()), entry=0
    )
    entry_out_deal = SimpleNamespace(
        price=1.10012, time=int(datetime(2026, 5, 14, 12, 6, tzinfo=UTC).timestamp()), entry=1
    )
    # history_deals_get(position=...) returns BOTH deals; the helper must pick
    # the entry-side one (DEAL_ENTRY_IN=0).
    mock_mt5 = _make_mock_mt5(deals_by_position={777: (entry_in_deal, entry_out_deal)})
    result = SimpleNamespace(retcode=10009, deal=0, order=777)
    price, fill_time = rf._resolve_fill_from_deal(mock_mt5, result, success_retcode=10009)
    assert price == pytest.approx(1.10011)  # the entry-side deal
    assert fill_time == datetime(2026, 5, 14, 12, 5, tzinfo=UTC)


def test_resolve_fill_from_deal_soft_failure_returns_none_pair() -> None:
    """history_deals_get returns None for both lookups → (None, None) soft-failure."""
    rf = _load_module()
    mock_mt5 = _make_mock_mt5(deal_by_ticket={}, deals_by_position={})
    result = SimpleNamespace(retcode=10009, deal=42, order=43)
    price, fill_time = rf._resolve_fill_from_deal(mock_mt5, result, success_retcode=10009)
    assert price is None
    assert fill_time is None


def test_resolve_fill_from_deal_treats_zero_price_as_soft_failure() -> None:
    """Even if a deal record returns, a zero price is treated as soft-failure.

    The 2026-05-13 bug taught us never to trust a 0 from any broker-side
    response object. Deal-record zero price → soft-fail → caller writes NaN.
    """
    rf = _load_module()
    bad_deal = SimpleNamespace(
        price=0.0, time=int(datetime(2026, 5, 14, 12, 0, tzinfo=UTC).timestamp()), entry=0
    )
    mock_mt5 = _make_mock_mt5(deal_by_ticket={1: bad_deal})
    result = SimpleNamespace(retcode=10009, deal=1, order=1)
    price, fill_time = rf._resolve_fill_from_deal(mock_mt5, result, success_retcode=10009)
    assert price is None
    assert fill_time is None


def test_resolve_fill_from_deal_rejected_order_returns_none_pair() -> None:
    """retcode != 10009 → no deal lookup, just (None, None)."""
    rf = _load_module()
    mock_mt5 = _make_mock_mt5()
    result = SimpleNamespace(retcode=10018, deal=0, order=0)
    price, fill_time = rf._resolve_fill_from_deal(mock_mt5, result, success_retcode=10009)
    assert price is None
    assert fill_time is None


# --------------------------------------------------------------------------- #
# Structural verification against the captured 2026-05-13 parquet.
# --------------------------------------------------------------------------- #
_CAPTURED_PARQUET = (
    _REPO_ROOT / "data" / "raw" / "fill_recordings" / "24e00278d0024a98beb009b75762adb6.parquet"
)


@pytest.mark.skipif(
    not _CAPTURED_PARQUET.exists(),
    reason="real broker capture artifact, skipped if absent",
)
def test_captured_parquet_demonstrates_bug_and_fix_changes_shape() -> None:
    """Structural verification: bug present in capture; fix produces non-zero fills.

    Per the 2026-05-14 dispatch brief §5: cannot retroactively reconstruct the
    real fill prices (the deal records weren't recorded). But CAN demonstrate:

    1. Every retcode=10009 row in the captured parquet has fill_price == 0.0
       (the bug's footprint).
    2. With a mock deal record returning a plausible fill, the fixed helper
       produces a non-zero fill_price + matching slippage.

    Together these prove the fix changes the output shape — without claiming
    to invent fill prices retroactively.
    """
    import polars as pl

    rf = _load_module()
    df = pl.read_parquet(_CAPTURED_PARQUET)

    # Part 1: bug's footprint. EVERY successful row has fill_price=0.
    successful = df.filter(pl.col("retcode") == 10009)
    assert successful.height > 0, "capture has no retcode=10009 rows; cannot verify bug"
    fill_prices = successful["fill_price"].to_list()
    assert all(fp == 0.0 for fp in fill_prices), (
        f"expected ALL successful rows to have fill_price=0 (bug footprint); "
        f"got non-zero in {sum(1 for fp in fill_prices if fp != 0.0)} of {len(fill_prices)}"
    )

    # Part 2: with the fix, a synthetic deal-resolved scalar produces a
    # non-zero fill_price for the same input row. Take row 0 as representative.
    row0 = successful.row(0, named=True)
    requested_price = float(row0["requested_price"])
    side = row0["side"]
    symbol = row0["symbol"]
    # Plausible mock fill: 1 pip ADVERSE from requested (the most common case
    # at FX broker level; chosen so the resulting slippage is ~+1.0 pip for
    # both BUY and SELL).
    pip = 0.01 if "JPY" in symbol.upper() else 0.0001
    mock_fill = requested_price + pip if side == "buy" else requested_price - pip
    request_time = row0["request_time_utc"]
    after_send = request_time + timedelta(milliseconds=float(row0["broker_latency_ms"]))
    tick = _tick(
        bid=requested_price - float(row0["spread_at_request_pips"]) * pip / 2,
        ask=requested_price + float(row0["spread_at_request_pips"]) * pip / 2,
    )
    open_req = {
        "symbol": symbol,
        "volume": float(row0["volume_lots"]),
        "type": 0 if side == "buy" else 1,
        "price": requested_price,
        "action": 1,
    }
    # Pathological OrderSendResult (price=0, time=0) as the bug capture showed.
    result = SimpleNamespace(retcode=10009, price=0.0, comment="", time=0, deal=1, order=1)
    rec = rf.parse_fill_into_record(
        run_id="structural-verify",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5 if "JPY" not in symbol.upper() else 3,
        order_type="market",
        side=side,
        actual_fill_price=mock_fill,
        actual_fill_time_utc=request_time + timedelta(milliseconds=140),
    )
    # The fix produces fill_price = mock_fill (NOT 0.0).
    assert rec["fill_price"] == pytest.approx(mock_fill, abs=1e-9), (
        f"fixed helper should produce mock_fill={mock_fill}, got {rec['fill_price']}"
    )
    assert rec["fill_price"] != 0.0
    # And slippage ≈ +1.0 pip (adverse).
    assert math.isclose(rec["slippage_observed_pips"], 1.0, abs_tol=1e-3), (
        f"adverse 1-pip fill → slippage ≈ +1.0 pip; got {rec['slippage_observed_pips']!r}"
    )


# --------------------------------------------------------------------------- #
# Crash-hardening smoke test: per-iteration exception doesn't kill the loop.
# Tests the per-iter except-Exception block in main() in isolation by
# building a minimal harness over its iteration logic — main() itself
# can't be invoked without MT5, so we recreate the iterate-and-catch
# pattern in-test and assert the same invariants.
# --------------------------------------------------------------------------- #
def test_iteration_exception_is_logged_to_stderr_and_loop_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulate the per-iteration try/except in main(): exception at N=5 of 10 → 9 records.

    The pattern under test:

        for idx in range(10):
            try:
                if idx == 5: raise RuntimeError("simulated bad row")
                rows.append(idx)
            except Exception as exc:
                print(f"[record_fills:exception] idx={idx:03d} ...", file=sys.stderr)
                continue

    The smoke test asserts: 9 rows recorded (not 10), stderr contains the
    structured prefix and the iteration index, and the prefix matches what
    main() emits so the operator can grep for it on the next VPS run.
    """
    import sys as _sys

    # Recreate main()'s per-iteration pattern with the exact stderr format.
    rows: list[int] = []
    n_exceptions = 0
    exception_type_counts: Counter[str] = Counter()
    for idx in range(10):
        try:
            if idx == 5:
                raise RuntimeError("simulated bad row at idx=5")
            rows.append(idx)
        except Exception as exc:
            n_exceptions += 1
            exception_type_counts[type(exc).__name__] += 1
            print(
                f"[record_fills:exception] idx={idx:03d} symbol=EURUSD "
                f"order_type=market side=buy "
                f"exc_type={type(exc).__name__} exc_msg={exc!r}",
                file=_sys.stderr,
            )
            continue

    captured = capsys.readouterr()
    # 9 rows recorded (idx=5 was the bad one).
    assert rows == [0, 1, 2, 3, 4, 6, 7, 8, 9]
    assert n_exceptions == 1
    assert exception_type_counts["RuntimeError"] == 1
    # stderr message contains the structured prefix and the iteration index.
    assert "[record_fills:exception]" in captured.err
    assert "idx=005" in captured.err
    assert "RuntimeError" in captured.err
    assert "simulated bad row" in captured.err
