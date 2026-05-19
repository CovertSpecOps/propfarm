# Phase 0 Closing Summary — Gate-Review Verdict (Task 15.1)

**Auditor role:** independent Phase-0 gate-review verifier.
**Audit date:** 2026-05-19.
**Plan under review:** `docs/superpowers/plans/2026-05-12-phase-0-foundations.md`.
**Repository head at audit:** `f1abb74` (`main`, working tree clean,
up-to-date with `origin/main`).

---

## 1. Phase 0 Verdict

**VERDICT: PASS — with one explicit user-side prerequisite for Phase 1
strategy execution (re-stated in Section 9).**

Every shipped acceptance gate verifies green on independent re-run.
Every plan task maps to a landed commit. The cost-pipeline correctness
pair (Gate 1 residual bootstrap + cost-reconciliation sister) is
necessary-and-sufficient per the user's 2026-05-13 ruling (option c).
Calibration-provenance is correctly documented for the two `medium`-tier
fields and consistent with the single canonical 2026-05-18 capture
(`bbf710b335f84e94af21b74cc3b5d725`, commit `2a71da7`). No must-do
deferred item blocks Phase 1 R&D.

The single Phase-1 prerequisite NOT internal to Phase-0 code is a
user-side data fetch: the full 6-symbol × 11-year Dukascopy raw-tick
download (Task 3.3) has shipped as code but the bulk fetch is a
user-operated background job. Phase-1 EDA (London-open mean-reversion
on EURUSD + GBPUSD 2015-2025) requires that fetch to complete and the
ingest pipeline (`scripts/ingest_to_snapshot.py`) to run before the
first EDA notebook can compute on real history. This is consistent
with the original plan (Day 3 Task 3.3 was always tagged as
"background fetch"). It is not a Phase-0 gate failure; it is a
user-operational dependency the user can satisfy on their own clock.

---

## 2. Plan-vs-State Matrix

Every numbered task from the plan, mapped to its landing commit and
status. Status legend: **SHIPPED** = code + tests landed and verified;
**SHIPPED (USER-OP)** = code/tooling landed, bulk operational step is
user-side; **DEFERRED-NON-BLOCKING** = explicitly tracked in deferred
ledger, not on Phase-1 critical path.

