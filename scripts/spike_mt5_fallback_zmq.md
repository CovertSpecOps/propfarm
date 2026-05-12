# Fallback: MQL5 EA + ZeroMQ bridge

**Status:** Contingency design. Instantiate **only** if `scripts/spike_mt5.py`
fails for a reason that doesn't go away with reinstall / different server /
different filling mode (i.e. the `MetaTrader5` Python pkg itself is the
blocker — Windows version conflict, MetaQuotes pulls the pkg, terminal
refuses programmatic logins, etc.).

**Lineage:** Darwinex `dwx-zeromq-connector` (MIT licensed, MQL4 originally,
MQL5 fork by community). That pattern is the proven reference for
Python ↔ MT5 over ZeroMQ. We are not redistributing their code; we copy the
*architecture* and write a minimal MQL5 EA + Python client to spec.

## Architecture

```
+----------------------+        REQ/REP        +---------------------------+
|  Python client       | <-------------------> |  MT5 terminal (Windows)   |
|  (any OS w/ pyzmq)   |   tcp://vps:5555      |  Expert Advisor on chart  |
|                      |                       |    - ZMQ REP socket       |
|                      |                       |    - dispatches to        |
|                      |                       |      OrderSend / etc.     |
|                      |                       |                           |
|                      | <-------------------- |    - PUB on tcp://...:5556|
|                      |   PUB/SUB (ticks)     |                           |
+----------------------+                       +---------------------------+
```

Two sockets:

