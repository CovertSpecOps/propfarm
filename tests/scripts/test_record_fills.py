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
    # Schema bumped to 1.2 on 2026-05-14 fix v2 reviewer follow-up
    # (market-only denominator for the Gate 2B threshold).
    assert manifest["schema_version"] == "1.2"
    assert manifest["vps_host_redacted"] is True
    # Default `n_market_lookup_failures` when no failures supplied: 0.
    assert manifest["n_market_lookup_failures"] == 0
    # `n_filled_market` counts retcode=success AND order_type=='market' rows.
    # The fixture in this test writes one market sell at retcode=success and
    # one limit at retcode=reject, so the market-fills count is 1.
    assert manifest["n_filled_market"] == 1


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
# uses. The mock models the 2026-05-14 fix-v2 contract: history_deals_get
# returns the pre-staged deals **only after** history_select has been
# called for a covering time range, matching real MT5 broker behavior
# (the precondition the v1 fix mocks failed to model).
# --------------------------------------------------------------------------- #
class _MockMt5:
    """Mock of the MetaTrader5 module's deal-lookup surface.

    Models two real-MT5 contracts:

    1. **History-cache precondition** (fix v2). ``history_deals_get``
       returns the pre-staged deals iff ``history_select(date_from,
       date_to)`` has been called for a range covering the relevant
       deal times. Without ``history_select``, every overload returns
       ``()`` — the failure mode the v1 fix's mocks did not model and
       that caused the 2026-05-14 short-test-1 capture to land with
       NaN fills.

    2. **Server-time semantics** (fix v3 — 2026-05-14). The
       ``date_from`` / ``date_to`` params on ``history_select`` and the
       date-range ``history_deals_get`` overload are interpreted as
       **server-time** Unix seconds, not UTC. Real MT5 + the MQL5
       ``HistorySelect`` reference doc state the date params are in
       server time. ``deal.time`` and ``tick.time`` are also server-time
       Unix seconds. If the helper passes UTC ints (the v2 bug), the
       mock returns ``()`` from the time-range overload — matching the
       real broker behavior the 2026-05-14 short-test-2 capture
       (run_id ``ef34a234bf1649418d3735c3b930ca8c``) exposed.

    Modes
    -----

    * ``history_select_failure_mode="absent"`` — no ``history_select``
      attribute at all. Simulates an older MT5 build where the function
      doesn't exist. Helper proceeds straight to ``history_deals_get``.
    * ``history_select_failure_mode="returns_false"`` — ``history_select``
      attribute exists and returns ``False``. Helper soft-fails and emits
      ``[record_fills:history_select_failed]`` to stderr.
    * ``history_select_failure_mode="returns_true"`` — normal happy path.
      ``history_select`` returns ``True``; ``history_deals_get`` is then
      gated on the prior ``history_select`` call covering the deal time.

    Server-time offset
    ------------------

    ``server_time_offset_seconds`` (default 0) controls the offset
    between server-time and UTC. ``symbol_info_tick(symbol).time``
    returns ``utc_now + server_time_offset_seconds`` so the helper's
    detection routine reads back the configured offset.
    """

    def __init__(
        self,
        *,
        deal_by_ticket: dict[int, Any] | None = None,
        deals_by_position: dict[int, tuple[Any, ...]] | None = None,
        deals_for_time_range: tuple[Any, ...] | None = None,
        history_select_failure_mode: str = "returns_true",
        deal_entry_in: int = 0,
        deal_type_buy: int = 0,
        deal_type_sell: int = 1,
        server_time_offset_seconds: int = 0,
        tick_bid: float = 1.10000,
        tick_ask: float = 1.10003,
    ) -> None:
        self._deal_by_ticket = deal_by_ticket or {}
        self._deals_by_position = deals_by_position or {}
        self._deals_for_time_range = deals_for_time_range or ()
        self._mode = history_select_failure_mode
        # Selected windows are stored as (df_unix_int, dt_unix_int) on a
        # **server-time** axis — that is the contract the date params
        # carry in fix v3.
        self._selected_windows: list[tuple[int, int]] = []
        self.history_select_calls: list[dict[str, Any]] = []
        self.DEAL_ENTRY_IN = deal_entry_in
        self.DEAL_TYPE_BUY = deal_type_buy
        self.DEAL_TYPE_SELL = deal_type_sell
        self.server_time_offset_seconds = int(server_time_offset_seconds)
        self._tick_bid = float(tick_bid)
        self._tick_ask = float(tick_ask)

        if self._mode != "absent":
            # Bind the method only if the mode says it should exist.
            self.history_select = self._history_select

    @staticmethod
    def _coerce_unix_seconds(date_param: Any) -> int | None:
        """Coerce a date param (int Unix or datetime) to int Unix seconds.

        The Python ``history_deals_get`` doc accepts either a
        ``datetime`` or "a number of seconds elapsed since 1970.01.01."
        The mock accepts both shapes so tests that pre-date fix v3 and
        pass datetimes continue to work — but the fix-v3 production
        code path always passes ints (server-time Unix).
        """
        if date_param is None:
            return None
        if isinstance(date_param, datetime):
            return int(date_param.timestamp())
        return int(date_param)

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        """Return a tick whose ``time`` reflects the configured offset.

        2026-05-14 fix v3 — the offset-detection helper compares
        ``tick.time`` against ``time.time()``. Setting
        ``server_time_offset_seconds`` on the mock makes
        ``tick.time = utc_now + offset`` so the detected value matches
        the mock's configured offset.
        """
        utc_now = datetime.now(UTC).timestamp()
        return SimpleNamespace(
            bid=self._tick_bid,
            ask=self._tick_ask,
            time=int(utc_now + self.server_time_offset_seconds),
            last=0.0,
            volume=0,
        )

    def _history_select(
        self,
        date_from: Any | None = None,
        date_to: Any | None = None,
    ) -> bool:
        df_unix = self._coerce_unix_seconds(date_from)
        dt_unix = self._coerce_unix_seconds(date_to)
        self.history_select_calls.append(
            {
                "date_from": date_from,
                "date_to": date_to,
                "date_from_unix": df_unix,
                "date_to_unix": dt_unix,
            }
        )
        if self._mode == "returns_false":
            return False
        if df_unix is not None and dt_unix is not None:
            self._selected_windows.append((df_unix, dt_unix))
        return True

    def _is_covered(self, deal_time_ts: int) -> bool:
        """True iff some prior history_select server-time window covers the deal time.

        Both the window and ``deal_time_ts`` are on the server-time
        axis (fix v3). A helper that passes UTC ints to the mock will
        produce windows that DON'T cover the (server-time) deal_time —
        which is the regression the mutation test exercises.
        """
        for df_unix, dt_unix in self._selected_windows:
            if df_unix <= deal_time_ts <= dt_unix:
                return True
        return False

    def history_deals_get(
        self,
        ticket: int | None = None,
        position: int | None = None,
        date_from: Any | None = None,
        date_to: Any | None = None,
        group: str | None = None,
    ) -> tuple[Any, ...]:
        # Precondition: history_select must have been called with a
        # covering window. In the "absent" mode there's no
        # history_select at all → the mock unconditionally returns the
        # pre-staged deals (some MT5 builds need no precondition).
        precondition_ok = self._mode == "absent" or bool(self._selected_windows)

        if ticket is not None:
            if not precondition_ok:
                return ()
            cand = self._deal_by_ticket.get(int(ticket))
            if cand is None:
                return ()
            cand_time = int(getattr(cand, "time", 0) or 0)
            if self._mode != "absent" and not self._is_covered(cand_time):
                return ()
            return (cand,)
        if position is not None:
            if not precondition_ok:
                return ()
            cands = self._deals_by_position.get(int(position))
            if not cands:
                return ()
            if self._mode == "absent":
                return tuple(cands)
            return tuple(c for c in cands if self._is_covered(int(getattr(c, "time", 0) or 0)))
        if date_from is not None and date_to is not None:
            if not precondition_ok:
                return ()
            # Filter the pre-staged time-range deals to those whose
            # **server-time** ``deal.time`` falls within the requested
            # **server-time** window (fix v3). Both axes are server-time
            # Unix int seconds.
            df_ts = self._coerce_unix_seconds(date_from)
            dt_ts = self._coerce_unix_seconds(date_to)
            if df_ts is None or dt_ts is None:
                return ()
            return tuple(
                d
                for d in self._deals_for_time_range
                if df_ts <= int(getattr(d, "time", 0) or 0) <= dt_ts
            )
        return ()


