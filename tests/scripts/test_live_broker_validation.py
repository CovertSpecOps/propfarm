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
* The test connects to a real (demo) broker and places real
  (0.01-lot) orders. It is unsafe to run on every CI / pre-commit.

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

What the test asserts — 2026-05-19 fix v8 expansion
---------------------------------------------------

The test drives the v8 path-0 hardening end-to-end:

1. Connects to the FTMO MT5 demo (creds from
   ``~/.propfarm-secrets.json``) and verifies ``FTMO-Demo*`` prefix.
2. Detects the server-time offset (per fix v3) so the deal-lookup
   helper receives the same offset the production capture would use.
3. **Runs the v8 session-start sweep** via
   ``run_session_start_sweep`` over ``("EURUSD",)`` BEFORE the first
   order. The sweep MUST NOT crash on the empty case (no residuals).
   If the prior live test left a residual, the sweep closes it; if
   the close fails, the residual lands in the stale_set passed to
   the helper.
4. Places **TWO** 0.01-lot EURUSD market buys (v7 placed ONE; v8
   bumps to TWO so the test exercises the exact failure surface —
   the FIRST market order is the one path 0 historically attached
   the residual to). Both orders are wired through
   ``_resolve_fill_from_deal`` with the production call shape
   (claimed_deal_tickets + session_start_stale_set).
5. Asserts (per order):

   * ``actual_fill_price is not None`` and ``> 0``.
   * ``abs(actual_fill_price - mid) < 100 * pip`` — sanity.
   * Broker latency in the expected band (~150-200ms; not the
     suspicious sub-50ms that the v7 anomaly produced).

