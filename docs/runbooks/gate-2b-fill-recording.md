# Gate 2B — fill-recording protocol

Goal: capture **100+ real broker fills** on the FTMO MT5 demo across 24–48h
of session diversity, so the Gate 2B comparison (simulator predicted price
vs. live broker fill) can run later from parquet without holding an MT5
session open.

**This runbook is operator-facing**: you execute it on the Windows VPS;
the script does the work. The pure helpers it relies on are
locked by `tests/scripts/test_record_fills.py` (30 tests, all green; see
the 2026-05-14 fix-up section at the end of this runbook before any
re-record).

## Why pre-record (not capture during the gate)

Gate 2B compares `propfarm.sim.engine.fill()` predictions against real
broker fills. Doing this **live during the gate** would couple gate
execution to broker connectivity, VPS uptime, and FTMO server load.
Pre-recording produces a fixed parquet corpus that the gate consumes
deterministically — repeatable, network-free, and inspectable.

The simulator side of Gate 2B is built and tested in pure Python; this
recording protocol owns the live-broker side.

## 0. Prerequisites (must all be ✅)

- [ ] Windows VPS up (Vultr Amsterdam from the spike; or equivalent EU host).
- [ ] MT5 terminal installed, logged into the **FTMO Free Trial demo**
      (server starts with `FTMO-Demo`).
- [ ] `MetaTrader5` Python package installed (`python -m pip install MetaTrader5`,
      already present from `spike_mt5.py`).
- [ ] `~/.propfarm-secrets.json` on the VPS with the FTMO creds (from the
      Day-1 spike — do NOT recreate; reuse the existing file).
- [ ] **Algo Trading enabled** in the MT5 terminal (button top-left,
      green when on). If off, every `order_send` will fail retcode 10027.
- [ ] The prop-farm repo is cloned (or just the `scripts/record_fills.py`
      file is dropped onto the VPS) at a path where you can run it.
- [ ] `polars` + `pydantic` + `pyarrow` installed on the VPS Python
      (`python -m pip install polars pydantic pyarrow`). The recording
      script imports these at module-load time.

## 1. Start the session

**You must NOT need to keep an RDP session open for 24-48 hours.**
PowerShell processes are tied to your interactive session and die when
RDP disconnects (default Windows Server behavior on disconnect). The two
supported ways to run the script through a disconnect:

### 1A. Recommended: Windows Task Scheduler

Creates a true detached process owned by the system, survives logout
and disconnect, restarts automatically if Windows reboots.

From an Administrator PowerShell on the VPS:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Python311\python.exe" `
    -Argument "C:\propfarm\scripts\record_fills.py --duration-hours 24 --n-samples 200" `
    -WorkingDirectory "C:\propfarm"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 49) `
    -RestartCount 0

$principal = New-ScheduledTaskPrincipal `
    -UserId "Administrator" `
    -LogonType S4U `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName "PropfarmFillRecording" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Gate 2B fill recording (24h window)"
```

Adjust `C:\Python311\python.exe` and `C:\propfarm` to match your install
paths. The task fires in 1 minute; if you change `--duration-hours`,
bump `ExecutionTimeLimit` by at least 1h so Windows doesn't kill it on
the hard cap. Logs go to the script's own stdout — redirect inside the
script invocation if you want a file (`*>> C:\propfarm\record.log`).

After registering, you can RDP disconnect (or even logout) and the
task keeps running. Reconnect later and check progress:

```powershell
Get-ScheduledTask -TaskName "PropfarmFillRecording" | Get-ScheduledTaskInfo
Get-Process python | Select-Object Id, StartTime, CPU
```

Stop early if you need to:

```powershell
Stop-ScheduledTask -TaskName "PropfarmFillRecording"
```

Cleanup after the session completes:

```powershell
Unregister-ScheduledTask -TaskName "PropfarmFillRecording" -Confirm:$false
```

### 1B. Alternative: `Start-Process -WindowStyle Hidden`

Lighter-weight than Task Scheduler but tied to your user session.
**Survives RDP disconnect** (the disconnect does NOT terminate user
processes by default; only an explicit *logout* does). If you only
plan to disconnect-and-reconnect, this is the simpler path.

```powershell
cd <repo-root>
Start-Process `
    -FilePath "python.exe" `
    -ArgumentList "scripts\record_fills.py","--duration-hours","24","--n-samples","200" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "record.log" `
    -RedirectStandardError "record.err.log"