1. **REQ/REP on `:5555`** — commands. Client sends JSON request, EA replies
   with JSON result. Synchronous, one in flight at a time. Sufficient for
   our throughput (we're not HFT — FTMO bans it anyway).
2. **PUB/SUB on `:5556`** — market data. EA publishes ticks per subscribed
   symbol. Client subscribes by topic prefix. Out of scope for the spike;
   spike only needs REQ/REP.

## MQL5 EA template (sketch — DwxConnect-style)

Save as `PropFarmZmqBridge.mq5` and attach to one (any) chart in the
terminal. Compile against the **MQL5-ZMQ** library bindings
(`github.com/dingmaotu/mql-zmq` — header-only, MIT, drop into
`MQL5/Include/Zmq/`).

```mql5
#property strict
#include <Zmq/Zmq.mqh>
#include <stdlib.mqh>

input string ZMQ_REP_ENDPOINT = "tcp://*:5555";

Context ctx("PropFarmBridge");
Socket rep(ctx, ZMQ_REP);

int OnInit() {
    if (!rep.bind(ZMQ_REP_ENDPOINT)) {
        Print("ZMQ bind FAILED: ", ZMQ_REP_ENDPOINT);
        return INIT_FAILED;
    }
    EventSetMillisecondTimer(50);   // poll every 50ms
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
    EventKillTimer();
    rep.unbind(ZMQ_REP_ENDPOINT);
}

void OnTimer() {
    ZmqMsg request;
    if (!rep.recv(request, ZMQ_DONTWAIT)) return;  // no pending request
    string body = request.getData();
    string reply = Dispatch(body);
    ZmqMsg out(reply);
    rep.send(out);
}

string Dispatch(string body) {
    // Parse JSON: {"op":"order_send","symbol":"EURUSD","type":"BUY","volume":0.01,...}
    // Build MqlTradeRequest from fields, call OrderSend, marshal result to JSON.
    // Ops to implement for spike: ping, account_info, order_send, positions_get,
    // order_send_close. Each returns {"ok":bool,"retcode":int,"data":{...}}.
    return JsonStringify(/* ... */);
}
```

Notes for the implementation pass:

- MQL5 has no built-in JSON. Use `MQL5/Include/Json.mqh` (community,
  permissive license) or hand-roll string concat for the spike — five ops
  total, not worth pulling a parser.
- Always wrap `OrderSend` so `GetLastError()` is captured into the reply
  on failure. The Python client needs to see retcode + last_error to make
  the same decisions the direct `MetaTrader5` pkg surface would.
- AlgoTrading must be enabled in the terminal (smiley face green) or the
  EA's `OrderSend` silently no-ops.

## Python client surface

Drop-in replacement for the parts of `MetaTrader5` we use. File would
live at `src/propfarm/bridge/zmq_client.py` once promoted out of spike.

```python
import zmq, json, time, uuid

class Mt5ZmqClient:
    def __init__(self, host: str, req_port: int = 5555, timeout_ms: int = 5000):
        self._ctx = zmq.Context.instance()
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._req.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self._req.connect(f"tcp://{host}:{req_port}")

    def _call(self, op: str, **kwargs) -> dict:
        msg = {"id": uuid.uuid4().hex, "op": op, **kwargs}
        self._req.send_string(json.dumps(msg))
        return json.loads(self._req.recv_string())

    def ping(self) -> bool: return self._call("ping").get("ok", False)
    def account_info(self) -> dict: return self._call("account_info")["data"]
    def symbol_info(self, symbol: str) -> dict:
        return self._call("symbol_info", symbol=symbol)["data"]
    def order_send(self, **req) -> dict: return self._call("order_send", **req)
    def positions_get(self, symbol: str) -> list[dict]:
        return self._call("positions_get", symbol=symbol)["data"]
    def close_position(self, ticket: int) -> dict:
        return self._call("close_position", ticket=ticket)
    def close(self) -> None: self._req.close()

# Spike usage mirrors spike_mt5.py:
# c = Mt5ZmqClient(host="vps.ip")
# t0 = time.perf_counter()
# r = c.order_send(symbol="EURUSD", type="BUY", volume=0.01, sl=..., tp=...)
# assert r["ok"] and r["retcode"] == 10009
```

## Trade-offs vs. direct `MetaTrader5` pkg

| Axis | Direct pkg | ZMQ + EA |
|---|---|---|
| Setup time | minutes | ~half-day (compile EA, attach, debug) |
| Python OS | Windows only | any |
| Failure modes | pkg version drift | EA crash, ZMQ socket leaks, JSON marshal bugs |
| Broker RTT (typical) | ~50–150 ms to FTMO EU | same + ~10–20 ms ZMQ hop on localhost-VPS |
| Auditability | opaque DLL | source of EA visible to us |
| FTMO ToS risk | none (uses official MT5 API) | none (EA is just MQL5; same surface) |

## Hard parts (don't gloss)

1. **Threading inside the EA.** MT5's `OnTimer` runs on the terminal's
   main thread. A blocking `OrderSend` blocks the EA from servicing the
   next ZMQ request. The Darwinex pattern handles this with REP being
   strictly synchronous and the client treating the terminal as serial.
   Fine for us at FTMO volumes.
2. **Reconnect on terminal restart.** If the terminal restarts, the EA
   re-binds. The Python client's REQ socket will be stuck in a bad state
   if a recv was in flight. Mitigation: short `RCVTIMEO` + recreate the
   REQ socket on each timeout. Built into the client sketch above.
3. **MQL5 → JSON of `MqlTradeResult`.** Many fields. Easy to miss one
   (e.g., `request_id`, `comment`). Define the wire schema once and write
   round-trip tests against a known-good `MetaTrader5` pkg output captured
   from the direct spike if it ever passed even once.
4. **Symbol info for SL/TP point-rounding.** `MetaTrader5` pkg returns
   `SymbolInfo` with `point` and `digits`. Need to expose `symbol_info`
   over the bridge too, otherwise the Python side can't compute correct
   SL/TP offsets per symbol.

## When to abandon both stacks

If ZMQ fallback also fails, the remaining option is **nautilus-trader's
own MT5 adapter** (if it has stabilized by the time we get here) or
switching firms to one with a native FIX/REST gateway (most FX prop firms
are MT4/MT5 only — this would mean re-evaluating the firm shortlist, not
just the bridge). Document in ADR-0003 before doing anything drastic.