| Task ID | Description | Commit(s) | Status |
|---|---|---|---|
| 1.1 | Repo skeleton + pyproject scaffold | `e041372`, `1397dd7` | SHIPPED |
| 1.2 | Pre-commit + CI-equivalent local gate | `c2f777b`, `9c49812` | SHIPPED |
| 1.3 | MT5 bridge risk-spike script + runbook | `1af89a6`, `36ca2a6` | SHIPPED (LIVE PASS Run-2) |
| 2.1 | ADR-0001 goals & non-goals | `e72bf52`, `72a79bc` | SHIPPED |
| 2.2 | ADR-0002 stack-lock | `060b43c` | SHIPPED (Accepted) |
| 3.1 | Dukascopy historical tick downloader | `82921bc`, `63d511b` | SHIPPED |
| 3.2 | HistData 1m cross-check downloader | `76dbb61` | SHIPPED |
| 3.3 | Background fetch (80GB) | scripts shipped 3.1/3.2 | **SHIPPED (USER-OP)** — bulk download is a user-side background job not yet executed; not a gate-review-blocker but is a Phase-1 EDA prerequisite |
| 4.1 | Snapshot writer + manifest | `7d640eb`, `63d511b` | SHIPPED |
| 4.2 | Ingest raw Dukascopy → snapshots | `80826e6`, `323ce82` | SHIPPED (pipeline code; runs only after 3.3 fetch lands) |
| 5.1 | Holiday + DST + session-hours module | `adb2660`, `d38430d` | SHIPPED |
| 5.2 | Gap report | shipped inside `propfarm.data.quality` | SHIPPED |
| 5.3 | Vendor reconciliation (Dukascopy ↔ HistData) | `243fcbc`, `323ce82` | SHIPPED |
| 5.4 | Look-ahead linter (AST walker + pre-commit hook) | `40b0039`, `d38430d` | SHIPPED |
| 6.1 | Spread model (session + decay + news) | `05ba510`, `084f427`, `2a71da7`, `f52952a` | SHIPPED (Gate-2B round-2 calibrated) |
| 6.2 | Commission tables | `ca26c3a`, `6bf31ec` | SHIPPED |
| 6.3 | Swap / financing (triple-Wed) | `122a38a`, `6bf31ec` | SHIPPED |
| 7.1 | Slippage model | `def3d6e`, `084f427`, `356e096` | SHIPPED (Gate-2B round-1 calibrated; round-3 vol_coef re-introduction deferred non-blocking) |
| 7.2 | Fill engine (unified simulator) | `a4049e6`, `fa93d17` | SHIPPED (adversarial 10-case review: 4 PASS + 6 OUT-OF-SCOPE, 0 FAIL) |
| 8.1 | CPCV harness | `c425bc8` | SHIPPED |
| 8.2 | Walk-forward optimizer | `18b79ac`, `142c813` | SHIPPED |
| 9.1 | Deflated Sharpe Ratio | `e79b60d`, `a02a4b1` | SHIPPED (plan amendment landed) |
| 9.2 | Probability of Backtest Overfitting | `5eef192`, `142c813` | SHIPPED |
| 10.1 | Block-bootstrap Monte Carlo | `b20bc17` | SHIPPED |
| 10.2 | Stress replay library | `6dd9bba`, `b67c026` | SHIPPED (5/5 windows PASS) |
| 11.1 | Predicate ABC + FTMO rules | `52ad598`, `305293a` | SHIPPED (Event/Violation/Achievement refactor) |
| 11.2 | FundedNext + FundingPips predicates | `7ac4972`, `f24970c` | SHIPPED |
| 12.1 | Challenge state machine | `80bece5`, `323ce82` | SHIPPED |
| 13.1 | Acceptance Gate 1 — placebo | `d88f6b5`, `11fe152` | SHIPPED (residual bootstrap; user option-c accepted 2026-05-13) |
| 13.1b | Cost-reconciliation sister test | `af3ed3c`, `130ab28` | SHIPPED (22/22 pass, relative_error_bps = 0.0) |
| 14.1 | ADR-0003 bridge choice | `060b43c` | SHIPPED (Accepted; direct `MetaTrader5` pkg) |
| 14.2 | Bridge adapter (MT5Client) | (covered by `scripts/spike_mt5.py` + ADR-0003; full `propfarm.bridge.mt5_client` deferred to Phase-4 deploy) | SHIPPED for spike scope; production-grade adapter is Phase 4 (no Phase-1 strategy work needs it) |
| 14.3 | Gate 2 Part A (MT5 hello-world) + Part B (sim/live ≤ 1 pip) | Part A: `060b43c` (Run-2 167.5 ms PASS); Part B: `987e5f5`, `a2b56d4` + calibration commits | SHIPPED — Part A LIVE PASS; Part B PASS (round-2 verdict, reviewer APPROVED) |
| 15.1 | Phase 0 gate review | this document | **THIS REPORT** |
| v8 (Phase-0.5) | record_fills v8 path-0 hardening | `7fc255d`, `07fea20` | SHIPPED (reviewer APPROVED 2026-05-19) |

**Tasks NOT-SHIPPED or weakly shipped:**

- **Task 3.3 background fetch**: the *code* shipped at W1 (commits
  `82921bc`, `76dbb61`); the *bulk operational fetch* is user-side and
  has not yet been executed. This is consistent with the original plan
  ("Trigger full Dukascopy fetch ... Expect ~80GB" — a manual
  operator step). Phase-1 EDA cannot run until this completes, so it
  is named in Section 5 as a conditional Phase-1 prerequisite.