```

The process is now backgrounded. Tail the log to follow progress:

```powershell
Get-Content -Path "record.log" -Wait -Tail 20
```

Find/kill the process if needed:

```powershell
Get-Process python | Where-Object { $_.MainWindowTitle -eq "" }
Stop-Process -Id <pid>
```

### 1C. NOT recommended: foreground PowerShell

```powershell
python scripts\record_fills.py --duration-hours 24 --n-samples 200
```

Works only if you keep RDP connected for the full 24-48h. Dies on RDP
disconnect if your Windows Server session policy is the default. Use
this only for `--dry-run` schedule previews or short test runs.

### What every path does

- Builds a deterministic 200-sample schedule covering London / NY / Tokyo
  session opens, mid-session quiet zones, and a 70% spread across the rest
  of the 24h window.
- Mix: ~60% market, ~25% limit (mixed inside/outside spread), ~15% stop.
- Symbols: EURUSD and GBPUSD by default. Override with
  `--symbols EURUSD,GBPUSD,USDJPY` if you want a third symbol.
- The script prints `run_id` on stdout — **copy it now** in case the
  session crashes and you need to resume.

You can preview the schedule without connecting to MT5 by adding
`--dry-run`. That prints all 200 (timestamp, symbol, order_type, side)
tuples and exits.

### What "running" looks like

For each scheduled sample, the script:

1. `time.sleep`s until the target UTC instant.
2. Snapshots `bid`/`ask` via `symbol_info_tick`.
3. Sends the order with `mt5.order_send`.
4. Records one row of (request_time, broker_fill_time, symbol, order_type,
   side, requested_price, fill_price, spread_at_request_pips,
   slippage_observed_pips, broker_latency_ms, retcode, comment).
5. **Closes** the position immediately (market round-trip) or **cancels**
   the pending order (limit/stop) — so positions never accumulate.
6. Every 10 fills, flushes to disk.

Expect a console line per attempt like:

```
[record_fills] idx=047 EURUSD market buy retcode=10009 fill=1.10847 slip_pips=0.30 latency_ms=152.4
```

## 2. What to expect across 24h

- ~200 attempted fills.
- Of those, roughly:
  - 140 market orders → 130–140 filled (small reject rate when spread
    explodes mid-NFP or the server requotes).
  - 50 limit orders → 30 filled within their window, 20 rejected /
    cancelled-unfilled (outside-spread limits are *meant* to reject — we
    want the reject behaviour on record).
  - 30 stop orders → most rest pending and are cancelled when the
    next sample fires; the few that fire become market-like fills.
- **You should clear the 100-filled threshold comfortably**; the 200
  attempts are budgeted with ~30% headroom for rejects.

## 3. If the session crashes mid-run

The script flushes every 10 fills, so at most ~10 records are lost.

To **resume** with the same `run_id`:

```powershell
python scripts\record_fills.py --duration-hours 24 --n-samples 200 --run-id <RUN_ID_FROM_FIRST_RUN>
```

Resume mode:

- Builds a fresh schedule (the second run's calendar starts at "now",
  not at the original `start_utc`).
- Writes to **the same parquet** (`data/raw/fill_recordings/{run_id}.parquet`)
  in append mode.
- Rewrites the manifest at the end with the cumulative counts.

If you can't find the `run_id`: it's the filename of the latest
`*.json` under `data/raw/fill_recordings/`.

### Common crash causes & fixes

| Symptom | Cause | Fix |
|---|---|---|
| `mt5.initialize failed` | terminal not running / wrong creds | restart terminal, verify login on the MT5 GUI, re-check `~/.propfarm-secrets.json` |
| `refusing to record on server=...` | connected to a non-demo server | log out, log back into FTMO-Demo, restart script |
| retcode 10027 on every order | Algo Trading disabled | toggle the Algo Trading button in the terminal |
| retcode 10030 on every order | unsupported filling mode | edit `type_filling` in the template from `ORDER_FILLING_IOC` to `ORDER_FILLING_FOK` |
| `position cap reached; session aborted` | something failed to close 5 positions | inspect MT5 Trade tab, close manually, re-run |
| retcode 10018 (Market closed) on weekends | FX closes Fri 22:00 UTC | wait for Sun 22:00 UTC; expected on weekends |

## 4. Where the data lands

- Parquet: `data/raw/fill_recordings/{run_id}.parquet` — one row per attempt.
- Manifest: `data/raw/fill_recordings/{run_id}.json` — summary + schema
  version.

The parquet is **gitignored** (per the repo `.gitignore`'s
`data/raw/` rule); the manifest is small enough that you can paste its
contents into a STATUS.md update.

### Parquet schema (v1.0 — unchanged)

(FillRecord column set is locked at v1.0 even though SessionManifest
bumped to v1.1 on 2026-05-14 fix-up #2. Only the sidecar JSON manifest
gained the new ``n_market_lookup_failures`` field; the parquet columns
below are untouched.)


| Column | Type | Notes |
|---|---|---|
| `run_id` | utf8 | identical across every row from one recording session |
| `request_time_utc` | timestamp (tz=UTC) | when `order_send` was called |
| `broker_fill_time_utc` | timestamp (tz=UTC) | from the deal record's `time` field (via `mt5.history_deals_get(ticket=result.deal)`); `after_send` only on rejected fills or soft-fail deal lookup. See 2026-05-14 fix-up section |
| `symbol` | utf8 | EURUSD / GBPUSD / USDJPY / etc |
| `order_type` | utf8 | `market` / `limit` / `stop` |
| `side` | utf8 | `buy` / `sell` |
| `volume_lots` | float64 | always 0.01 |
| `requested_price` | float64 | price field of the request |
| `fill_price` | float64 | from the deal record (NOT `result.price` — that is 0 for MT5 market orders); NaN on reject or soft-fail deal lookup. See 2026-05-14 fix-up section |
| `spread_at_request_pips` | float64 | `(ask - bid) / pip` from the tick at request |
| `slippage_observed_pips` | float64 | signed adverse — positive = bad for trader |
| `broker_latency_ms` | float64 | `(after_send - request_time) * 1000` |
| `retcode` | int64 | MT5 retcode; 10009 = `TRADE_RETCODE_DONE` |
| `comment` | utf8 | `result.comment` if any |

## 5. Safety checklist (the script enforces these; verify on first launch)

- [ ] **Server name starts with `FTMO-Demo`** — script bails with
      `refusing to record on server=...` if not. This is the single most
      important safety belt.
- [ ] **Volume is 0.01 lot** every time (the smallest size FTMO accepts;
      `LOT_SIZE` constant in the script).
- [ ] **Max 5 simultaneous open positions** — if reached, the script
      sweep-closes everything and aborts the session with a clear log.
- [ ] **48h hard wall-clock cap** regardless of `--duration-hours`.
      Useful if you forget the script is running.
- [ ] **No SL/TP on recording orders** — keeps slippage attribution clean
      (stops introduce their own fill behaviour that's orthogonal to
      Gate 2B's question).
- [ ] **Credentials and VPS IPs are never written** into the parquet or
      manifest. Manifest has `vps_host_redacted: true` as a constant
      reminder.

## 6. End of session — what to paste back

After 24h, the script exits cleanly. Paste this into the chat:

```
record_fills.py result: PASS|FAIL
run_id: <hex>
manifest:
<contents of data/raw/fill_recordings/{run_id}.json>
```

The manifest content is safe to paste — no creds, no IPs. Example:

```json
{
  "run_id": "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d",
  "start_utc": "2026-05-13T07:00:00+00:00",
  "end_utc": "2026-05-14T07:00:00+00:00",
  "n_attempted": 200,
  "n_filled": 167,
  "n_rejected": 33,
  "n_market_lookup_failures": 0,
  "schema_version": "1.1",
  "vps_host_redacted": true
}
```

`n_market_lookup_failures` is added in schema v1.1 (2026-05-14 fix-up
#2). The expected value on a healthy capture is `0`. Gate 2B's
harness refuses any manifest where
`n_market_lookup_failures / max(n_filled, 1) > 0.05`.

`n_filled >= 100` is the success condition. If `n_filled < 100`, do not
proceed to Gate 2B — investigate the rejection codes in the parquet
first, fix, and re-record.

## 7. Cleanup before walking away

Before disconnecting RDP:

1. In the MT5 terminal, **Trade** tab: confirm **zero open positions**.
   The script's round-trip-close should leave the account flat, but
   verify visually.
2. **Pending orders** tab: confirm zero pending limits/stops. The
   script's cancel-pending step should have cleaned these too; if any
   remain, right-click → Delete.
3. (Optional) capture the FTMO terminal **History** tab as a screenshot
   for your own audit trail — the broker's record of every deal is the
   ground truth if the parquet is ever questioned.
4. Disconnect RDP. The script is already finished; nothing is still
   running on the VPS.

## Cross-references

- Schema validated by `scripts/record_fills.py::FillRecord`
  (pydantic, frozen).
- Pure helpers locked by `tests/scripts/test_record_fills.py` (30 tests).
- Day-1 spike infrastructure (VPS + FTMO demo + secrets file) is
  documented in `docs/runbooks/mt5-spike-runbook.md`; do not duplicate
  that here — this runbook assumes the spike host is intact.
- Gate 2B comparison itself (parquet → divergence analysis) is
  Task 14.3 of the Phase 0 plan.

## 2026-05-14 fix-up — `OrderSendResult.price = 0` lesson (READ BEFORE RE-RECORDING)

The 2026-05-13 ~15h capture (`data/raw/fill_recordings/24e00278d0024a98beb009b75762adb6.parquet`,
110 rows / 107 filled / 3 rejected) landed with `fill_price = 0.0` on
**every** retcode=10009 row and `slippage_observed_pips` in the ±11,700
to ±13,500 range — the bug's signature. Sidecar: `…UNUSABLE.md`.

### Root cause

The pre-fix `parse_fill_into_record` read `fill_price` from
`mt5.OrderSendResult.price` directly. **MT5 returns `result.price = 0`
for market deals in most cases**; the executed fill price lives in the
subsequent deal record, retrieved via `mt5.history_deals_get(...)`.
The existing 18 unit tests passed because their mocked
`OrderSendResult` set `price` to a non-zero value — the
semantically-clean shape, NOT the pathological one a real broker
returns. Bug introduced in commit `450873c` (2026-05-13 "feat(scripts):
Gate 2B fill-recording protocol") and persisted unchanged through
`130ab28`.

### Fix (commit `9dd9af6`, 2026-05-14)

* `_resolve_fill_from_deal` helper: tries `mt5.history_deals_get(ticket=result.deal)`
  first, falls back to `position=result.order` filtered to `DEAL_ENTRY_IN`,
  treats a returned `deal.price == 0` as soft-failure.
* `parse_fill_into_record` now takes `actual_fill_price` /
  `actual_fill_time_utc` keyword-only args (helper stays pure; never
  imports MT5).
* Soft-failure (retcode=10009 but deal lookup returned `None` or
  `price == 0`): the record's `comment` is prefixed
  `retcode_or_deal_failure:` so downstream consumers distinguish a
  broker reject from a successful send with no deal yet.
* Per-iteration `try / except KeyboardInterrupt / except Exception` in
  `main()`: a transient broker error on one bad row no longer kills
  the session (the 2026-05-13 run exited with `LastTaskResult=1` at
  iteration 110/200 — unknown specific cause, but the loop is now
  resilient regardless). Exceptions log to `sys.stderr` with prefix
  `[record_fills:exception]` so Task Scheduler hidden jobs surface
  failures. End-of-loop summary `[record_fills] session complete: …`
  reports `scheduled / attempted / exceptions / exc_types`.
* AST regression test locks the exception-handler contract against
  future broadening to `BaseException` or bare `except:`.

### Short-test capture protocol (do this BEFORE any future 24h run)

The user-mandated gate before relaunching a 24h Task Scheduler run is a
**short-test capture** to verify the fix populates `fill_price`
correctly against the live broker. Working invocation (both flags
confirmed against `python scripts/record_fills.py --help`):

```powershell
python scripts/record_fills.py --duration-hours 1 --n-samples 10
```

Then:

1. Read the first 5 rows of `data/raw/fill_recordings/{new_run_id}.parquet`
   via polars (e.g. `pl.read_parquet(...).head(5)`).
2. Paste the `fill_price` column values back to the orchestrator.
3. **All five values must be non-zero.** If any are zero, the fix did
   not engage on the live broker — STOP and investigate. Do not
   proceed to a 24h run.
4. Once the short-test gate passes, the operator can kick off the 24h
   Task Scheduler run per §1A above.

### Salvage from the 2026-05-13 capture

The bad capture is preserved (not deleted) for partial salvage. Per
the sidecar UNUSABLE.md:

* **VALID** downstream uses (allowed): spread model recalibration
  (W3 follow-up), latency baseline for fill engine, retcode
  distribution under normal conditions.
* **INVALID** uses (blocked): Gate 2B fill comparison. The harness
  `src/propfarm/gates/gate_2b.py::_reject_if_unusable_manifest`
  refuses any capture whose sibling manifest carries
  `"status": "fill_price-unusable"`, with regression test
  `tests/gates/test_gate_2b.py::test_run_gate_2b_rejects_unusable_manifest_status`.

### Cross-links

* Fix commit: `9dd9af6`. Follow-ups: `a91ccbf`, `09bf313`.
* Sidecar: `data/raw/fill_recordings/24e00278d0024a98beb009b75762adb6.UNUSABLE.md`.
* Manifest: same dir, `.json` extension, has `"status": "fill_price-unusable"`.
* Playbook entry: STATUS.md "Pathological-vendor-response catch pattern".

## 2026-05-14 fix-up #2 — `history_select` precondition + market lookup tracking

The fix-v1 (commit `9dd9af6`) added `_resolve_fill_from_deal` calling
`mt5.history_deals_get(ticket=...)` / `(position=...)`. The user ran the
short-test capture (run_id `a68b59a65e384f4d859d3bf257253d75`,
2026-05-14 16:11 UTC, Ctrl-C'd at idx=006 before any flush so no parquet
landed) and observed every market fill come back with `fill_price=NaN`.
MT5 History tab on the VPS confirmed the deals DID execute broker-side
(~17 round-trip deals, real prices like GBPUSD 1.35237 / EURUSD 1.17179,
real ticket numbers, balance change $100,000 → $99,990.89 matching
individual round-trip costs). So orders fired and filled; only the
Python `history_deals_get(...)` lookup returned empty.

Short-test session log: **`short-test-1 FAILED`** — no parquet on disk
(stdout transcript in the dispatch brief is the only artifact).

### Root cause v2

The Python `MetaTrader5` package's `history_deals_get(ticket=...)` and
`(position=...)` overloads silently return `()` on certain MT5 client
builds unless the deal-history cache has been populated for the
relevant time window first. The MetaQuotes Python docs describe the
**date-range overload** `history_deals_get(date_from, date_to)` as the
documented robust path: it "allows receiving all history deals within
a specified period in a single call similar to the HistoryDealsTotal
and HistoryDealSelect tandem" — i.e. it drives the MQL5 `HistorySelect`
step internally. The ticket / position overloads do NOT.

The v1 fix unit tests modelled `history_deals_get(ticket=...)` as
unconditionally returning the pre-staged deal — they did not model the
real-broker behavior where the lookup needs the history cache engaged.
The tests passed; live broker behavior failed.

### Fix v2 (this commit)

* **`_resolve_fill_from_deal` rewrite** — three-path lookup ordering:

  1. `mt5.history_deals_get(ticket=result.deal)` (fast path).
  2. `mt5.history_deals_get(position=result.order)` filtered to
     `DEAL_ENTRY_IN` (legacy v1 fallback).
  3. `mt5.history_deals_get(date_from=request_time-1s,
     date_to=max(now, request_time)+5s)` filtered by symbol + volume +
     side + `DEAL_ENTRY_IN`, closest-time match on multi-candidate.
     This is the documented MetaQuotes overload that engages the
     history cache.

* **Defensive `history_select` precondition** — the helper calls
  `mt5.history_select(date_from, date_to)` iff `hasattr(mt5,
  "history_select")`. The Python `MetaTrader5` documented API does
  not list `history_select` (only the MQL5 server-side function),
  but some MT5 builds expose it. Calling it via `hasattr` is safe on
  all versions and may engage the history cache on builds where it
  exists. On `False` return the helper soft-fails and emits a
  `[record_fills:history_select_failed]` stderr log.

* **Session-scoped claim tracking** — `_resolve_fill_from_deal`
  optionally tracks attributed deal tickets in a session-scoped set
  passed by `main()`. The time-range fallback skips already-claimed
  tickets, preventing double-attribution when two same-symbol
  same-side market orders fire within the ~6-second lookup window.
  Closest-time matching on multi-candidate also emits
  `[record_fills:ambiguous_deal_match]` to stderr.

### Market-vs-pending lookup-failure distinction

The user-mandated behavioral change for fix v2:

* **`order_type == "market"`** with `retcode == 10009` and empty
  lookup after all three paths is an **error condition** — the deal
  MUST exist (broker confirmed the fill). The helper records
  `fill_price=NaN`, the comment is prefixed `retcode_or_deal_failure:`,
  AND `main()` increments a session-scoped `n_market_lookup_failures`
  counter AND emits a `[record_fills:market_lookup_failure] idx=N
  symbol=S order=M side=D request_time=T window=[F,T]` stderr log.

* **`order_type in ("limit", "stop")`** with `retcode == 10009` and
  empty lookup is **expected** — a pending order returns 10009 to
  acknowledge the placement, not a fill. The helper records
  `fill_price=NaN` (the correct expected state for a queued pending
  order) silently, without incrementing the counter or emitting a log.

### Manifest schema bump v1.0 → v1.1

`SessionManifest` (the sidecar JSON) gains a top-level
`n_market_lookup_failures: int` field. `SCHEMA_VERSION` bumped to
`"1.1"`. **`FillRecord` (parquet column) schema is unchanged** — the
parquet column set stays locked at v1.0 column names.

### Gate 2B threshold

`src/propfarm/gates/gate_2b.py::_reject_if_unusable_manifest` gains a
second rejection criterion: if
`n_market_lookup_failures / max(n_filled, 1) > 0.05`, the harness
refuses to run. The 5% threshold is the inclusive tolerance ceiling
(strict-greater-than rejection) — a capture at exactly 5% passes; one
above 5% rejects. Constant: `MAX_MARKET_LOOKUP_FAILURE_RATIO`.

### Stderr log prefixes (operator grep cheat sheet)

| Prefix | Meaning |
|---|---|
| `[record_fills:exception]` | Per-iteration exception (loop continues) |
| `[record_fills:history_select_failed]` | `history_select(date_from, date_to)` returned False |
| `[record_fills:market_lookup_failure]` | Market order succeeded broker-side but Python could not retrieve the deal |
| `[record_fills:ambiguous_deal_match]` | Time-range fallback found >1 candidate matching (symbol, volume, side, entry); closest-time wins |

On the next 24h Task Scheduler run, `findstr "[record_fills:"
stderr.log` surfaces every diagnostic the script emits.

### Short-test gate (do this BEFORE any future 24h run)

Unchanged from fix-v1 — but the success criteria now also include
`n_market_lookup_failures == 0` in the manifest:

```powershell
python scripts\record_fills.py --duration-hours 1 --n-samples 10
```

Then:

1. Read first 5 rows of `data/raw/fill_recordings/{run_id}.parquet`
   via `pl.read_parquet(...).head(5)`. All `fill_price` values must be
   non-zero / non-NaN.
2. Read the manifest at `data/raw/fill_recordings/{run_id}.json`.
   Confirm `n_market_lookup_failures` is `0`.
3. If either gate fails, STOP and re-investigate before any 24h run.

## 2026-05-14 fix-up #3 — server-time offset for `history_deals_get`

The fix v2 short-test capture (run_id
`ef34a234bf1649418d3735c3b930ca8c`, 2026-05-14, Ctrl-C'd before the
first 10-row flush — no parquet on disk; only the stdout transcript
is the artifact) revealed that every market fill triggered
`[record_fills:market_lookup_failure] idx=N symbol=EURUSD order=market
side=buy request_time=2026-05-14T18:04:34.058477+00:00
window=[2026-05-14T18:04:33.058477+00:00,
2026-05-14T18:04:39.204354+00:00]` on stderr — even though the MT5
History tab confirmed the deals materialised broker-side. The
window in UTC corresponds to ~21:04 server time (FTMO MT5 runs
EET/EEST, currently UTC+3); the actual deals live at server-time
~21:04 (= UTC 18:04). The script was querying a UTC window against
a server-time-keyed cache, missing every deal by exactly 3 h.

Short-test session log: **`short-test-2 FAILED`** — no parquet on
disk (only the stdout transcript is the artifact; sibling note to
the failed `a68b59a6…` short-test-1 from fix v2).

### Root cause v3

The MQL5 `HistorySelect` reference page
(`https://www.mql5.com/en/docs/trading/historyselect`) states verbatim
*"Retrieves the history of deals and orders for the specified period
of server time."* The Python `history_deals_get` doc is silent on
timezone semantics — but `HistorySelect` is the MQL5 primitive the
date-range overload drives. The Python `symbol_info_tick` doc shows
`tick.time = 1585070338` (a Unix-second value) without saying which
timezone; the MQL5 `TimeCurrent` doc clarifies *"The time value is
formed on a trade server and does not depend on the time settings on
your computer"* — i.e. tick.time is server-time Unix seconds.

