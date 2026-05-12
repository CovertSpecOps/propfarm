"""MT5 bridge risk spike — minimum viable order round-trip.

Purpose
-------
Throwaway script that answers a single binary question on Day 1 of Phase 0:
    "Can a Python process place a market order on an FTMO MT5 demo account,
     attach SL/TP, and close the position — today, with the chosen stack?"

If this script exits 0 with RTT < 2s, the `MetaTrader5` Python pkg stack is
viable and Task 2.1 (stack-lock ADR) proceeds with it locked in. If it exits
non-zero, fall back to the MQL5 + ZeroMQ architecture sketched in
`scripts/spike_mt5_fallback_zmq.md` before Day 2 ends.

Runs only on the Windows VPS where MT5 terminal is installed and logged in.
Reads credentials from `~/.propfarm-secrets.json` (never committed, never
synced to the macOS laptop).

Exit codes
----------
0   PASS — order opened, position observed, position closed, all retcodes
    were TRADE_RETCODE_DONE. Capture stdout RTT line and paste into STATUS.md.
1   AssertionError — `mt5.initialize` failed, or an order_send returned a
    non-DONE retcode. `mt5.last_error()` and the OrderSendResult are printed
    by the assertion message. This is a HARD FAIL: the spike has answered
    "no" for this stack on this host.
2   Any other unhandled exception (file missing, JSON parse error, symbol
    not found, network drop). Inspect traceback before declaring fallback —
    these are recoverable, unlike retcode failures.

Operational notes
-----------------
- Uses 0.01 lot EURUSD with SL 20 pips, TP 40 pips on a BUY market order.
- SL/TP are attached at submission time (preferred `MetaTrader5` pkg pattern).
- Close leg reuses most of the open req with action flipped to SELL and the
  position ticket attached.
- `time.sleep(2)` between open and close is deliberate — gives the demo
  server time to settle the position so `positions_get` returns it.
- Total wall-clock should be under ~3s including the sleep; the printed
  `send rtt_ms` is the network+broker round-trip for the order itself.

See `docs/runbooks/mt5-spike-runbook.md` for the full procedure.
"""

import MetaTrader5 as mt5
import json, time, pathlib

creds = json.loads(pathlib.Path.home().joinpath(".propfarm-secrets.json").read_text())["ftmo_demo"]
assert mt5.initialize(login=creds["login"], password=creds["password"], server=creds["server"]), mt5.last_error()

try:
    symbol = "EURUSD"
    info = mt5.symbol_info_tick(symbol)
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY, "price": info.ask,
        "sl": info.ask - 0.0020, "tp": info.ask + 0.0040,
        "deviation": 10, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    t0 = time.perf_counter()
    result = mt5.order_send(req)
    print(f"send rtt_ms={(time.perf_counter()-t0)*1000:.1f} retcode={result.retcode}")
    assert result.retcode == mt5.TRADE_RETCODE_DONE, result

    time.sleep(2)
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        raise SystemExit(f"no position after open (positions_get returned {positions!r}; last_error={mt5.last_error()})")
    pos = positions[0]
    close_req = {**req, "action": mt5.TRADE_ACTION_DEAL, "type": mt5.ORDER_TYPE_SELL,
                  "position": pos.ticket, "price": mt5.symbol_info_tick(symbol).bid}
    result = mt5.order_send(close_req)
    assert result.retcode == mt5.TRADE_RETCODE_DONE, result
finally:
    mt5.shutdown()