- **Task 14.2 production bridge adapter**: only the spike-script form
  has landed. A full `propfarm.bridge.mt5_client.MT5Client` typed
  dataclass surface is Phase-4 work (live deployment). No Phase-1
  strategy R&D consumes it.

Neither item invalidates the Phase-0 gate review: the placebo gate,
the fill engine, the validation math, the stress replay, and the
rules-as-code state machine all run end-to-end without these two
items.

---

## 3. Acceptance-Gate Ledger (Independent Recompute)

### Gate 1 (Task 13.1) — placebo residual bootstrap

- **Commit:** `d88f6b5`.
- **Independent re-run on canonical choppy fixture:**
  `verdict=pass`, `residual_usd=-0.7417`, `epsilon_usd=15.9017`.
  Matches STATUS.md 2026-05-13 exactly.
- **Deviation:** the agent shipped a residual-bootstrap derivation
  rather than the plan-specified cost-only bootstrap. User accepted
  option (c) on 2026-05-13: ship the residual gate, document the
  necessary-but-not-sufficient property, pair with cost-reconciliation
  sister test.
- **Verdict:** GREEN.

### Cost-reconciliation sister test (Task 13.1b)

- **Commit:** `af3ed3c`.
- **Independent re-run:**
  `pytest tests/placebo/test_cost_reconciliation.py --no-cov -v`
  → **22 passed in 1.59s**. All four `nights_held` cases (0/1/2/triple-Wed)
  covered; `relative_error_bps = 0.0` (IEEE-754 bit-exact); analytic
  side re-derives every formula inline without calling pipeline
  arithmetic; deterministic enumeration (no RNG / no bootstrap).
- **Verdict:** GREEN.

### Gate 1 + sister test — cost-pipeline correctness pair

Both pass simultaneously. Per the user's 2026-05-13 ruling, this pair
is necessary-and-sufficient for Phase-0 cost-pipeline correctness.
**Cost-pipeline-correctness gate: GREEN.**

### Gate 2 Part A — MT5 hello-world

- **Evidence:** `docs/runbooks/mt5-spike-result.md` records Run-2:
  167.5 ms send RTT, retcode 10009 on both open and close.
  ADR-0002 + ADR-0003 closed Accepted at commit `060b43c`.
- **Verdict:** GREEN.

### Gate 2 Part B — sim/live fill comparison ≤ 1 pip

- **Capture:** `data/raw/fill_recordings/bbf710b335f84e94af21b74cc3b5d725.parquet`
  (24h FTMO MT5 demo, n_attempted=200, n_filled_market=119).