The v2 mocks did not model server-time semantics — `_MockMt5`
compared `date_from.timestamp()` (UTC) against `deal.time` (also
UTC in the mock fixtures). Tests passed; live broker failed.

### Fix v3 (this commit)

* **`detect_server_time_offset_seconds(tick_time_server_unix, utc_now_unix)`**
  helper: rounds `(tick.time - utc) / 1800` to the nearest 30 minutes.
  Most broker timezones are whole hours (FTMO EET/EEST), but several
  real locales use 30-min offsets — India UTC+5:30, Iran UTC+3:30,
  Afghanistan UTC+4:30, parts of Australia UTC+9:30, Newfoundland
  UTC-3:30 — and a force-round to the nearest hour would silently
  miss those captures' history by 30 min. Sub-30-min skew (clock
  drift + broker-side write latency) rounds away.
* **`main()` startup**: after `mt5.initialize()` and before the first
  `order_send`, the script reads `mt5.symbol_info_tick("EURUSD").time`,
  detects the offset, and emits
  `[record_fills:server_time_offset_seconds=N] server_tz_offset_hours=+H`
  to stderr. A non-whole-hour 30-min multiple (e.g. India UTC+5:30 =
  19800s) additionally emits `[record_fills:non_hourly_server_offset_detected]`
  as an INFO line so the operator can confirm the unusual locale is
  deliberate. If `abs(offset) > 43200` (12 h), the script RAISES
  `ValueError` via `validate_server_time_offset_seconds(...)` with a
  message naming VPS clock skew and broker timezone as the canonical
  causes — refusing to record protects the capture from silently
  misclassifying every `broker_fill_time_utc` by an implausible
  amount.