def _make_mock_mt5(
    *,
    deal_by_ticket: dict[int, Any] | None = None,
    deals_by_position: dict[int, tuple[Any, ...]] | None = None,
    deals_for_time_range: tuple[Any, ...] | None = None,
    history_select_failure_mode: str = "returns_true",
    deal_entry_in: int = 0,
    deal_type_buy: int = 0,
    deal_type_sell: int = 1,
    server_time_offset_seconds: int = 0,
) -> _MockMt5:
    """Thin factory for :class:`_MockMt5` — preserves the call site shape."""
    return _MockMt5(
        deal_by_ticket=deal_by_ticket,
        deals_by_position=deals_by_position,
        deals_for_time_range=deals_for_time_range,
        history_select_failure_mode=history_select_failure_mode,
        deal_entry_in=deal_entry_in,
        deal_type_buy=deal_type_buy,
        deal_type_sell=deal_type_sell,
        server_time_offset_seconds=server_time_offset_seconds,
    )


def _resolve_kwargs(
    *,
    request_time_utc: datetime,
    order_type: str = "market",
    symbol: str = "EURUSD",
    volume_lots: float = 0.01,
    side: str = "buy",
    idx: int | None = None,
    claimed_deal_tickets: set[int] | None = None,
) -> dict[str, Any]:
    """Bundle the v2 required kwargs so test call sites stay readable."""
    return {
        "request_time_utc": request_time_utc,
        "order_type": order_type,
        "symbol": symbol,
        "volume_lots": volume_lots,
        "side": side,
        "idx": idx,
        "claimed_deal_tickets": claimed_deal_tickets,
    }


