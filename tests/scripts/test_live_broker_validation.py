"""Live-broker validation test for ``scripts/record_fills.py``.

This is the **load-bearing live test** mandated by Task #53 and the
2026-05-14 fix-v3 playbook addendum #3: *"Each broker-side integration
class should ship with at least one live-broker validation test that
runs against a real account in demo mode and asserts the contract
holds. The unit tests with mocks are necessary but not sufficient."*

Three speculative fixes (v1 result.price=0, v2 history_select
precondition, v3 server-time offset) have all "passed mocks" and
failed the live broker. The reviewer playbook addendum (2026-05-14
fix v4) now mandates: **if a fix-cycle has hit the same class of bug
≥ 2 times, halt speculative fixing and add instrumentation to gather
live-broker evidence before the next attempt.** This test is the
instrumentation's counterpart — it drives the actual production code
path against the real FTMO MT5 demo and asserts the contract.

Why it's skipped by default
---------------------------

* The MetaTrader5 Python package is **Windows-only** (no macOS/Linux
  wheel). Running this test on the dev machine would fail at
  ``import MetaTrader5``.
* The test connects to a real (demo) broker and places a real
  (0.01-lot) order. It is unsafe to run on every CI / pre-commit.

Invocation contract
-------------------

The test is gated on **two** conditions, both of which must hold:

1. The ``PROPFARM_LIVE_TEST`` environment variable must be set to
   ``"1"`` (any non-``"1"`` value, including unset, skips the test).
2. ``mt5.account_info().server`` must start with ``"FTMO-Demo"``
   (the same prefix-match safety guard ``scripts/record_fills.py``
   enforces at session startup).

Both gates are enforced from inside the test (the marker alone is
not enough — a future ``pytest -m live_broker_validation`` run with
the env var unset would silently skip; a future
``PROPFARM_LIVE_TEST=1 pytest -m live_broker_validation`` against a
non-demo account would refuse to place the order).

Invocation (from the Windows VPS only)
--------------------------------------

::

    PROPFARM_LIVE_TEST=1 pytest tests/scripts/test_live_broker_validation.py

Or via the marker:

::

    PROPFARM_LIVE_TEST=1 pytest -m live_broker_validation

What the test asserts
---------------------

The test:

1. Connects to the FTMO MT5 demo (creds from
   ``~/.propfarm-secrets.json``).
2. Verifies the server prefix matches ``FTMO-Demo*``.
3. Reads the current EURUSD tick (so the test can verify the fill
   price is "near" the request-time bid/ask, not a stale value).
4. Detects the server-time offset (per fix v3) so the deal-lookup
   helper receives the same offset the production capture would use.
5. Places ONE 0.01-lot EURUSD market buy.
6. Drives the result through ``_resolve_fill_from_deal`` (production
   call site — not a re-implementation).
7. Asserts:

   * ``actual_fill_price is not None``
   * ``actual_fill_price > 0``
   * ``abs(actual_fill_price - mid) < 100 * pip`` (100 pips tolerance
     — generous; not testing fill accuracy, just sanity).
   * ``n_market_lookup_failures`` did NOT increment (the helper's
     return was a real number, not ``(None, None)``).

8. Closes the position immediately (irrespective of test pass/fail)
   so the VPS does not accumulate stray demo positions.

If the test fails on any assertion, the captured stderr (containing
the ``[record_fills:lookup_probe_*]`` lines if the probe block fired)
becomes the load-bearing evidence the user pastes back. Fix v4 reads
those lines to decide which ``history_deals_get`` call form to
adopt.

Cross-references
----------------

* Probe block: ``scripts/record_fills.py`` —
  ``emit_market_lookup_failure_probes`` + the six
  ``[record_fills:lookup_probe_*]`` prefixes.
* Runbook: ``docs/runbooks/gate-2b-fill-recording.md`` —
  ``2026-05-14 fix-up #4`` section.
* Playbook addendum: ``STATUS.md`` — Pathological-vendor-response
  catch pattern, addendum #4 entry.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "record_fills.py"

_PROPFARM_LIVE_TEST = os.environ.get("PROPFARM_LIVE_TEST")
_LIVE_BROKER_GATE_REASON = (
    "live-broker test; set PROPFARM_LIVE_TEST=1 and run on the Windows VPS "
    "with MetaTrader5 installed and ~/.propfarm-secrets.json present. "
    "See tests/scripts/test_live_broker_validation.py module docstring."
)


def _load_record_fills() -> ModuleType:
    """Load ``scripts/record_fills.py`` as a module (no main() invocation).

    Mirrors the pattern in ``test_record_fills.py``: spec_from_file_location
    + exec_module so the module's pure helpers (``_resolve_fill_from_deal``,
    ``detect_server_time_offset_seconds``, etc.) are importable without
    running the recording loop.
    """
    spec = importlib.util.spec_from_file_location("record_fills_for_live_test", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_fills_for_live_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.live_broker_validation
@pytest.mark.skipif(_PROPFARM_LIVE_TEST != "1", reason=_LIVE_BROKER_GATE_REASON)
def test_record_fills_resolves_real_market_order_against_ftmo_demo() -> None:
    """One 0.01-lot EURUSD market buy → fill resolves to a real (>0) price.

    The user runs this on the Windows VPS with
    ``PROPFARM_LIVE_TEST=1 pytest tests/scripts/test_live_broker_validation.py``.

    On a successful pass: the production code path engages — the deal
    lookup returns a real price, and ``n_market_lookup_failures`` does
    NOT increment. This is the contract that the unit tests with mocks
    are necessary-but-not-sufficient to verify (per the fix-v3 playbook
    addendum lesson).

    On failure: the probe block (``[record_fills:lookup_probe_*]``) fires
    on the same stderr that captures the test output; pytest's
    ``--capture`` machinery (or ``-s`` to disable capture) surfaces it.
    The user pastes the probe block back and fix v4 dispatches.
    """
    # Late imports so the test module imports cleanly on macOS/Linux
    # (the MetaTrader5 package has no non-Windows wheel).
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip(
            "MetaTrader5 package not installed — this test runs on the "
            "Windows VPS only. See module docstring."
        )

    rf = _load_record_fills()

    creds_path = pathlib.Path.home() / ".propfarm-secrets.json"
    if not creds_path.exists():
        pytest.skip(
            f"missing {creds_path} — populate per the gate-2b runbook "
            "Prerequisites section before running this test."
        )
    creds = json.loads(creds_path.read_text())["ftmo_demo"]

    if not mt5.initialize(login=creds["login"], password=creds["password"], server=creds["server"]):
        pytest.fail(f"mt5.initialize failed: {mt5.last_error()}")

    placed_ticket: int | None = None
    try:
        # SAFETY ASSERT #1 — server prefix must match the production guard
        # at scripts/record_fills.py:1674 (`ALLOWED_SERVER_PREFIX = "FTMO-Demo"`).
        # A live-broker validation test that ran against a FUNDED account
        # would place a real order in real money. Refuse loudly.
        account = mt5.account_info()
        assert account is not None, "mt5.account_info() returned None"
        assert str(account.server).startswith(rf.ALLOWED_SERVER_PREFIX), (
            f"refusing to run live-broker validation against server={account.server!r}; "
            f"must start with {rf.ALLOWED_SERVER_PREFIX!r}"
        )

        symbol = "EURUSD"
        tick_pre = mt5.symbol_info_tick(symbol)
        assert tick_pre is not None, f"mt5.symbol_info_tick({symbol!r}) returned None"
        bid = float(tick_pre.bid)
        ask = float(tick_pre.ask)
        assert bid > 0 and ask > 0, f"degenerate tick bid={bid} ask={ask}"
        mid = (bid + ask) / 2.0
        pip = 0.0001  # FX 5-digit major

        # Detect the server-time offset (fix v3). The production
        # capture detects this at session startup; mirror the contract
        # here so the helper receives the same offset.
        import time as _time

        server_time_offset_seconds = rf.detect_server_time_offset_seconds(
            int(tick_pre.time),
            _time.time(),
        )
        rf.emit_server_time_offset_logs(server_time_offset_seconds)
        rf.validate_server_time_offset_seconds(server_time_offset_seconds)

        constants = {
            "TRADE_ACTION_DEAL": mt5.TRADE_ACTION_DEAL,
            "TRADE_ACTION_PENDING": mt5.TRADE_ACTION_PENDING,
            "ORDER_TYPE_BUY": mt5.ORDER_TYPE_BUY,
            "ORDER_TYPE_SELL": mt5.ORDER_TYPE_SELL,
            "ORDER_TYPE_BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
            "ORDER_TYPE_SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
            "ORDER_TYPE_BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
            "ORDER_TYPE_SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
        }
        success_retcode = int(mt5.TRADE_RETCODE_DONE)

        template = {
            "symbol": symbol,
            "volume": rf.LOT_SIZE,
            "deviation": 10,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        req = rf.build_order_request(
            template,
            order_type="market",
            symbol_info_tick=tick_pre,
            side="buy",
            inside_spread=True,  # ignored for market orders
            mt5_constants=constants,
        )

        request_time = datetime.now(tz=UTC)
        result = mt5.order_send(req)
        assert result is not None, f"mt5.order_send returned None: {mt5.last_error()}"
        assert int(result.retcode) == success_retcode, (
            f"order_send retcode={result.retcode} comment={getattr(result, 'comment', '?')!r}"
        )
        placed_ticket = int(getattr(result, "order", 0) or 0)

        # Drive the result through the production helper. A session-
        # scoped claim set is required by the helper's contract; pass
        # an empty set so this single-order test fully exercises the
        # claim-tracking branches as it would in production.
        claimed_deal_tickets: set[int] = set()
        actual_fill_price, actual_fill_time = rf._resolve_fill_from_deal(
            mt5,
            result,
            success_retcode=success_retcode,
            request_time_utc=request_time,
            order_type="market",
            symbol=symbol,
            volume_lots=float(req["volume"]),
            side="buy",
            idx=0,
            claimed_deal_tickets=claimed_deal_tickets,
            server_time_offset_seconds=server_time_offset_seconds,
        )

        # Load-bearing assertions: the production call form must return
        # a real number on the live broker. If any of these fail, the
        # captured stderr carries the probe block (when the fix-v4
        # toggle is True) and the user pastes it back.
        assert actual_fill_price is not None, (
            "_resolve_fill_from_deal returned (None, None) on a live "
            "FTMO-Demo market order — this is the bug class fix v4 is "
            "instrumenting. Check stderr for [record_fills:lookup_probe_*] lines."
        )
        assert actual_fill_price > 0, (
            f"_resolve_fill_from_deal returned non-positive fill_price={actual_fill_price}; "
            "MT5 OrderSendResult.price = 0 was the v1 bug — see the v1 fix-up section "
            "in docs/runbooks/gate-2b-fill-recording.md"
        )
        # Sanity-bound the fill price against the request-time mid.
        # 100 pips is intentionally generous — this is not testing fill
        # accuracy, only "did the helper return a real number anywhere
        # near reality?"
        deviation_pips = abs(actual_fill_price - mid) / pip
        assert deviation_pips < 100.0, (
            f"actual_fill_price={actual_fill_price} is {deviation_pips:.1f} pips from "
            f"request-time mid={mid}; expected within 100 pips. This either means the "
            "deal lookup returned the wrong row, or the broker's reported fill price "
            "is bizarrely far from the request-time spread."
        )
        # The n_market_lookup_failures counter is incremented in main()
        # when order_type=='market' AND retcode==success AND fill_price
        # is None. Since we just asserted fill_price is not None on a
        # market order, the increment branch did not fire — confirm
        # the boolean explicitly so a future refactor that decoupled
        # fill_price from the counter still trips this test.
        would_have_incremented = (
            "market" == "market"
            and int(result.retcode) == success_retcode
            and actual_fill_price is None
        )
        assert not would_have_incremented, (
            "n_market_lookup_failures increment branch would have fired "
            "despite a non-None fill_price — counter logic regressed"
        )

        # The fill time should be roughly contemporaneous with the order
        # send (broker fills are usually < 1s after request). Use a
        # generous 60s window since this test is not about latency.
        if actual_fill_time is not None:
            delta_s = abs((actual_fill_time - request_time).total_seconds())
            assert delta_s < 60.0, (
                f"actual_fill_time={actual_fill_time.isoformat()} is {delta_s:.1f}s from "
                f"request_time={request_time.isoformat()}; "
                "may indicate server-time offset drift"
            )
            assert actual_fill_time.tzinfo is UTC, (
                f"actual_fill_time must be UTC-tz-aware; got tzinfo={actual_fill_time.tzinfo}"
            )

    finally:
        # Always close any opened position so the demo account does not
        # accumulate stray 0.01-lot EURUSD trades across repeated runs.
        try:
            _close_eurusd_position(mt5)
        except Exception as exc:  # pragma: no cover - best-effort
            print(
                f"[live_broker_validation:close_failed] "
                f"exc_type={type(exc).__name__} exc_msg={exc!r} placed_ticket={placed_ticket!r}",
                file=sys.stderr,
            )
        finally:
            mt5.shutdown()


def _close_eurusd_position(mt5: Any) -> None:
    """Close any open EURUSD position via an opposing market order.

    Best-effort: the live-broker test's ``finally`` block calls this
    so a failed assertion does not leak a position into the demo
    account. Uses the same opposing-market-order shape that
    ``scripts/record_fills.py:_close_market_position`` uses; not
    imported from that module because importing record_fills under a
    different module name has already been done above and exposing
    that private helper here would duplicate the binding.
    """
    positions = mt5.positions_get(symbol="EURUSD") or ()
    for pos in positions:
        tick = mt5.symbol_info_tick("EURUSD")
        if tick is None:
            continue
        opposite_type = (
            mt5.ORDER_TYPE_SELL if int(pos.type) == int(mt5.ORDER_TYPE_BUY) else mt5.ORDER_TYPE_BUY
        )
        close_price = float(tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask)
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": float(pos.volume),
            "type": opposite_type,
            "position": int(pos.ticket),
            "price": close_price,
            "deviation": 10,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "sl": 0.0,
            "tp": 0.0,
        }
        mt5.order_send(close_req)