* **`_resolve_fill_from_deal`** gains keyword-only
  `server_time_offset_seconds: int = 0`. Internal datetimes stay UTC;
  translation lives at the MT5 call-site boundary only. The helper
  computes `date_from_unix_server = int(request_time_utc.timestamp())
  - HISTORY_LOOKUP_WINDOW_PAD_BEFORE_SECONDS + offset` and the matching
  `date_to_unix_server`, and passes these ints (not datetimes) to
  `mt5.history_select` and `mt5.history_deals_get(date_from, date_to)`.
  The Python doc explicitly permits int Unix seconds for date params
  ("Set by the 'datetime' object or as a number of seconds elapsed
  since 1970.01.01") so int form is canonical, not a workaround.
* **`deal.time` is server-time Unix**; the helper subtracts the offset
  before constructing a UTC-tz-aware datetime, so the parquet's
  `broker_fill_time_utc` column remains in UTC as documented.

### Mock + test updates

`_MockMt5` gains `server_time_offset_seconds: int = 0`.
`symbol_info_tick(symbol)` returns `Tick(time = int(utc_now + offset),
bid, ask)`. `history_select` and `history_deals_get(date_from,
date_to)` interpret date params as server-time Unix seconds. The mock
accepts either ints or datetimes for back-compat with v1/v2 fixtures.

The mutation regression test
(`test_resolve_fill_from_deal_returns_empty_when_offset_misapplied_signals_market_lookup_failure`)
stages a deal at server-time UTC+3 and calls the helper with
`server_time_offset_seconds=0` (the v2 bug condition). Assertion:
result is `(None, None)` and `[record_fills:market_lookup_failure]`
appears on stderr. The positive control
(`test_resolve_fill_from_deal_engages_when_server_time_offset_applied`)
calls the same helper with `server_time_offset_seconds=10800` and
asserts the fill resolves. Both must pass.

### What running looks like (operator stderr cheat sheet)

After `mt5.initialize()`:

```
[record_fills] connected to server=FTMO-Demo01 login=12345
[record_fills:server_time_offset_seconds=10800] server_tz_offset_hours=+3
```

If the broker is genuinely on UTC (or the VPS clock matches the
broker), the line will read `=0]` with `+0` — that is fine, document
it. The first non-zero `fill_price` value is only meaningful AFTER you
confirm this line.

If `validate_server_time_offset_seconds` raises a `ValueError` with
the "exceeds the sanity bound" message at startup, the VPS clock is
likely misconfigured (or the broker is in an odd timezone). The
script REFUSES to record (hard-fail at startup, no parquet written);
fix the clock or confirm the broker locale before re-running.

If you see `[record_fills:non_hourly_server_offset_detected]`,
confirm the broker IS on a 30-min-offset timezone (India, Iran,
Afghanistan, Newfoundland, etc.). FTMO MT5 is EET/EEST (whole hours)
so this line on an FTMO run almost certainly means VPS clock drift
> ±14m59s — the 30-min granularity narrows the clock-drift tolerance
from the old ±29m59s (under hour-granularity) to ±14m59s. Verify
`w32tm /query /status` reports < 1s drift before running 24h.

### Stderr log prefixes (extended cheat sheet)

| Prefix | Meaning |
|---|---|
| `[record_fills:server_time_offset_seconds=N]` | (fix v3) Detected MT5 server-time offset on session startup. Companion line: `server_tz_offset_hours=+H`. |
| `[record_fills:non_hourly_server_offset_detected]` | (fix v3 reviewer-delta) Offset is a 30-min multiple but NOT a whole hour. Legal for India / Iran / Afghanistan / Newfoundland brokers; **suspect clock drift on an FTMO run**. |
| `[record_fills:server_time_offset_detection_failed]` | (fix v3) `symbol_info_tick` failed or `tick.time` was 0; offset defaults to 0. Investigate before running 24h. |

Pre-`378d1ae` reviewer-delta only: the soft
`[record_fills:server_offset_out_of_range]` warning line is GONE —
its role moved to a hard-fail `ValueError` from
`validate_server_time_offset_seconds(...)` per user mandate.

### Updated short-test gate

After re-running the short test, the user must paste THREE items
(the third was missing from the initial v3 runbook and added on the
reviewer follow-up, since the fix v2 manifest counter is the load-
bearing diagnostic if the offset is wrong):

1. The full `[record_fills:server_time_offset_seconds=N]` line from
   stderr.
2. The first 5 **non-zero** `fill_price` values from
   `data/raw/fill_recordings/{run_id}.parquet`.
3. The `n_market_lookup_failures` value from the sibling JSON
   manifest. **Must be `0`.** A non-zero value means the offset
   translation is not engaging — re-investigate before any 24h run.

The non-zero `fill_price` values are only meaningful if (a) the
offset detection logged a non-zero N matching the broker's real
timezone, OR (b) it logged N=0 with a documented reason (broker on
UTC, VPS clock matches broker).

If `[record_fills:market_lookup_failure]` appears in stderr for any
market row, STOP — the offset translation is not engaging on the
live broker. Re-investigate (likely: the offset detected at startup
doesn't match the offset the date-range overload actually wants;
re-check the doc reading).

### Cross-links

* Fix v3 commits: `1fa8013` (impl) + `ba5f5ec` (hash backfill) +
  `378d1ae` (reviewer-mandated deltas: WARN→RAISE on out-of-range,
  30-min detection granularity, `non_hourly_server_offset_detected`
  INFO line, short-test gate now requires `n_market_lookup_failures=0`).
* Failed short-test session: run_id `ef34a234bf1649418d3735c3b930ca8c`
  (no parquet flushed; stdout transcript only).
* Playbook addendum: STATUS.md "Pathological-vendor-response catch
  pattern → 2026-05-14 addendum #3" — three cumulative learnings
  verbatim.

## 2026-05-14 fix-up #4 — diagnostic probe pass (NOT a fix)

The fix-v3 short-test capture (2026-05-14, run_id discarded — user
Ctrl-C'd before any flush) **still triggered**
`[record_fills:market_lookup_failure]` on every market row, despite
the server-time offset detection working perfectly
(`[record_fills:server_time_offset_seconds=10800] server_tz_offset_hours=+3`,
correct for FTMO EEST). The integer args path 3 passed to
`history_deals_get(date_from=int, date_to=int)` decode back to server
wall-clock `21:56:30..21:56:36` — the same window the MT5 History tab
shows the deal lives in (server-time `21:56:31.976`). The math is
correct; the broker returns empty anyway. **This is a fourth API
contract gotcha** after v1 (`result.price=0`), v2 (`history_select`
precondition), and v3 (server-time semantics).

### Why we are NOT shipping another speculative fix

Three speculative fixes in a row all "passed mocks" and failed the
live broker. The strategic learning is that the mock contract has
not been independently verified against the real MT5 API for
`history_deals_get` / `history_select`. **The right move is
instrumentation that produces concrete evidence**, not another guess.

The reviewer playbook adds a new entry on this cycle: *"If a
fix-cycle has hit the same class of bug ≥ 2 times, halt speculative
fixing and add instrumentation to gather live-broker evidence before
the next attempt."* See `STATUS.md` "Pathological-vendor-response
catch pattern → 2026-05-14 addendum #4."

### What this fix-up changes

* `scripts/record_fills.py` gains
  `emit_market_lookup_failure_probes(...)` plus a module-level
  toggle `EMIT_MARKET_LOOKUP_FAILURE_PROBES: Final[bool] = True`
  (default ON for this diagnostic pass; flip to `False` once fix v4
  lands).
* `main()` invokes the probe block BEFORE
  `emit_market_lookup_failure_log` on every market_lookup_failure
  event. The probe block re-issues `history_deals_get` six different
  ways and logs each return count to stderr; whichever form returns
  `> 0` tells us how the live broker wants to be called.
* The production call form (path 3 in `_resolve_fill_from_deal`) is
  **UNCHANGED**. This pass is GATHER, not ACT.
* `tests/scripts/test_live_broker_validation.py` ships the
  Task-#53 live-broker test (gated on `PROPFARM_LIVE_TEST=1`; refuses
  to run unless `mt5.account_info().server` starts with `FTMO-Demo`).
  The marker `live_broker_validation` is registered in
  `pyproject.toml`.

### Stderr probe block (operator grep cheat sheet)

When a market_lookup_failure fires, the operator sees this block on
stderr **before** the existing `[record_fills:market_lookup_failure]`
line:

```
[record_fills:lookup_probe_args_passed] int_kwargs window_server_unix=[N,M] window_utc_unix=[N-offset,M-offset] offset_seconds=S
[record_fills:lookup_probe_a] int_kwargs_server window=[N,M] returned=K
[record_fills:lookup_probe_b] datetime_naive_server window=[<iso>,<iso>] returned=K
[record_fills:lookup_probe_c] datetime_utc_aware window=[<iso>,<iso>] returned=K
[record_fills:lookup_probe_d] int_kwargs_utc window=[N-offset,M-offset] returned=K
[record_fills:lookup_probe_e] int_kwargs_server_widewindow window=[N-86400,M+86400] returned=K
[record_fills:lookup_probe_f] datetime_naive_server_widewindow window=[<iso>,<iso>] returned=K
[record_fills:market_lookup_failure] idx=N symbol=S order=M side=D request_time=T window=[F,T]
```

| Prefix | What it tests | Expected on live broker |
|---|---|---|
| `[record_fills:lookup_probe_args_passed]` | Sanity-prints the EXACT int args path 3 used + offset, so the operator can confirm the values match path 3's actual call. | Always present. |
| `[record_fills:lookup_probe_a]` (`int_kwargs_server`) | Re-issues the SAME call that just failed. | Should match path 3's behavior (returned=0). |
| `[record_fills:lookup_probe_b]` (`datetime_naive_server`) | Naive datetimes carrying server-local time. MT5's MQL5 heritage may require this form. | **If `returned > 0`, this is the form fix v4 should switch to.** |
| `[record_fills:lookup_probe_c]` (`datetime_utc_aware`) | UTC-aware datetimes (the v2 call form). | Likely 0 (this is what v2 did). |
| `[record_fills:lookup_probe_d]` (`int_kwargs_utc`) | Ints in UTC Unix seconds (the v2 bug condition). | Should be 0. |
| `[record_fills:lookup_probe_e]` (`int_kwargs_server_widewindow`) | Same as probe_a but ±24h. | **If `returned > 0`, the bug is the narrow ±6s window, not the call form.** |
| `[record_fills:lookup_probe_f]` (`datetime_naive_server_widewindow`) | Same as probe_b but ±24h. | Tells us whether the datetime form works at all. |

A probe call that raises an exception logs
`returned=ERROR exc_type=<T> exc_msg=<repr>` on its own line; the
remaining five probes still log their counts (per-probe `try/except`).

### Updated short-test gate

After re-running the short test
(`python scripts/record_fills.py --duration-hours 1 --n-samples 10`),
the user must paste back **FOUR** items now (the fourth is the new
probe block):

1. The full `[record_fills:server_time_offset_seconds=N]` line from
   stderr.
2. The first 5 **non-zero** `fill_price` values from
   `data/raw/fill_recordings/{run_id}.parquet`.
3. The `n_market_lookup_failures` value from the sibling JSON
   manifest. **Expected `> 0`** for this diagnostic pass (the
   probe block fires when this counter increments).
4. **The full probe block** (one
   `[record_fills:lookup_probe_args_passed]` line + the six
   `[record_fills:lookup_probe_*]` lines + the immediately-following
   `[record_fills:market_lookup_failure]` line) from stderr.

The probe block is the load-bearing diagnostic. Whichever probe's
`returned=K` is greater than zero tells us which `history_deals_get`
call form the live broker accepts. Fix v4 will switch the production
call form to that one.

### Live-broker validation test (Task #53)

The companion deliverable: a pytest-marked test that runs against the
real FTMO MT5 demo on the Windows VPS. **Invocation**:

```
PROPFARM_LIVE_TEST=1 pytest tests/scripts/test_live_broker_validation.py
```

The test:

* Refuses to run unless `PROPFARM_LIVE_TEST=1` (skipped by default).
* Refuses to run unless `mt5.account_info().server` starts with
  `FTMO-Demo` (matches the production safety guard).
* Places ONE 0.01-lot EURUSD market buy.
* Drives the result through `_resolve_fill_from_deal` (production
  call site, not a re-implementation).
* Asserts the fill resolves to a real (`> 0`) price within 100 pips
  of the request-time mid.
* Closes the position immediately afterwards so the demo account
  does not accumulate stray trades across repeated runs.

Run this on the VPS only — the MetaTrader5 Python package is
Windows-only. The dev-machine `pytest` deselects the test by default
(both `PROPFARM_LIVE_TEST` unset AND the marker is registered so
`--strict-markers` does not warn).

### Cross-links

* Fix v4 diagnostic commit: see STATUS.md 2026-05-14 #4 session-log
  entry for the hash.
* Probe block implementation: `scripts/record_fills.py` ->
  `emit_market_lookup_failure_probes(...)` +
  `EMIT_MARKET_LOOKUP_FAILURE_PROBES` toggle.
* Probe tests: `tests/scripts/test_record_fills.py` ->
  the `test_emit_market_lookup_failure_probes_*` and
  `test_main_*_probe*` group.
* Playbook addendum: STATUS.md "Pathological-vendor-response catch
  pattern -> 2026-05-14 addendum #4" — the
  "halt-speculative-fixing-at-2-hits" rule.