def test_resolve_fill_from_deal_happy_path_ticket_lookup() -> None:
    """Deal ticket present on result → ticket lookup resolves after history_select."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        ticket=555,
        price=1.10007,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(deal_by_ticket={555: deal})
    result = SimpleNamespace(retcode=10009, deal=555, order=999)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price == pytest.approx(1.10007)
    assert fill_time == request_time
    # The fix v2 precondition: history_select MUST have been called.
    assert mock_mt5.history_select_calls, (
        "_resolve_fill_from_deal did not call history_select before "
        "history_deals_get — this is the v2 regression the bug v2 fixed"
    )


def test_resolve_fill_from_deal_position_fallback() -> None:
    """deal=0 on result → position-keyed lookup, DEAL_ENTRY_IN-filtered."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 12, 5, tzinfo=UTC)
    entry_in_deal = SimpleNamespace(
        ticket=10,
        price=1.10011,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    entry_out_deal = SimpleNamespace(
        ticket=11,
        price=1.10012,
        time=int((request_time + timedelta(minutes=1)).timestamp()),
        entry=1,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(deals_by_position={777: (entry_in_deal, entry_out_deal)})
    result = SimpleNamespace(retcode=10009, deal=0, order=777)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price == pytest.approx(1.10011)  # entry-side deal
    assert fill_time == request_time


def test_resolve_fill_from_deal_soft_failure_returns_none_pair() -> None:
    """history_deals_get returns empty across all three paths → (None, None)."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 14, 0, tzinfo=UTC)
    mock_mt5 = _make_mock_mt5(deal_by_ticket={}, deals_by_position={}, deals_for_time_range=())
    result = SimpleNamespace(retcode=10009, deal=42, order=43)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price is None
    assert fill_time is None


def test_resolve_fill_from_deal_treats_zero_price_as_soft_failure() -> None:
    """Deal exists but reports a 0 price → soft-fail → caller writes NaN."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    bad_deal = SimpleNamespace(
        ticket=1,
        price=0.0,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(deal_by_ticket={1: bad_deal})
    result = SimpleNamespace(retcode=10009, deal=1, order=1)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price is None
    assert fill_time is None


def test_resolve_fill_from_deal_rejected_order_returns_none_pair() -> None:
    """retcode != 10009 → no deal lookup, just (None, None) — no history_select call."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 15, 0, tzinfo=UTC)
    mock_mt5 = _make_mock_mt5()
    result = SimpleNamespace(retcode=10018, deal=0, order=0)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price is None
    assert fill_time is None
    # Short-circuit on the rejected retcode means no history_select call either.
    assert not mock_mt5.history_select_calls


# --------------------------------------------------------------------------- #
# Fix v2 regression: history_select precondition + time-range fallback +
# market-vs-pending lookup-failure distinction. These tests pin the
# 2026-05-14 short-test bug surface: real MT5 brokers require the
# history cache to be populated before ticket / position lookups
# succeed, and the absence of the precondition silently returns ().
# --------------------------------------------------------------------------- #
def test_resolve_fill_from_deal_requires_history_select_precondition() -> None:
    """The 2026-05-14 fix-v2 contract: history_select MUST be called.

    Two halves:

    1. With a mock that returns deals only after history_select(...) is
       called, _resolve_fill_from_deal succeeds and the mock records the
       call.
    2. With a mock that flags history_select as failing (returns False),
       the helper soft-fails to (None, None) AND emits a structured
       stderr log starting with [record_fills:history_select_failed].
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 16, 0, tzinfo=UTC)

    # --- Half 1: happy path. history_select returns True, then ticket lookup wins.
    deal = SimpleNamespace(
        ticket=200,
        price=1.30000,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="GBPUSD",
        volume=0.01,
        type=0,
    )
    happy_mock = _make_mock_mt5(deal_by_ticket={200: deal})
    result = SimpleNamespace(retcode=10009, deal=200, order=201)
    price, fill_time = rf._resolve_fill_from_deal(
        happy_mock,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time, symbol="GBPUSD"),
    )
    assert price == pytest.approx(1.30000)
    assert fill_time == request_time
    # Single history_select call must have been made, with date_from / date_to keys.
    assert len(happy_mock.history_select_calls) == 1
    sel = happy_mock.history_select_calls[0]
    assert sel["date_from"] is not None
    assert sel["date_to"] is not None
    # 2026-05-14 fix v3 — date params are now int Unix seconds (server
    # time). With ``server_time_offset_seconds=0`` (default for this
    # test) server-time and UTC are identical, so the window encloses
    # ``request_time.timestamp()``.
    request_unix = int(request_time.timestamp())
    assert int(sel["date_from"]) < request_unix < int(sel["date_to"])