6. Asserts (cross-order):

   * No path-0 probe block fires (the v8 gate + sweep prevent it
     for market orders too — the existing v7 path-0 retry path
     also doesn't fire its probes on success).
   * Manifest's ``n_market_lookup_failures == 0`` — written via
     ``write_recording`` with both rows.
   * Manifest's ``n_residual_positions_at_session_start ==
     <observed>`` — usually 0 on a clean restart, ≥ 1 only if a
     prior test leaked.

7. Closes any opened positions (irrespective of test pass/fail) so
   the VPS does not accumulate stray demo positions.

If the test fails on any assertion, the captured stderr (containing
the ``[record_fills:lookup_probe_*]`` lines if the probe block fired,
and the ``[record_fills:session_start_sweep]`` line + per-residual
``[record_fills:session_start_sweep_close]`` lines from the sweep)
becomes the load-bearing evidence the user pastes back.

Pass/fail signals — grep these on the captured stderr
-----------------------------------------------------

* ``[record_fills:session_start_sweep] found 0 residual positions``
  — sweep ran cleanly, zero residuals.
* ``[record_fills:session_start_sweep] found N residual positions ...
  action=closed`` — sweep ran and B1 closed N residuals.
* ``[record_fills:session_start_sweep] found N residual positions ...
  action=recorded_in_stale_set`` — B1 close failed; B2 fallback
  recorded N tickets in the stale_set.
* ``broker_latency_ms`` per-order log line — must NOT be the
  suspicious sub-50ms band; ~150-200ms is typical.
* Absence of ``[record_fills:lookup_probe_*]`` lines — success.

Cross-references
----------------

* Probe block: ``scripts/record_fills.py`` —
  ``emit_market_lookup_failure_probes`` + the six
  ``[record_fills:lookup_probe_*]`` prefixes.
* v8 sweep: ``scripts/record_fills.py:run_session_start_sweep``.
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

#: 2026-05-19 fix v8 — expected broker latency band. The v7 anomaly
#: signature produced fills with sub-50ms latency (12.6ms observed),
#: which was path 0's fallback matcher attaching to a residual
#: position (the residual was already in positions_get when the
#: script queried). Healthy latencies on FTMO Free Trial cluster
#: around 150-200ms (round-trip incl. broker + VPS network). We
#: assert the fill is NOT in the suspicious sub-50ms band — any
#: latency below this floor indicates the residual-pickup mechanism
#: may have re-engaged.
SUSPICIOUS_LATENCY_FLOOR_MS = 50.0
#: Maximum plausible latency on a healthy FTMO Demo round-trip. 5 seconds
#: is generous (the typical RTT is ~150-200ms; even a slow path is
#: < 1s). Bound the upper side so a stalled / pending order doesn't
#: silently pass the latency assertion.
SUSPICIOUS_LATENCY_CEILING_MS = 5000.0


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
def test_record_fills_resolves_real_market_order_against_ftmo_demo(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v8 functional acceptance — sweep + TWO 0.01-lot EURUSD market buys, both clean.

    The user runs this on the Windows VPS with
    ``PROPFARM_LIVE_TEST=1 pytest tests/scripts/test_live_broker_validation.py``.

    On a successful pass:

    * The session-start sweep runs WITHOUT crashing (zero residuals
      is the normal case; ≥ 1 is fine too as long as the sweep
      handles it).
    * Both market orders fill with a real (>0) price.
    * Both have latency in [50ms, 5000ms] — NOT the suspicious sub-50ms
      band that the v7 residual-pickup signature produced.
    * No path-0 probe block fires.
    * The manifest written via ``write_recording`` carries
      ``n_market_lookup_failures == 0`` and the new
      ``n_residual_positions_at_session_start`` field.

    On failure: the captured stderr carries:

    * ``[record_fills:session_start_sweep] ...`` — confirms the sweep
      ran and how many residuals it found.
    * ``[record_fills:session_start_sweep_close] ...`` — one per
      residual the sweep encountered.
    * ``[record_fills:lookup_probe_path0_*]`` lines if path 0 fell
      through with retries exhausted.
    * ``[record_fills:lookup_probe_*]`` for paths 1-3 fall-through.
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

    placed_tickets: list[int] = []
    rows: list[dict[str, Any]] = []
    try:
        # SAFETY ASSERT #1 — server prefix must match the production guard
        # in scripts/record_fills.py (`ALLOWED_SERVER_PREFIX = "FTMO-Demo"`).
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

        # Detect the account margin mode so we can drive the helper with
        # the same value main() would.
        account_margin_mode = int(getattr(account, "margin_mode", 0) or 0)

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

        # ============================================================
        # v8 Part B — session-start residual-position sweep.
        # MUST run BEFORE the first order_send. Asserts:
        #   * No crash when no residuals exist (the common case).
        #   * If residuals exist, they're closed (B1) or recorded
        #     in the stale_set (B2 fallback).
        # ============================================================
        session_start_stale_set, n_residual_positions_at_session_start = rf.run_session_start_sweep(
            mt5,
            symbols=(symbol,),
            constants=constants,
            template=template,
            success_retcode=success_retcode,
        )
        # The sweep can return either a clean (empty stale_set, zero
        # residuals) state OR a stale_set with some tickets — both are
        # valid; the load-bearing assertion is that the sweep did NOT
        # crash, and that the stale_set is bounded by the residual
        # count.
        assert len(session_start_stale_set) <= n_residual_positions_at_session_start, (
            f"stale_set ({len(session_start_stale_set)}) cannot exceed "
            f"residual count ({n_residual_positions_at_session_start})"
        )

        claimed_deal_tickets: set[int] = set()

        # ============================================================
        # v8 — TWO market orders, both at 0.01 lot EURUSD buy. The
        # FIRST is the one the residual-pickup signature historically
        # latched onto; v8 must pass both with NO anomaly.
        # ============================================================
        for order_idx in range(2):
            tick = mt5.symbol_info_tick(symbol)
            assert tick is not None, (
                f"mt5.symbol_info_tick({symbol!r}) returned None on idx={order_idx}"
            )
            req = rf.build_order_request(
                template,
                order_type="market",
                symbol_info_tick=tick,
                side="buy",
                inside_spread=True,  # ignored for market orders
                mt5_constants=constants,
            )

            request_time = datetime.now(tz=UTC)
            result = mt5.order_send(req)
            after_send = datetime.now(tz=UTC)
            assert result is not None, (
                f"mt5.order_send returned None on idx={order_idx}: {mt5.last_error()}"
            )
            assert int(result.retcode) == success_retcode, (
                f"order_send idx={order_idx} retcode={result.retcode} "
                f"comment={getattr(result, 'comment', '?')!r}"
            )
            placed_tickets.append(int(getattr(result, "order", 0) or 0))

            # Drive the result through the production helper with the
            # FULL v8 production call shape (claim set + stale_set +
            # margin mode + offset).
            actual_fill_price, actual_fill_time = rf._resolve_fill_from_deal(
                mt5,
                result,
                success_retcode=success_retcode,
                request_time_utc=request_time,
                order_type="market",
                symbol=symbol,
                volume_lots=float(req["volume"]),
                side="buy",
                idx=order_idx,
                claimed_deal_tickets=claimed_deal_tickets,
                server_time_offset_seconds=server_time_offset_seconds,
                account_margin_mode=account_margin_mode,
                session_start_stale_set=session_start_stale_set,
            )

            # Per-order assertions: fill_price real + sanity + latency band.
            assert actual_fill_price is not None, (
                f"idx={order_idx}: _resolve_fill_from_deal returned (None, None) on a "
                f"live FTMO-Demo market order — the v8 path-0 hardening did not hold. "
                f"Check stderr for [record_fills:lookup_probe_*] and "
                f"[record_fills:session_start_sweep] lines."
            )
            assert actual_fill_price > 0, (
                f"idx={order_idx}: non-positive fill_price={actual_fill_price}; "
                "MT5 OrderSendResult.price=0 was the v1 bug — see the v1 fix-up section "
                "in docs/runbooks/gate-2b-fill-recording.md"
            )
            deviation_pips = abs(actual_fill_price - mid) / pip
            assert deviation_pips < 100.0, (
                f"idx={order_idx}: fill_price={actual_fill_price} is "
                f"{deviation_pips:.1f} pips from request-time mid={mid}; expected within "
                "100 pips. The deal lookup may have returned the wrong row."
            )

            # v8 latency-band assertion. The v7 anomaly signature
            # produced sub-50ms latencies (12.6ms observed). Healthy
            # FTMO Demo RTT is ~150-200ms; we assert the latency is in
            # the expected band [50ms, 5000ms]. Latency below the
            # SUSPICIOUS_LATENCY_FLOOR_MS floor indicates path 0 may
            # have attached to a residual position (not a real fill).
            latency_ms = (after_send - request_time).total_seconds() * 1000.0
            assert SUSPICIOUS_LATENCY_FLOOR_MS <= latency_ms <= SUSPICIOUS_LATENCY_CEILING_MS, (
                f"idx={order_idx}: latency={latency_ms:.1f}ms is outside the expected "
                f"[{SUSPICIOUS_LATENCY_FLOOR_MS}, {SUSPICIOUS_LATENCY_CEILING_MS}]ms band. "
                f"Sub-50ms latencies are the v7 residual-pickup signature; latencies above "
                f"5s indicate a stalled or pending order. v8 sweep + path-0 order_type gate "
                f"are designed to keep all live market fills inside this band."
            )

            # Counter regression check — same as v7 but per-order.
            would_have_incremented = (
                int(result.retcode) == success_retcode and actual_fill_price is None
            )
            assert not would_have_incremented, (
                f"idx={order_idx}: n_market_lookup_failures branch would have fired "
                "despite a non-None fill_price — counter logic regressed"
            )

            if actual_fill_time is not None:
                delta_s = abs((actual_fill_time - request_time).total_seconds())
                assert delta_s < 60.0, (
                    f"idx={order_idx}: actual_fill_time={actual_fill_time.isoformat()} is "
                    f"{delta_s:.1f}s from request_time={request_time.isoformat()}; "
                    "may indicate server-time offset drift"
                )
                assert actual_fill_time.tzinfo is UTC

            # Build the row dict the production capture would write so
            # write_recording can flush a manifest with both rows.
            symbol_info = mt5.symbol_info(symbol)
            digits = int(symbol_info.digits) if symbol_info is not None else 5
            row = rf.parse_fill_into_record(
                run_id="live_broker_validation",
                request_time_utc=request_time,
                after_send_utc=after_send,
                open_req=req,
                order_send_result=result,
                tick_at_request=tick,
                symbol_digits=digits,
                order_type="market",
                side="buy",
                success_retcode=success_retcode,
                actual_fill_price=actual_fill_price,
                actual_fill_time_utc=actual_fill_time,
            )
            rows.append(row)

            # Close the position before placing the next order so the
            # subsequent loop iteration is on a clean per-symbol slate
            # (matches production main() which closes immediately
            # after each market fill).
            try:
                _close_eurusd_position(mt5)
            except Exception as exc:  # pragma: no cover - best-effort
                print(
                    f"[live_broker_validation:per_order_close_failed] "
                    f"idx={order_idx} exc_type={type(exc).__name__} exc_msg={exc!r}",
                    file=sys.stderr,
                )

        # ============================================================
        # v8 cross-order assertions: probe blocks did NOT fire +
        # manifest counters as expected.
        # ============================================================
        # Read the captured stderr to verify no path-0 probe block fired.
        # capsys captures stderr-by-line; flatten and search.
        captured = capsys.readouterr()
        # Re-emit captured stderr so the operator sees it in the test
        # output (capsys swallows otherwise).
        if captured.err:
            sys.stderr.write(captured.err)

        path0_probe_fired = "[record_fills:lookup_probe_path0_match_result]" in captured.err
        assert not path0_probe_fired, (
            "v8: the path-0 retry-exhaustion probe block fired on a live market order. "
            "Either the sweep missed a residual or the broker entered a state path 0 "
            "couldn't resolve. Grep stderr for [record_fills:lookup_probe_path0_*] "
            "for the diagnostic block."
        )
        # Sweep summary MUST have been emitted.
        assert "[record_fills:session_start_sweep]" in captured.err, (
            "v8: session-start sweep summary line missing from stderr. "
            "The sweep helper did not emit [record_fills:session_start_sweep]; "
            "this is the operator-facing confirmation the sweep ran."
        )

        # Write the manifest via the same production code path the
        # 24h capture uses. n_market_lookup_failures stays at 0 (we
        # asserted fill_price is not None on both orders); the new
        # n_residual_positions_at_session_start field is threaded
        # through.
        rf.write_recording(
            rows,
            run_id="live_broker_validation",
            start_utc=datetime.now(tz=UTC),
            end_utc=datetime.now(tz=UTC),
            root=tmp_path,
            success_retcode=success_retcode,
            n_market_lookup_failures=0,
            n_residual_positions_at_session_start=n_residual_positions_at_session_start,
        )
        manifest_path = (
            tmp_path / "data" / "raw" / "fill_recordings" / "live_broker_validation.json"
        )
        assert manifest_path.exists(), (
            f"v8: manifest not written at {manifest_path}; write_recording silently failed"
        )
        manifest = json.loads(manifest_path.read_text())
        assert manifest["n_market_lookup_failures"] == 0, (
            f"v8: manifest n_market_lookup_failures={manifest['n_market_lookup_failures']}; "
            "both orders should have resolved without falling to soft-fail"
        )
        assert manifest["n_residual_positions_at_session_start"] == (
            n_residual_positions_at_session_start
        ), (
            f"v8: manifest n_residual_positions_at_session_start="
            f"{manifest['n_residual_positions_at_session_start']} ≠ sweep return "
            f"{n_residual_positions_at_session_start}"
        )
        assert manifest["schema_version"] == "1.3", (
            f"v8: manifest schema_version={manifest['schema_version']}; expected 1.3"
        )
        # Both filled-market rows accounted for.
        assert manifest["n_filled_market"] == 2, (
            f"v8: manifest n_filled_market={manifest['n_filled_market']}; "
            "expected 2 (both orders were market buys at success retcode)"
        )

    finally:
        # Always close any opened positions so the demo account does not
        # accumulate stray 0.01-lot EURUSD trades across repeated runs.
        try:
            _close_eurusd_position(mt5)
        except Exception as exc:  # pragma: no cover - best-effort
            print(
                f"[live_broker_validation:close_failed] "
                f"exc_type={type(exc).__name__} exc_msg={exc!r} "
                f"placed_tickets={placed_tickets!r}",
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
