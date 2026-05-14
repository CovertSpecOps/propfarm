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

### Parquet schema (v1.0)

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
  "schema_version": "1.0",
  "vps_host_redacted": true
}
```

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