def test_resolve_fill_from_deal_history_select_returns_false_soft_fails_and_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """history_select returns False → (None, None) + [record_fills:history_select_failed] stderr."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    bad_mock = _make_mock_mt5(
        deal_by_ticket={
            555: SimpleNamespace(
                ticket=555,
                price=1.10000,
                time=int(request_time.timestamp()),
                entry=0,
                symbol="EURUSD",
                volume=0.01,
                type=0,
            )
        },
        history_select_failure_mode="returns_false",
    )
    result = SimpleNamespace(retcode=10009, deal=555, order=556)
    price, fill_time = rf._resolve_fill_from_deal(
        bad_mock,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time, idx=42),
    )
    assert price is None
    assert fill_time is None
    captured = capsys.readouterr()
    assert "[record_fills:history_select_failed]" in captured.err
    assert "idx=42" in captured.err
    assert "symbol=EURUSD" in captured.err


def test_resolve_fill_from_deal_works_on_build_without_history_select() -> None:
    """Older MT5 builds lack history_select; helper still works (hasattr-gated)."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        ticket=900,
        price=1.10005,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    no_select_mock = _make_mock_mt5(
        deal_by_ticket={900: deal},
        history_select_failure_mode="absent",
    )
    # The mock has no history_select attribute in this mode.
    assert not hasattr(no_select_mock, "history_select")
    result = SimpleNamespace(retcode=10009, deal=900, order=901)
    price, fill_time = rf._resolve_fill_from_deal(
        no_select_mock,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price == pytest.approx(1.10005)
    assert fill_time == request_time


def test_resolve_fill_from_deal_time_range_fallback_when_ticket_and_position_empty() -> None:
    """Both ticket=... and position=... lookups return empty → time-range path wins.

    Real-broker shape: OrderSendResult.deal and .order are populated
    but history_deals_get(ticket=...) and history_deals_get(position=...)
    return () because the history cache wasn't engaged for that ticket
    on this build. The time-range fallback queries by date range —
    documented in MetaQuotes as the "single call similar to the
    HistoryDealsTotal and HistoryDealSelect tandem" overload — and
    filters by symbol, volume, side, and entry.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 19, 0, tzinfo=UTC)
    # The deal exists, but only in the time-range bucket — ticket and
    # position lookups return empty even after history_select.
    target_deal = SimpleNamespace(
        ticket=12345,
        price=1.10009,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    # Time-range bucket also contains an exit-leg deal (entry=1, same
    # symbol same volume) that the filter must skip.
    exit_leg = SimpleNamespace(
        ticket=12346,
        price=1.10010,
        time=int((request_time + timedelta(seconds=1)).timestamp()),
        entry=1,
        symbol="EURUSD",
        volume=0.01,
        type=1,  # opposite side: closing
    )
    # And an unrelated EURUSD entry deal that's outside the volume filter.
    unrelated = SimpleNamespace(
        ticket=12347,
        price=1.10011,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.10,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={},  # ticket lookup returns ()
        deals_by_position={},  # position lookup returns ()
        deals_for_time_range=(target_deal, exit_leg, unrelated),
    )
    result = SimpleNamespace(retcode=10009, deal=99, order=100)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(
            request_time_utc=request_time,
            symbol="EURUSD",
            volume_lots=0.01,
            side="buy",
        ),
    )
    assert price == pytest.approx(1.10009)
    assert fill_time == request_time


def test_resolve_fill_from_deal_time_range_fallback_filters_by_side() -> None:
    """Time-range fallback filters by DEAL_TYPE_BUY / DEAL_TYPE_SELL."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 20, 0, tzinfo=UTC)
    buy_deal = SimpleNamespace(
        ticket=1,
        price=1.10000,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,  # DEAL_TYPE_BUY
    )
    sell_deal = SimpleNamespace(
        ticket=2,
        price=1.10005,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=1,  # DEAL_TYPE_SELL
    )
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={},
        deals_by_position={},
        deals_for_time_range=(buy_deal, sell_deal),
    )
    result = SimpleNamespace(retcode=10009, deal=99, order=100)
    # Request a SELL side → only sell_deal should win.
    price, _fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time, side="sell"),
    )
    assert price == pytest.approx(1.10005)


def test_resolve_fill_from_deal_time_range_fallback_claim_tracking() -> None:
    """Claimed deal tickets are skipped → no double-attribution."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 21, 0, tzinfo=UTC)
    deal_one = SimpleNamespace(
        ticket=501,
        price=1.10000,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    deal_two = SimpleNamespace(
        ticket=502,
        price=1.10005,
        time=int((request_time + timedelta(milliseconds=500)).timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={},
        deals_by_position={},
        deals_for_time_range=(deal_one, deal_two),
    )
    result = SimpleNamespace(retcode=10009, deal=99, order=100)
    claimed: set[int] = set()
    # First call: should grab deal_one (closer to request_time) and claim 501.
    p1, _ = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(
            request_time_utc=request_time,
            symbol="EURUSD",
            volume_lots=0.01,
            side="buy",
            claimed_deal_tickets=claimed,
        ),
    )
    assert p1 == pytest.approx(1.10000)
    assert 501 in claimed
    # Second call: same window, but ticket 501 is claimed → deal_two wins.
    p2, _ = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(
            request_time_utc=request_time,
            symbol="EURUSD",
            volume_lots=0.01,
            side="buy",
            claimed_deal_tickets=claimed,
        ),
    )
    assert p2 == pytest.approx(1.10005)
    assert 502 in claimed


def test_resolve_fill_from_deal_time_range_ambiguous_match_logs_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multi-match → log [record_fills:ambiguous_deal_match], pick closest in time."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 22, 0, tzinfo=UTC)
    deal_close = SimpleNamespace(
        ticket=701,
        price=1.10000,
        time=int(request_time.timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    deal_far = SimpleNamespace(
        ticket=702,
        price=1.10010,
        time=int((request_time + timedelta(seconds=3)).timestamp()),
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={},
        deals_by_position={},
        deals_for_time_range=(deal_far, deal_close),
    )
    result = SimpleNamespace(retcode=10009, deal=99, order=100)
    price, _ = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        **_resolve_kwargs(request_time_utc=request_time, idx=7),
    )
    # Closest-time wins.
    assert price == pytest.approx(1.10000)
    captured = capsys.readouterr()
    assert "[record_fills:ambiguous_deal_match]" in captured.err
    assert "idx=7" in captured.err
    assert "n_candidates=2" in captured.err


# --------------------------------------------------------------------------- #
# Market-vs-pending lookup-failure logging contract — exercises the
# emit_market_lookup_failure_log helper directly.
# --------------------------------------------------------------------------- #
def test_emit_market_lookup_failure_log_format(capsys: pytest.CaptureFixture[str]) -> None:
    """The stderr format is exactly `[record_fills:market_lookup_failure] idx=N ...`."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 23, 0, tzinfo=UTC)
    rf.emit_market_lookup_failure_log(
        idx=5,
        symbol="EURUSD",
        order_type="market",
        side="buy",
        request_time_utc=request_time,
        date_from=request_time - timedelta(seconds=1),
        date_to=request_time + timedelta(seconds=5),
    )
    captured = capsys.readouterr()
    assert "[record_fills:market_lookup_failure]" in captured.err
    assert "idx=5" in captured.err
    assert "symbol=EURUSD" in captured.err
    assert "order=market" in captured.err
    assert "side=buy" in captured.err
    assert "request_time=" in captured.err
    assert "window=" in captured.err