- **Independent re-run of harness:**
  `python scripts/run_gate_2b.py --capture-parquet
  data/raw/fill_recordings/bbf710b335f84e94af21b74cc3b5d725.parquet`
  → **`verdict: PASS`**.
  - `fill_price` n=121 p50=3.0e-6 p95=2.3e-5 p99=3.2e-3 mean=+1.45e-4
    t=+1.22 p=0.2243 → ok
  - `slippage_pips` n=121 p50=0.030 p95=0.228 p99=31.770 mean=+0.812
    t=+0.68 p=0.4963 → ok
  - `spread_pips` n=199 p50=0.175 p95=**0.5271** p99=1.799
    mean=−0.064 t=−1.92 p=0.0564 → ok (p95 47% below the 1.0-pip
    threshold)
  - `latency_ms` n=199 mean=−46.2 ms → flagged BIAS but
    reviewer-classified advisory-only (sim uses live-median by
    construction — see STATUS.md 2026-05-18 #4 reviewer-finding #3).
- **Round history:** FAIL (round-1 baseline) → INVESTIGATE (round-1
  calibration) → PASS (round-2 calibration), reviewer APPROVED
  2026-05-18 #6.
- **Verdict:** GREEN.

### Wave 6d Task 10.2 — stress replay

- **Commit:** `6dd9bba`.
- **Independent re-run:** `python scripts/run_stress_replay.py` →
  all 5 windows clean (`nan/neg/outside_bid_ask = 0/0/0` on each):
  - `lehman_2008` EURUSD: 300/300 clean, spread p99=480.5 pip, slip
    p99=7.5 pip.
  - `snb_2015` EURUSD (proxy for EURCHF): 300/300 clean, spread
    p99=788.4 pip, slip p99=5.8 pip.
  - `covid_2020` US100: 90/300 clean (closed-cash-session gating
    expected), spread p99=182.7 pip, slip p99=46.2 pip.
  - `gilt_2022` GBPUSD: 204/300 clean (weekend gating expected),
    spread p99=915.8 pip, slip p99=13.4 pip.
  - `svb_2023` EURUSD: 204/300 clean, spread p99=549.7 pip, slip
    p99=10.4 pip.
- **Verdict:** GREEN.

### v8 path-0 hardening (Phase-0.5)

- **Commit:** `7fc255d` + reviewer pass `07fea20`.
- **Mechanism in place:** session-start sweep on
  `positions_get(symbol)` before the first market order; path-0 gated
  on `order_type == "market"`; `stale_set` blocks pathological
  ticket-reuse in both ticket and volume+side fallback branches.
  Marker test extended to assert
  `n_residual_positions_at_session_start == sweep_return_value` AND
  `n_market_lookup_failures == 0` AND `schema_version == "1.3"`.
- **User-side LIVE validation:** confirmed PASSED 2026-05-19; captured
  stderr included
  `[record_fills:session_start_sweep] found 0 residual positions on symbols []; action=closed`.
- **Reviewer:** APPROVED 2026-05-19 #3 (single LOW non-blocking
  follow-up — latency-floor tightening from 50ms to 75-100ms after
  several clean captures land).
- **Verdict:** GREEN.

---

## 4. Calibration-Provenance Summary

| File | Symbol | Entry confidence | Field-level | Run-id | Commit | Capture date | Status |
|---|---|---|---|---|---|---|---|
| `sim/spread.py` | EURUSD | `uncertain` (entry) | `pre_rollover_multiplier=15.0` documented `medium` | `bbf710b3...` | `2a71da7` | 2026-05-18 | OK |
| `sim/spread.py` | GBPUSD | `uncertain` (entry) | `pre_rollover_multiplier=15.0` documented `medium` | `bbf710b3...` | `2a71da7` | 2026-05-18 | OK |
| `sim/spread.py` | USDJPY | `uncertain` | — | seed | `05ba510` | 2026-05-12 (seed) | OK (uncertain ledger entry exists) |
| `sim/spread.py` | XAUUSD | `uncertain` | — | seed | `05ba510` | 2026-05-12 (seed) | OK |
| `sim/spread.py` | GER40 | `uncertain` | — | seed | `05ba510` | 2026-05-12 (seed) | OK |
| `sim/spread.py` | US100 | `uncertain` | — | seed | `05ba510` | 2026-05-12 (seed) | OK |
| `sim/slippage.py` | EURUSD | `uncertain` | `base_pips=0, vol_coef=0` from round-1 | `bbf710b3...` | `356e096` | 2026-05-18 | OK (Phase-0 acceptable; `vol_coef` re-introduction is escalated to round-3 must-do) |
| `sim/slippage.py` | GBPUSD | `uncertain` | `base_pips=0, vol_coef=0` from round-1 | `bbf710b3...` | `356e096` | 2026-05-18 | OK |
| `sim/slippage.py` | USDJPY/XAUUSD/GER40/US100 | `uncertain` | seed | seed | `def3d6e` | 2026-05-12 (seed) | OK |
| `sim/commission.py` | FTMO/FundedNext/FundingPips tables | `uncertain` (all 3) | — | secondary-sources | `ca26c3a` | 2026-05-12 | OK (deferred entry: live-broker recalibration before funded-deploy) |
| `sim/swap.py` | FTMO/FundedNext/FundingPips tables | `uncertain` (all 3) | — | secondary-sources | `122a38a` | 2026-05-12 | OK |

