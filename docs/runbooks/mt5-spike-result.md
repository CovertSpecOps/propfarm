# MT5 bridge spike — recorded results

Datapoints from the live spike runs. ADR-0002 (stack-lock) and ADR-0003
(bridge choice) cite this file for empirical evidence — do not silently
overwrite entries; append a new dated row for each rerun.

## Run 1 — 2026-05-12 (PARTIAL PASS; cleanup-leg bug)

| Field | Value |
|---|---|
| Date | 2026-05-12 |
| VPS | Vultr Amsterdam, `voc-c-2c-4gb-50s`, Win Server 2022 Std |
| VPS IP | `95.179.153.105` |
| Python | 3.14 on VPS (cp314 wheels available for MetaTrader5 5.0.5735) |
| MT5 server | `FTMO-Demo` ($10k Free Trial account) |
| MT5 pkg version | 5.0.5735 |
| FTMO Algo Trading | enabled |
| Open leg | **PASS** — retcode 10009 (`TRADE_RETCODE_DONE`) |
| **Open RTT** | **151.4 ms** (Amsterdam → FTMO) |
| Close leg | **FAIL** — retcode 10016 (`INVALID_STOPS`) |
| Root cause | `close_req = {**open_req, ...}` spread inherited `sl` and `tp` from the open leg; close-by-ticket deals must not carry stops |
| Fix | `_build_close_req` helper builds the close request from scratch with `sl=0.0` / `tp=0.0`; regression test in `tests/scripts/test_spike_mt5.py` locks the behavior |
| Bridge verdict | **PROVEN** — open leg confirms nautilus-trader → MetaTrader5 pkg → FTMO MT5 path is viable |

### What ADR-0002 should cite from this run

- **Stack viability:** `MetaTrader5` Python pkg (5.0.5735) on Windows VPS,
  authenticated against FTMO-Demo, places a market order with attached
  SL/TP and receives a `TRADE_RETCODE_DONE` response with a deterministic
  ticket. The chosen stack works.
- **Acceptable latency floor:** 151.4 ms round-trip from Amsterdam to FTMO
  on a 4 GB Vultr Windows VPS. Comfortably under the spike's 2 s gate and
  under Phase-0 Gate 2's required p95 < 500 ms over 10 cycles.
- **FTMO ToS compliance:** a single 0.01-lot BUY with inline SL/TP and a
  ~2 s hold is not flagged as HFT, latency arbitrage, or tick-scalping by
  any clause in the current FTMO Forbidden Trading Practices page.
- **Open question for re-run:** confirm a clean PASS end-to-end after the
  close-leg fix, then close ADR-0002 + ADR-0003 with the second result row.

## Run 2 — 2026-05-12 (CLEAN PASS)

| Field | Value |
|---|---|
| Date | 2026-05-12 (same day, after patch) |
| VPS | unchanged — Vultr Amsterdam, `95.179.153.105` |
| Python | unchanged (3.14) |
| MT5 server | unchanged (`FTMO-Demo`, $10k Free Trial) |
| MT5 pkg version | unchanged (5.0.5735) |
| Open leg | **PASS** — retcode 10009 |
| **Open RTT** | **167.5 ms** |
| Close leg | **PASS** — retcode 10009 (no AssertionError; `{**req, ..., "sl": 0.0, "tp": 0.0}` fix held) |
| Exit code | 0 |

### Combined empirical latency band (Run-1 open + Run-2 open)

- Run-1: 151.4 ms
- Run-2: 167.5 ms
- **Range: 150–170 ms** Amsterdam → FTMO
- Both samples are single-shot opens; a 10-cycle distribution is still
  required to confirm Phase-0 Gate 2's p95 < 500 ms claim.

### What ADR-0002 and ADR-0003 cite

- Run-2 is the empirical justification for closing both ADRs. The
  150–170 ms band is the latency floor we expect from a Frankfurt-class
  EU VPS to FTMO's demo cluster.
- Future spike re-runs (e.g. for Phase-0 Gate 2 acceptance — the
  simulator-vs-live fill comparison) append new dated rows here. Do not
  edit Run-1 or Run-2 in place.

## Future runs — protocol

- Append a new dated row with the same field set.
- If the open / close RTT regresses materially (> 300 ms sustained),
  open an issue and re-check VPS region, FTMO server load (status.ftmo.com),
  and Windows Update activity on the VPS.
- The next planned run is Phase-0 Gate 2's 10-cycle distribution capture,
  which produces (open_rtt, close_rtt, sim_predicted_price,
  live_actual_price) tuples for divergence analysis.