# --------------------------------------------------------------------------- #
# Fix v3 regression — server-time offset for history_deals_get date params.
# Encodes the 2026-05-14 short-test-2 (run_id ef34a234bf1649418d3735c3b930ca8c)
# bug class: the MQL5 ``HistorySelect`` doc states the date params are
# interpreted in **server time**, but fix v2 passed UTC datetimes. FTMO
# MT5 runs on EET/EEST (UTC+3 in summer); the offset must be applied at
# the MT5 boundary. The mutation test is the load-bearing check.
# --------------------------------------------------------------------------- #
def test_detect_server_time_offset_rounds_to_nearest_hour() -> None:
    """Sub-hour skew rounds to the closest whole-hour offset.

    Real-world timezone offsets are integer hours; broker write latency
    and clock drift can produce sub-hour skew that should be rounded
    away. The detection helper rounds (tick.time - utc_now) / 3600 to
    the nearest integer hour, then multiplies back to seconds.
    """
    rf = _load_module()
    # 3 hours minus 55 seconds: still rounds to 3h = 10800s.
    detected = rf.detect_server_time_offset_seconds(
        tick_time_server_unix=1715600000 + 10745,
        utc_now_unix=1715600000,
    )
    assert detected == 10800, f"sub-hour skew of -55s should round to nearest hour; got {detected}"

    # Slightly past 3h (3h + 200s): also rounds to 3h.
    detected = rf.detect_server_time_offset_seconds(
        tick_time_server_unix=1715600000 + 10800 + 200,
        utc_now_unix=1715600000,
    )
    assert detected == 10800

    # 3h + 31 min = 12660s → rounds UP to 4h (14400).
    detected = rf.detect_server_time_offset_seconds(
        tick_time_server_unix=1715600000 + 12660,
        utc_now_unix=1715600000,
    )
    assert detected == 14400

    # Negative offset: -5h - 30s.
    detected = rf.detect_server_time_offset_seconds(
        tick_time_server_unix=1715600000 - 18030,
        utc_now_unix=1715600000,
    )
    assert detected == -18000

    # Exactly zero.
    assert (
        rf.detect_server_time_offset_seconds(
            tick_time_server_unix=1715600000,
            utc_now_unix=1715600000,
        )
        == 0
    )


def test_emit_server_time_offset_logs_normal_offset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normal offset (e.g. UTC+3) emits a single startup line.

    Format: ``[record_fills:server_time_offset_seconds=N] server_tz_offset_hours=+H``
    """
    rf = _load_module()
    rf.emit_server_time_offset_logs(10800)
    captured = capsys.readouterr()
    assert "[record_fills:server_time_offset_seconds=10800]" in captured.err
    assert "server_tz_offset_hours=+3" in captured.err
    # No out-of-range warning at a normal offset.
    assert "server_offset_out_of_range" not in captured.err


def test_emit_server_time_offset_logs_at_12h_does_not_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exactly 12h (43200s) is at the threshold but does NOT warn.

    The sanity bound is ``abs(offset) > 12h`` (strict greater-than).
    """
    rf = _load_module()
    rf.emit_server_time_offset_logs(43200)
    captured = capsys.readouterr()
    assert "[record_fills:server_time_offset_seconds=43200]" in captured.err
    assert "server_offset_out_of_range" not in captured.err


def test_emit_server_time_offset_logs_above_12h_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """43201s (just above the threshold) triggers the out-of-range warning.

    Pinned to ``43200 + 1`` (and not at exactly ``43200``) per the fix v3
    spec: the warning fires at ``abs(offset_seconds) > 43200``, strict.
    """
    rf = _load_module()
    rf.emit_server_time_offset_logs(43201)
    captured = capsys.readouterr()
    assert "[record_fills:server_time_offset_seconds=43201]" in captured.err
    assert "[record_fills:server_offset_out_of_range]" in captured.err
    assert "offset_seconds=43201" in captured.err
    assert "VPS clock may be misconfigured" in captured.err
    # Continues, does not abort — caller invariant. The function returns None
    # rather than raising; we already got here, so the invariant holds.