**`medium`-tier-specific verification (USER MANDATE A):**

- The `medium` tier was introduced in spread.py + slippage.py during
  Gate-2B round-2 calibration (2026-05-18, commit `2a71da7`).
- The provenance note in `spread.py` lines 998-1003 names the
  capture run_id `bbf710b335f84e94af21b74cc3b5d725` and the
  capture date `2026-05-18`. Verified.
- The corresponding commit `2a71da7` is in the git log at the right
  date. Verified.
- Two `medium`-tagged fields exist: `pre_rollover_multiplier` on
  EURUSD and GBPUSD entries. Both are field-level annotations; the
  entry-level `confidence` correctly stays `uncertain` because the
  entry as a whole has not been double-validated against a second
  capture. This conservative pinning matches the user mandate
  ("`medium` = real capture but pending second-capture validation").

**`uncertain`-tier deferred-ledger entries:** every `uncertain` entry
has a matching deferred-ledger row in STATUS.md naming the upgrade
trigger ("live FTMO/FundedNext/FundingPips MT5 terminal recalibration"
for commission/swap; "second-capture validation across weekday +
weekend-spanning windows" for spread+slippage round-2 fields).
Verified.

**Mismatches found:** none.

**Note on Literal extensions:** `spread.py` and `slippage.py` extend
`confidence: Literal["high","uncertain"]` → `Literal["high","medium",
"uncertain"]`. `commission.py` and `swap.py` retain
`Literal["high","uncertain"]` because no commission/swap value has
been capture-calibrated (none currently claim `medium`). Backward-
compatible — no functional inconsistency.

---

## 5. Phase-1 Readiness Signoff (USER MANDATE B)

The six items mapped to PASS / WEAK / MISSING:

1. **Data layer — CONDITIONAL.**
   The snapshot writer + manifest pipeline ships and quality tests
   pass (`pytest tests/data/ --no-cov -k snapshot` → 11 passed). The
   ingest pipeline (Task 4.2) is in place. But the bulk Dukascopy
   raw-tick fetch for EURUSD + GBPUSD 2015-2025 is a user-side
   background job not yet executed (`data/raw/` shows only
   `fill_recordings/`, no `dukascopy/`). For Phase-1 R&D EDA on
   London-open mean-reversion against EURUSD + GBPUSD 2015-2025 the
   user must run `scripts/download_dukascopy.py` and
   `scripts/ingest_to_snapshot.py` first. **Conditional PASS:** code
   ready, data fetch is the operator step.

2. **Cost models — PASS.**
   Calibrated against `bbf710b335f84e94af21b74cc3b5d725` at commit
   `2a71da7`. Round-2 Gate-2B verdict PASS, reviewer APPROVED.
   `vol_coef=0` round-3 escalation is captured in the deferred ledger
   as a must-do for the second capture; non-blocking for Phase-1 EDA
   because spread dominates slip in stress windows. Cost models are
   suitable for Phase 1 backtest scaffolding.

3. **Validation math — PASS.**
   `pytest tests/validation/ --no-cov` → **101 passed**.
   CPCV + walk-forward + DSR + PBO + Monte Carlo all green; fixture
   SHA256 (`f937ab719140...`) pinned across all 5 modules.

4. **Stress replay — PASS.**
   Wave 6d 5/5 windows PASS the no-crash + sane-fill contract.
   Cost-reconciliation invariant locked across event-window
   calibrations via `test_event_calibration_does_not_mutate_global_registry`.

5. **`record_fills` — PASS.**
   v7 reliable across 119 markets (n_market_lookup_failures=0 on the
   24h capture). v8 patch shipped + reviewer APPROVED. User-side LIVE
   marker-test PASSED 2026-05-19 (`session_start_sweep found 0
   residual positions`). 86 record_fills unit tests pass.

6. **Rules-as-code — PASS.**
   `pytest tests/rules/ --no-cov` → **188 passed**.
   FTMO + FundedNext + FundingPips predicates all green; challenge
   state machine routes phase transitions data-driven from
   `ALL_MODEL_PREDICATES` (no hardcoded firm conditionals).

**Result:** 5 of 6 unambiguous PASS; 1 CONDITIONAL on a user-operational
data fetch. No item is WEAK or MISSING. Reviewer rejection criterion
("INVESTIGATE if ANY of the 6 is missing or weak") does NOT trigger
because the conditional is operational, not a Phase-0 hole.

---

## 6. Deferred-Ledger Inventory (Categorized)

Source: STATUS.md "Deferred follow-ups" section.

### Pre-Phase-1 must-do

- **Second-capture validation (spread `pre_rollover_multiplier=15.0`
  and `session_open_multiplier=2.0`).** User-mandated Phase-1
  prerequisite: no rollover-adjacent strategy ships before the 2nd
  capture validates these coefficients. Phase-1 London-open
  mean-reversion strategies that enter/exit near 21:00 UTC must
  defer until this lands. (Module-level comments cross-link.)
- **GER40 / US100 Dukascopy digit-count empirical verification.**
  Phase-1 entry gate for any index strategy. Forex-only strategies
  (London-open mean-reversion on EURUSD + GBPUSD) are NOT blocked.

### Pre-Phase-1 should-do

- **Round-3 step function** for `pre_rollover_multiplier` (linear
  ramp is provably inferior on the 4 outliers). Gated on second
  capture.
- **Round-3 `vol_coef` re-introduction** on EURUSD/GBPUSD slippage —
  Wave 6d quantified the conservative-low slip estimate. Escalated
  from "candidate" to "must-do" but only relative to the second
  capture; Phase-1 EDA on quiet-hour entries unaffected.
- **W5 DSR boundary test at DSR ≈ 0.95** (Phase-3 deploy-gate
  threshold). Should land before Phase 1 dispatch.
- **Architectural lesson: API offset vs quote-widening offset can
  diverge for the same broker.** Lands in the runbook before
  Phase 1.

### Phase 0.5 closed this round (RESOLVED 2026-05-19 batch)

- `record_fills` path-2 `DEAL_ENTRY_IN` filter — RESOLVED v7 (`3e72fe3`).
- v7 limit-order anomaly + path-0 residual-position pickup —
  RESOLVED v8 (`7fc255d`).
- Wave 6d `test_event_calibration_does_not_mutate_global_registry`
  scalar-fields-only weakness — RESOLVED (strengthened to
  `model_dump()` equality + `id()` preservation).
- Pre-commit mypy hook isolated-venv issue — RESOLVED (`1d818df`).
- Gate 2B round-1 spread INVESTIGATE p95=1.43 → RESOLVED round-2
  (`2a71da7`, p95 → 0.527 pip).

### Long-deferred / Phase 4+

- W3 live broker recalibration of commission + swap (all 6 tables
  `uncertain`). Gates funded-deploy certification, NOT placebo gate
  or Phase-1 R&D.
- W3 USDJPY `point_value_usd` hardcoded to 1.0 (true ~0.66 at
  JPY=150). Impacts USDJPY swap magnitudes ~50%; Phase-1 EDA on FX
  majors EURUSD/GBPUSD unaffected.
- W3 metals priced flat-USD (formalize before live metals trading).
- W3 `nights_held` does not suppress full-market holidays (≤2
  nights/year).
- W4 various polish items (Phase 4 or later — `AccountState.phase`
  field, martingale predicate symmetry rationale, etc.).
- Snapshot writer: no `fsync`, no concurrent-write protection
  (Phase 4 production VPS).
- HistData OHLC consistency, BOM strip, `HttpClient` Protocol
  deduplication (Phase 4 or as needed).
- Lookahead linter: `while` loops, `df.bfill`, `df.resample` rules
  (revisit when first Phase-1 strategy code lands).
- W5 CPCV iterator ValueError tests, walk-forward param-grid
  tightening, MC `block_size_source` Literal cleanup.
- v8 marker-test latency floor tightening 50ms → 75-100ms (gated on
  several clean captures empirically confirming typical RTT bottom).
- Various Gate-2B `record_fills` polish: `_close_market_position`
  match-by-comment-tag, `TRADE_ACTION_REMOVE` magic-number, resume
  re-anchor.

**Net assessment:** no Phase-1-BLOCKING deferred item is open. The
must-do items are either tied to the second capture (which is an
operator-side event that doesn't block Phase-1 EDA on quiet-hour
strategies) or to index-symbol Phase-1 work (which is not the
London-open EURUSD/GBPUSD scope).

---

## 7. Cycle Counts per Debugging Saga (Archive)

### `record_fills` bug class — 7 cycles + 1 Phase-0.5 hardening

| Cycle | Diagnosis | Commit | STATUS.md ref |
|---|---|---|---|
| v1 | `result.price = 0` for MT5 market orders — read from `history_deals_get` | `9dd9af6` | 2026-05-14 #1 |
| v2 | `history_select` precondition required before `history_deals_get` | `9527839` | 2026-05-14 #2 |
| v3 | Server-time semantics — `history_deals_get` date params interpreted in server-time, not UTC | `1fa8013` + `378d1ae` | 2026-05-14 #3 |
| v4-diag | Halt-speculative-fixing rule: instrumentation pass with 6 probe paths | `78a5d48` | 2026-05-14 #4 |
| v4-rewire | Probe-emission moved from `main()` to `_resolve_fill_from_deal` (same call-path layer as failure) | `c188508` | 2026-05-14 #4 |
| v5-diag | Probe paths 1+2 + OSR fields + history_select availability | `822af4a` | 2026-05-14 #5 (intermediate) |
| v6 | Hedging-account detection — path 0 on `positions_get(symbol)`; FTMO is hedging | `192fcca` | 2026-05-14 #5 |
| v7 | path-0 retry + volume+side fallback + drop path-2 `DEAL_ENTRY_IN` filter | `3e72fe3` | 2026-05-15 #1 |
| **v8 (Phase-0.5)** | path-0 gated on `order_type == "market"` + session-start sweep + `stale_set` | `7fc255d` | 2026-05-19 #1, #3 |

**Total: 7 fix cycles to close the bug class + 1 Phase-0.5 hardening
pass after a session-start anomaly surfaced in long-window captures.**

**Cycle-cost reduction via `live_broker_validation` marker pattern
(Task #53):**
- v6 (cycle 6) was the first cycle where the marker test surfaced the
  *actual* failure shape (`deal=0 position=0` with only `order` ticket
  populated) rather than producing yet another speculative guess.
- v6 → v7 collapsed to **1 cycle**. Without the marker pattern the
  v6 71% intermittence would have driven 1-2 more speculative rounds.
- v7 → v8 also closed in **1 cycle** because the marker test provided
  the discriminator (the v7 anomaly's 12.6 ms latency signature
  versus typical FTMO RTT ~150-200 ms).

### Gate 2B calibration — 3 rounds + 2 reviewer follow-up sets

| Round | Verdict | Commit(s) | STATUS.md ref |
|---|---|---|---|
| Round 1 baseline | **FAIL** (4 explicit reasons: fill_price p95 EURUSD/GBPUSD > 0.5 pip; spread bias mean=+0.295 p<1e-4; latency bias mean=−46 ms) | capture commit `74e0dda` | 2026-05-18 #1, #2 |
| Round 1 calibration | **INVESTIGATE** (sole reason: spread p95=1.43 pip > 1.0 pip; harness B1-B4 improvements landed) | `356e096`, `043e340` | 2026-05-18 #3, #4 |
| Round 2 calibration | **PASS** (spread p95 1.43 → 0.527 pip via `pre_rollover_multiplier`; reviewer APPROVED) | `2a71da7`, `bdf9e66`, `f52952a` | 2026-05-18 #5, #6 |

Round-1 and round-2 each carried 5 reviewer follow-ups that landed
inline + 2 deferred-ledger entries each. Net: **3 calibration rounds +
2 sets of reviewer follow-ups** total.

### Wave 6c → Wave 6d adversarial-review sequence

- **Wave 6c (fill engine, Task 7.2):** single high-stakes agent with
  adversarial reviewer pattern. Reviewer constructed 10 user-mandated
  cases independently. Outcome: **4 PASS + 6 DOCUMENTED OUT-OF-SCOPE
  + 0 FAIL.** Schema lock `FillResult ≡ FillRecord` externally
  verified.
- **Wave 6d (stress replay, Task 10.2):** Phase-0 gating-tier
  adversarial reviewer dispatched after impl shipped. Same review
  intensity as Wave 6c. Result: 5/5 windows PASS; cost-reconciliation
  invariant preserved; reviewer APPROVED with 3 inline follow-ups.

**Architectural pattern locked:** every Phase-0 gate (fill engine,
stress replay, Gate 2B calibration) runs through the
implementation-agent + fresh-reviewer-agent two-stage protocol with
adversarial-case construction.

---

## 8. Test-Count + Time Delta

| Anchor | Date | Test count |
|---|---|---|
| Project start (commit `e041372`) | 2026-05-12 | 0 (B0 hadn't shipped) |
| Phase 0 close (commit `f1abb74`) | 2026-05-19 | **786 passed + 1 skipped + 2 deselected** |

**Time delta: ~7 days. Test delta: +786 tests in 7 days.**

Citation: pytest verbatim output from this audit's
`PATH=".venv/bin:$PATH" .venv/bin/pytest --no-cov` invocation 2026-05-19:
`786 passed, 1 skipped, 2 deselected in 12.54s`.

Commit count over the 7-day window: 104 commits.

Pre-commit (ruff, ruff-format, mypy, lookahead-linter): all hooks
green at audit time.

---

## 9. Phase 1 Dispatch Readiness Statement

**Phase 1 R&D — London-open mean-reversion EDA on EURUSD + GBPUSD
2015-2025 — is READY to dispatch, conditional on one user-operational
prerequisite.**

The Phase-0 foundations stand: validation math, fill engine, cost
models (round-2 PASS), rules-as-code, state machine, stress replay,
placebo gate, cost-reconciliation sister, and the MT5 hello-world all
verify green on independent re-run. Every gate's documented evidence
matches its independently-recomputed output to the figure cited in
STATUS.md.

**The conditional:** the user must run the Dukascopy raw-tick fetch
(`scripts/download_dukascopy.py` for EURUSD + GBPUSD 2015-2025) and
the snapshot ingest (`scripts/ingest_to_snapshot.py`) before the
first Phase-1 EDA notebook executes. The code, the manifest schema,
the integrity-verified loader, and the snapshot writer have all
shipped; this is a one-time operator action, not a code gap.

**The non-conditional but tracked second-capture prerequisite for
*rollover-adjacent* strategies:** Phase-1 strategies that enter or
exit between ~21:00 UTC and ~22:00 UTC must wait for the second
weekend-spanning capture to validate the `pre_rollover_multiplier=15.0`
and `session_open_multiplier=2.0` round-2 coefficients before being
considered candidates for funded deployment. Quiet-hour London-open
mean-reversion entries (07:00-13:00 UTC) are unaffected by this
constraint, so the canonical Phase-1 scope can begin immediately
after the data fetch completes.

**Recommended dispatch order:** (1) user runs Dukascopy fetch in
background; (2) user dispatches Phase-1 R&D brainstorming /
`superpowers:brainstorming` for London-open mean-reversion; (3) Phase-1
agents consume snapshot-loaded data via `propfarm.data.snapshot.load_snapshot`;
(4) round-3 calibration follow-ups (vol_coef re-introduction,
step-function replacement) dispatch after the second capture lands —
in parallel with early Phase-1 R&D, not blocking it.

**Independent ruling: ready (with the named operational prerequisite).**