def test_emit_server_time_offset_logs_negative_extreme_also_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Negative offsets > 12h in magnitude also trigger the warning."""
    rf = _load_module()
    rf.emit_server_time_offset_logs(-43201)
    captured = capsys.readouterr()
    assert "[record_fills:server_time_offset_seconds=-43201]" in captured.err
    assert "[record_fills:server_offset_out_of_range]" in captured.err


def test_resolve_fill_from_deal_engages_when_server_time_offset_applied() -> None:
    """Positive control: correct offset → lookup succeeds.

    Simulates the live FTMO summer scenario (UTC+3). The mock's
    ``history_deals_get(date_from=int, date_to=int)`` interprets the
    date params as server-time Unix seconds; the staged deal sits at
    ``int(utc_now + 10800)`` (server-time). Calling the helper with the
    matching ``server_time_offset_seconds=10800`` translates the UTC
    window to the same server-time axis, the lookup engages, and the
    fill price is returned.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 18, 4, 34, tzinfo=UTC)
    # Server-time deal: at UTC+3, ``deal.time`` = utc_unix + 10800.
    deal_server_time_unix = int(request_time.timestamp()) + 10800
    deal = SimpleNamespace(
        ticket=8001,
        price=1.10009,
        time=deal_server_time_unix,
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={8001: deal},
        server_time_offset_seconds=10800,
    )
    result = SimpleNamespace(retcode=10009, deal=8001, order=8002)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        server_time_offset_seconds=10800,
        **_resolve_kwargs(request_time_utc=request_time, symbol="EURUSD"),
    )
    assert price == pytest.approx(1.10009)
    # Returned fill_time MUST be UTC. The deal.time is server-time
    # (utc + 10800); the helper subtracts the offset before constructing
    # the UTC datetime so the parquet's broker_fill_time_utc column is
    # genuinely UTC.
    assert fill_time is not None
    assert fill_time.tzinfo is UTC
    assert fill_time == request_time


def test_resolve_fill_from_deal_returns_empty_when_offset_misapplied_signals_market_lookup_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation regression (load-bearing): offset=0 on a UTC+3 server reproduces the v2 bug.

    Pre-stage a real deal at server-time (utc + 10800); call the helper
    with the BUG condition ``server_time_offset_seconds=0`` (i.e. pass
    UTC ints to ``history_deals_get`` against a server-time-keyed deal
    cache). The mock returns ``()`` because the UTC window misses the
    server-time deal time by 3h. Helper soft-fails to ``(None, None)``
    and ``main()``'s downstream contract — exercised via the
    market-lookup-failure log helper here — emits the
    ``[record_fills:market_lookup_failure]`` line to stderr.

    This pair (positive + negative) locks the regression: if a future
    change drops the offset translation, the positive test fails
    because the helper no longer engages; the negative test still
    passes because it's testing the bug condition exactly.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 18, 4, 34, tzinfo=UTC)
    # The deal sits at server-time UTC+3.
    deal_server_time_unix = int(request_time.timestamp()) + 10800
    deal = SimpleNamespace(
        ticket=8101,
        price=1.10009,
        time=deal_server_time_unix,
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    # Same deal also staged in the time-range bucket so path 3 is exercised.
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={8101: deal},
        deals_for_time_range=(deal,),
        server_time_offset_seconds=10800,
    )
    result = SimpleNamespace(retcode=10009, deal=8101, order=8102)

    # BUG CONDITION: server_time_offset_seconds=0. The helper passes
    # UTC int seconds to the mock's history_select / history_deals_get;
    # the mock interprets them as server-time and finds NO covering
    # window for the deal at utc+10800.
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        server_time_offset_seconds=0,  # ← the v2 bug condition
        **_resolve_kwargs(
            request_time_utc=request_time,
            symbol="EURUSD",
            order_type="market",
            side="buy",
            idx=2,
        ),
    )
    assert price is None, (
        "offset=0 against a server-time-keyed deal cache must miss; "
        "if this asserts price != None, the fix v3 regression has reverted"
    )
    assert fill_time is None

    # Downstream: main()'s market-lookup-failure path would emit the log.
    # We exercise the helper directly so the test doesn't depend on main().
    rf.emit_market_lookup_failure_log(
        idx=2,
        symbol="EURUSD",
        order_type="market",
        side="buy",
        request_time_utc=request_time,
        date_from=request_time - timedelta(seconds=1),
        date_to=request_time + timedelta(seconds=5),
    )
    captured = capsys.readouterr()
    assert "[record_fills:market_lookup_failure]" in captured.err
    assert "idx=2" in captured.err
    assert "symbol=EURUSD" in captured.err


def test_resolve_fill_from_deal_default_offset_zero_preserves_back_compat() -> None:
    """Default ``server_time_offset_seconds=0`` preserves pre-fix-v3 mock contracts.

    All v2 tests pass without specifying ``server_time_offset_seconds``
    on the mock or the helper (both default to 0). This test is the
    explicit pin: with both defaults at zero, deal.time = utc_unix
    matches the helper's UTC-axis window, and the lookup succeeds.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 11, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        ticket=8201,
        price=1.10005,
        time=int(request_time.timestamp()),  # both axes UTC
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    mock_mt5 = _make_mock_mt5(deal_by_ticket={8201: deal})  # offset=0 default
    result = SimpleNamespace(retcode=10009, deal=8201, order=8202)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        # No server_time_offset_seconds kwarg — defaults to 0.
        **_resolve_kwargs(request_time_utc=request_time),
    )
    assert price == pytest.approx(1.10005)
    assert fill_time == request_time


def test_resolve_fill_from_deal_offset_applied_via_time_range_fallback() -> None:
    """Time-range fallback path (path 3) also applies the server-time offset.

    Path 3 is the documented robust path. Verifies the offset reaches
    the ``history_deals_get(date_from=int, date_to=int)`` call site,
    not only the optional ``history_select`` precondition.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 19, 30, tzinfo=UTC)
    deal_server_time_unix = int(request_time.timestamp()) + 10800
    target = SimpleNamespace(
        ticket=9001,
        price=1.10011,
        time=deal_server_time_unix,
        entry=0,
        symbol="EURUSD",
        volume=0.01,
        type=0,
    )
    # Path 1 and 2 return empty (no ticket / position match); only path
    # 3 (time-range fallback) can resolve.
    mock_mt5 = _make_mock_mt5(
        deal_by_ticket={},
        deals_by_position={},
        deals_for_time_range=(target,),
        server_time_offset_seconds=10800,
    )
    result = SimpleNamespace(retcode=10009, deal=42, order=43)
    price, fill_time = rf._resolve_fill_from_deal(
        mock_mt5,
        result,
        success_retcode=10009,
        server_time_offset_seconds=10800,
        **_resolve_kwargs(request_time_utc=request_time, symbol="EURUSD"),
    )
    assert price == pytest.approx(1.10011)
    assert fill_time == request_time


def test_mock_symbol_info_tick_reflects_configured_offset() -> None:
    """``_MockMt5.symbol_info_tick`` returns server-time + offset.

    This is the surface the offset-detection helper reads. Pin the
    contract so a future mock change can't silently break detection.
    """
    rf = _load_module()
    mock_mt5 = _make_mock_mt5(server_time_offset_seconds=10800)
    utc_before = datetime.now(UTC).timestamp()
    tick = mock_mt5.symbol_info_tick("EURUSD")
    utc_after = datetime.now(UTC).timestamp()
    detected = rf.detect_server_time_offset_seconds(int(tick.time), utc_before)
    assert detected == 10800
    detected2 = rf.detect_server_time_offset_seconds(int(tick.time), utc_after)
    assert detected2 == 10800


# --------------------------------------------------------------------------- #
# Manifest schema v1.1 — n_market_lookup_failures field.
# --------------------------------------------------------------------------- #
def test_session_manifest_schema_version_bumped_to_1_2() -> None:
    """SCHEMA_VERSION constant + manifest default must be '1.2'.

    Bumped from 1.1 → 1.2 on the 2026-05-14 fix v2 reviewer follow-up,
    when ``n_filled_market`` was added so Gate 2B's market-lookup-failure
    ratio uses a market-only denominator instead of the all-fills
    denominator (which mixed market + pending and was lenient by ~2x).
    """
    rf = _load_module()
    assert rf.SCHEMA_VERSION == "1.2"
    manifest = rf.SessionManifest(
        run_id="test",
        start_utc=datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 5, 14, 1, 0, tzinfo=UTC),
        n_attempted=0,
        n_filled=0,
        n_rejected=0,
    )
    assert manifest.schema_version == "1.2"
    assert manifest.n_market_lookup_failures == 0
    assert manifest.n_filled_market == 0


def test_write_recording_persists_n_market_lookup_failures(tmp_path: Path) -> None:
    """write_recording threads n_market_lookup_failures into the manifest JSON."""
    rf = _load_module()
    start = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    rows = [
        {
            "run_id": "lookup-fail-test",
            "request_time_utc": start,
            "broker_fill_time_utc": start + timedelta(milliseconds=150),
            "symbol": "EURUSD",
            "order_type": "market",
            "side": "buy",
            "volume_lots": 0.01,
            "requested_price": 1.10000,
            "fill_price": math.nan,
            "spread_at_request_pips": 0.3,
            "slippage_observed_pips": math.nan,
            "broker_latency_ms": 150.0,
            "retcode": 10009,
            "comment": "retcode_or_deal_failure: filled",
        }
    ]
    _, mf = rf.write_recording(
        rows,
        run_id="lookup-fail-test",
        start_utc=start,
        end_utc=start + timedelta(minutes=5),
        root=tmp_path,
        n_market_lookup_failures=3,
    )
    import json as _json

    manifest = _json.loads(mf.read_text())
    assert manifest["n_market_lookup_failures"] == 3
    assert manifest["schema_version"] == "1.2"
    # The single row above is order_type="market" + retcode=10009 → counted.
    assert manifest["n_filled_market"] == 1


# --------------------------------------------------------------------------- #
# Per-order-type behavior on empty lookup. parse_fill_into_record's
# soft-failure path is order-type agnostic (the helper records NaN
# either way), but main() decides separately whether to bump the
# counter + emit the stderr log. These tests pin that contract by
# inspecting parse_fill_into_record + the documented behavior.
# --------------------------------------------------------------------------- #
def test_parse_fill_market_empty_lookup_records_nan_and_annotates_comment() -> None:
    """market + 10009 + empty lookup → fill_price=NaN, comment prefixed with marker."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 13, 0, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=180)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {"symbol": "EURUSD", "volume": 0.01, "type": 0, "price": 1.10003, "action": 1}
    result = SimpleNamespace(retcode=10009, price=0.0, comment="filled", time=0, deal=0, order=42)
    rec = rf.parse_fill_into_record(
        run_id="market-empty",
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
    assert math.isnan(rec["fill_price"])
    assert rec["comment"].startswith(rf.DEAL_LOOKUP_FAILURE_PREFIX)


def test_parse_fill_pending_empty_lookup_records_nan_without_market_log() -> None:
    """limit + 10009 + empty lookup → fill_price=NaN expected (no broker fill yet).

    The parse_fill_into_record helper is order-type agnostic — it
    records NaN either way. The main()-level distinction is the stderr
    log and counter increment, which only fires on order_type='market'.
    This test verifies that the helper-level shape is the same as the
    market path so downstream Gate 2B logic doesn't need to special-case
    limit/stop rows that legitimately have no fill.
    """
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 13, 30, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=20)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {"symbol": "EURUSD", "volume": 0.01, "type": 2, "price": 1.09995, "action": 5}
    result = SimpleNamespace(retcode=10009, price=0.0, comment="placed", time=0, deal=0, order=99)
    rec = rf.parse_fill_into_record(
        run_id="limit-empty",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="limit",
        side="buy",
        actual_fill_price=None,
        actual_fill_time_utc=None,
    )
    assert math.isnan(rec["fill_price"])
    # The parse helper still annotates the comment because it cannot know
    # from order_type alone whether the empty lookup is legit (limit not
    # yet filled) or a bug (market that should have filled). The main()
    # counter / log distinguishes the two.
    assert rec["comment"].startswith(rf.DEAL_LOOKUP_FAILURE_PREFIX)


def test_parse_fill_stop_empty_lookup_records_nan() -> None:
    """stop + 10009 + empty lookup → fill_price=NaN. Parity with limit case."""
    rf = _load_module()
    request_time = datetime(2026, 5, 14, 13, 45, tzinfo=UTC)
    after_send = request_time + timedelta(milliseconds=25)
    tick = _tick(bid=1.10000, ask=1.10003)
    open_req = {"symbol": "EURUSD", "volume": 0.01, "type": 4, "price": 1.10010, "action": 5}
    result = SimpleNamespace(retcode=10009, price=0.0, comment="placed", time=0, deal=0, order=100)
    rec = rf.parse_fill_into_record(
        run_id="stop-empty",
        request_time_utc=request_time,
        after_send_utc=after_send,
        open_req=open_req,
        order_send_result=result,
        tick_at_request=tick,
        symbol_digits=5,
        order_type="stop",
        side="buy",
        actual_fill_price=None,
        actual_fill_time_utc=None,
    )
    assert math.isnan(rec["fill_price"])


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


def test_main_except_clauses_match_documented_crash_hardening_contract() -> None:
    """AST regression: lock the structure of ``main()``'s exception handlers.

    The 2026-05-14 crash-hardening commit added a per-iteration
    ``try / except KeyboardInterrupt / except Exception`` block so the
    capture loop survives transient broker errors. The smoke test
    ``test_iteration_exception_is_logged_to_stderr_and_loop_continues``
    recreates the *pattern* locally, but does NOT bind to ``main()``'s
    actual source — a regression that broadened to ``except BaseException``
    (swallowing ``KeyboardInterrupt`` / ``SystemExit``) or removed the
    dedicated ``except KeyboardInterrupt: raise`` clause would not fail
    that smoke test.

    This AST test parses the script and asserts the contract directly:

    * ``main()`` MUST have at least one ``except`` handler.
    * No bare ``except:`` clauses.
    * No ``except BaseException`` clauses.
    * A dedicated ``except KeyboardInterrupt`` handler exists whose body
      re-raises (so Ctrl-C on the VPS RDP still kills the capture).
    * An ``except Exception`` handler exists (the per-iteration soft-fail).
    """
    import ast

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "record_fills.py"
    tree = ast.parse(script_path.read_text())
    main_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        ),
        None,
    )
    assert main_fn is not None, "scripts/record_fills.py has no main() function"

    handlers = [node for node in ast.walk(main_fn) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "main() has no exception handlers — crash-hardening regressed"

    def handler_type_name(h: ast.ExceptHandler) -> str | None:
        if h.type is None:
            return None  # bare except:
        if isinstance(h.type, ast.Name):
            return h.type.id
        if isinstance(h.type, ast.Attribute):
            return h.type.attr
        return None

    type_names = [handler_type_name(h) for h in handlers]

    assert None not in type_names, (
        "main() has a bare `except:` clause — swallows BaseException incl. KeyboardInterrupt"
    )
    assert "BaseException" not in type_names, (
        "main() catches BaseException; this swallows KeyboardInterrupt and SystemExit. "
        "Use `except Exception` with a separate `except KeyboardInterrupt: raise` block."
    )

    kbi_handlers = [h for h in handlers if handler_type_name(h) == "KeyboardInterrupt"]
    assert kbi_handlers, (
        "main() has no dedicated `except KeyboardInterrupt` handler — "
        "Ctrl-C on the VPS RDP would fall through to `except Exception` and be swallowed."
    )
    for h in kbi_handlers:
        assert any(isinstance(stmt, ast.Raise) for stmt in h.body), (
            "KeyboardInterrupt handler in main() does not re-raise"
        )

    assert "Exception" in type_names, (
        "main() has no `except Exception` handler — per-iteration crash-hardening regressed"
    )
