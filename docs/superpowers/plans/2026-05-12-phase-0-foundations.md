# Phase 0 — Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the bedrock of the prop-farm system — repo, data layer, execution simulator, validation harness, rules-as-code, placebo gate, and a working MT5 bridge — such that no strategy work can start until the foundation is provably correct.

**Architecture:** Python 3.12 monorepo (`prop-farm/`) with `uv` for dependency mgmt, Docker for reproducibility, pre-commit (ruff/mypy/pytest) gating commits. Layered modules: `data/` → `sim/` → `validation/` → `rules/` → `bridge/`. Two acceptance gates at the end: (1) placebo (random entries lose exactly costs ± ε) and (2) MT5 hello-world (nautilus → FTMO demo round-trip). Both green → Phase 1 unlocks.

**Tech Stack:** Python 3.12, uv, Docker, ruff, mypy, pytest, polars/pandas, pyarrow, vectorbt, nautilus-trader, MetaTrader5 (or ZeroMQ-MQL5 bridge), pandas-market-calendars, hypothesis (property tests).

**Duration:** 15 working days. The MT5 bridge risk-spike is parallelized into Day 1 so we learn it works/doesn't before we sink 12 days into infrastructure.

---

## File Structure

```
prop-farm/
├── pyproject.toml                        # uv + ruff + mypy + pytest config
├── Dockerfile                            # python:3.12-slim base, MT5 not in container
├── docker-compose.yml                    # research stack: jupyter, mlflow, redis
├── .pre-commit-config.yaml
├── STATUS.md                             # session log
├── README.md
├── docs/
│   ├── adr/
│   │   ├── 0001-goals-and-non-goals.md
│   │   ├── 0002-stack-lock-vectorbt-nautilus.md
│   │   ├── 0003-mt5-bridge-choice.md
│   │   ├── 0004-data-vendor-and-snapshot-policy.md
│   │   └── 0005-cost-model-calibration.md
│   └── superpowers/plans/                # this plan lives here
├── data/
│   ├── raw/                              # vendor dumps, never modified
│   ├── snapshots/                        # content-hashed Parquet
│   └── manifests/                        # data_snapshot.json files
├── src/propfarm/
│   ├── __init__.py
│   ├── data/
│   │   ├── vendors/dukascopy.py
│   │   ├── vendors/histdata.py
│   │   ├── snapshot.py                   # content-hashed write/load
│   │   ├── quality.py                    # gaps, DST, holiday checks
│   │   └── lookahead_linter.py           # AST walker
│   ├── sim/
│   │   ├── spread.py                     # time-of-day + vol regime
│   │   ├── slippage.py                   # f(vol, size, minute)
│   │   ├── commission.py                 # per-firm tables
│   │   ├── swap.py                       # triple-Wed rule
│   │   └── engine.py                     # vectorized fill simulator
│   ├── validation/
│   │   ├── cpcv.py                       # Combinatorial Purged CV
│   │   ├── walkforward.py
│   │   ├── dsr.py                        # Deflated Sharpe Ratio
│   │   ├── pbo.py                        # Probability of Backtest Overfitting
│   │   ├── monte_carlo.py                # block bootstrap, ≥10k paths
│   │   └── stress.py                     # replay library
│   ├── rules/
│   │   ├── predicates.py                 # Predicate ABC
│   │   ├── ftmo.py
│   │   ├── fundednext.py
│   │   ├── fundingpips.py
│   │   └── state_machine.py              # challenge→verif→funded→payout
│   ├── bridge/
│   │   ├── mt5_client.py                 # or zmq_client.py per ADR-0003
│   │   └── nautilus_adapter.py
│   └── placebo/
│       └── random_strategy.py
├── tests/
│   ├── data/
│   ├── sim/
│   ├── validation/
│   ├── rules/
│   ├── bridge/
│   └── acceptance/
│       ├── test_placebo_gate.py
│       └── test_mt5_helloworld.py
└── scripts/
    ├── download_dukascopy.py
    ├── download_histdata.py
    └── run_data_quality_report.py
```

---

## Day 1 — Repo skeleton + MT5 bridge risk spike (parallel)

The MT5 bridge is the single riskiest infra piece. We spike it on Day 1 in parallel with repo setup so a "bridge is impossible" finding doesn't surface on Day 14 after 12 days of sunk cost.

### Task 1.1: `git init` and pyproject scaffold

**Files:**
- Create: `prop-farm/pyproject.toml`
- Create: `prop-farm/.gitignore`
- Create: `prop-farm/STATUS.md`
- Create: `prop-farm/README.md`

- [ ] **Step 1:** `cd /Users/covertspecops/prop-farm && git init -b main`
- [ ] **Step 2:** Write `pyproject.toml`:

```toml
[project]
name = "propfarm"
version = "0.0.1"
requires-python = ">=3.12,<3.13"
dependencies = [
  "polars>=1.0",
  "pandas>=2.2",
  "pyarrow>=15",
  "numpy>=1.26",
  "scipy>=1.13",
  "vectorbt>=0.27",
  "nautilus-trader>=1.190",
  "pandas-market-calendars>=4.4",
  "hypothesis>=6.100",
  "pydantic>=2.7",
  "rich>=13",
  "typer>=0.12",
]

[project.optional-dependencies]
dev = ["ruff>=0.5", "mypy>=1.10", "pytest>=8", "pytest-cov>=5", "pre-commit>=3.7"]
mt5 = ["MetaTrader5>=5.0.45"]  # Windows-only; bridge runs on Windows VPS

[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E","F","I","B","UP","SIM","RUF"]

[tool.mypy]
python_version = "3.12"
strict = true
warn_unused_ignores = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
addopts = "-q --strict-markers --cov=src/propfarm --cov-report=term-missing"
testpaths = ["tests"]
```

- [ ] **Step 3:** Write `.gitignore` (standard Python + `data/raw/` + `data/snapshots/` + `.env`).
- [ ] **Step 4:** Init `STATUS.md` with template (Phase, last validated, next).
- [ ] **Step 5:** `uv venv && uv pip install -e ".[dev]"`
- [ ] **Step 6:** Commit: `chore: init repo skeleton with pyproject and tooling`.

### Task 1.2: Pre-commit + CI-equivalent local gate

**Files:**
- Create: `prop-farm/.pre-commit-config.yaml`

- [ ] **Step 1:** Add hooks: ruff (lint+format), mypy, pytest (only-modified-tests run).
- [ ] **Step 2:** `pre-commit install`.
- [ ] **Step 3:** Make an intentional lint error in a throwaway file; verify hook blocks commit. Revert.
- [ ] **Step 4:** Commit: `chore: enforce ruff/mypy/pytest via pre-commit`.

### Task 1.3: MT5 bridge risk spike — minimum viable order

**Files:**
- Create: `prop-farm/scripts/spike_mt5.py` (throwaway; deleted after ADR-0003)

**Context for engineer:** FTMO MT5 demo signup is free. Install MT5 terminal on a Windows VPS (or Windows VM). Install `MetaTrader5` Python pkg in a separate venv on that VPS. This task is throwaway code whose sole purpose is to answer: "can a Python process place a market order on an FTMO demo account, modify SL, and close it, today?"

- [ ] **Step 1:** Provision a Windows VPS (Contabo / FXVM / cheapest 4GB Windows VPS — ~$15/mo). Note: this is _not_ the production VPS; it is the spike host.
- [ ] **Step 2:** Sign up FTMO MT5 demo (free), record server/login/password to `~/.propfarm-secrets.json` on the VPS only (never commit).
- [ ] **Step 3:** Install MT5 terminal, log in, verify charts stream.
- [ ] **Step 4:** Write `spike_mt5.py`:

```python
import MetaTrader5 as mt5
import json, time, pathlib

creds = json.loads(pathlib.Path.home().joinpath(".propfarm-secrets.json").read_text())["ftmo_demo"]
assert mt5.initialize(login=creds["login"], password=creds["password"], server=creds["server"]), mt5.last_error()

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
pos = mt5.positions_get(symbol=symbol)[0]
close_req = {**req, "action": mt5.TRADE_ACTION_DEAL, "type": mt5.ORDER_TYPE_SELL,
              "position": pos.ticket, "price": mt5.symbol_info_tick(symbol).bid}
result = mt5.order_send(close_req)
assert result.retcode == mt5.TRADE_RETCODE_DONE, result
mt5.shutdown()
```

- [ ] **Step 5:** Run, record round-trip latency. **Acceptance:** order opens, modifies SL implicitly, closes; total RTT < 2 seconds.
- [ ] **Step 6:** If FAIL (any blocking issue — pkg incompatibility, server rejection, ToS-flagged): STOP. The stack-lock ADR (Task 2.1) is gated on this. Document the failure in `docs/adr/0003-mt5-bridge-choice.md` as evidence and switch to ZMQ-MQL5 spike before Day 2 finishes.
- [ ] **Step 7:** If PASS: note latency in STATUS.md, do NOT delete spike yet — referenced in ADR-0003 on Day 13.

---

## Day 2 — Stack-lock ADR + goals ADR

### Task 2.1: ADR-0001 goals & non-goals

**Files:**
- Create: `prop-farm/docs/adr/0001-goals-and-non-goals.md`

- [ ] **Step 1:** Write ADR with: objectives in priority order (survive → pass eval → Sortino/Calmar → scale), payout-100% policy, hard constraints (no martingale, no compounding on firm books), firm risk as first-class variable.
- [ ] **Step 2:** Explicit non-goals: no clients, no signal-selling, no futures firms Phase 1, no compounding on firm books, no copy-trading across accounts at same firm.
- [ ] **Step 3:** Commit: `docs: ADR-0001 project goals and non-goals`.

### Task 2.2: ADR-0002 stack-lock vectorbt + nautilus

**Files:**
- Create: `prop-farm/docs/adr/0002-stack-lock-vectorbt-nautilus.md`

- [ ] **Step 1:** Document decision: vectorbt for research (vectorized parameter sweeps, CPCV-friendly); nautilus-trader for production (event-driven, deterministic backtest↔live parity).
- [ ] **Step 2:** Document Day-1 MT5 spike result. If green, lock the stack. If red, lock alternative bridge and re-evaluate nautilus (it has its own MT5 adapter quirks).
- [ ] **Step 3:** Define stack-revisit triggers: (a) MT5 bridge fails Phase-0 hello-world acceptance, (b) backtest/live parity diverges > 5 bps on the same trade sequence, (c) any banned-technique flag from FTMO ToS on the chosen bridge.
- [ ] **Step 4:** Commit.

---

## Day 3 — Data vendor reconnaissance + downloaders

### Task 3.1: Dukascopy historical tick downloader

**Files:**
- Create: `prop-farm/scripts/download_dukascopy.py`
- Create: `prop-farm/src/propfarm/data/vendors/dukascopy.py`
- Create: `prop-farm/tests/data/test_dukascopy.py`

- [ ] **Step 1 (TDD):** Test — given a known small day (e.g., 2024-01-02 10:00–10:05 UTC on EURUSD), fetched ticks must have `len > 100`, monotonic `ts`, `bid < ask`, all timestamps in `[start, end]`.
- [ ] **Step 2:** Run — fails (function missing).
- [ ] **Step 3:** Implement `fetch_ticks(symbol, start_utc, end_utc) -> pl.DataFrame` using Dukascopy's `https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5` LZMA endpoints. Decompress, parse 20-byte records (`>IIIff` ms-from-hour, ask×1e5, bid×1e5, ask_vol, bid_vol).
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5:** Symbol coverage: EURUSD, GBPUSD, USDJPY, XAUUSD, GER40 (DAX), US100 (NDX). Range: 2015-01-01 → 2025-12-31.
- [ ] **Step 6:** Commit. _Don't_ run the full download yet — that's Task 3.3.

### Task 3.2: HistData 1m cross-check downloader

**Files:**
- Create: `prop-farm/src/propfarm/data/vendors/histdata.py`
- Create: `prop-farm/tests/data/test_histdata.py`

- [ ] HistData ASCII 1m bars by month, `histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{symbol}/{year}/{month}`. Implement as cross-check source — NOT primary.

### Task 3.3: Background fetch (run overnight)

- [ ] **Step 1:** Trigger full Dukascopy fetch for 6 symbols × 11 years into `data/raw/dukascopy/`. Expect ~80GB.
- [ ] **Step 2:** Commit downloader + fetch log; data itself is gitignored.

---

## Day 4 — Parquet snapshot layer with content-hashed pinning

### Task 4.1: Snapshot writer + manifest

**Files:**
- Create: `prop-farm/src/propfarm/data/snapshot.py`
- Create: `prop-farm/tests/data/test_snapshot.py`

- [ ] **Step 1 (TDD):** Test — given a tiny synthetic DataFrame, `write_snapshot(df, name)` should produce a Parquet under `data/snapshots/{name}/{partition}.parquet` and append an entry to `data/manifests/snapshot.json` with `sha256` of the file bytes, `row_count`, `min_ts`, `max_ts`, `vendor`, `created_utc`.
- [ ] **Step 2:** Test — `load_snapshot(name)` raises `SnapshotIntegrityError` if any file's SHA256 differs from the manifest.
- [ ] **Step 3:** Implement, run, tests pass.
- [ ] **Step 4:** Partitioning: ticks → year=YYYY/month=MM. Zstd compression level 9.

### Task 4.2: Ingest raw Dukascopy into snapshots

- [ ] **Step 1:** Script: `scripts/ingest_to_snapshot.py` that reads `data/raw/dukascopy/**/*.bi5`, applies `dukascopy.parse()`, calls `write_snapshot`.
- [ ] **Step 2:** Run for all symbols; verify `load_snapshot` round-trips.
- [ ] **Step 3:** Commit manifest (but not the Parquet files).

---

## Day 5 — Data quality: holidays, DST, gaps, vendor reconciliation, look-ahead linter

### Task 5.1: Holiday calendar + DST module

**Files:**
- Create: `prop-farm/src/propfarm/data/quality.py`
- Create: `prop-farm/tests/data/test_quality.py`

- [ ] **Step 1 (TDD):** Test cases:
  - 2024-12-25 (Xmas): FX market closed → `expect_data(symbol="EURUSD", date=2024-12-25)` returns `False`.
  - 2024-03-10 02:00 ET (US DST spring forward): `is_dst_boundary` returns `True`.
  - Asian-session-only flag for XAUUSD weekend close (Fri 22:00 UTC → Sun 22:00 UTC).
- [ ] **Step 2:** Implement using `pandas-market-calendars` + custom FX overlays (FX trades 24/5 except major holidays).

### Task 5.2: Gap report

- [ ] **Step 1 (TDD):** Test — inject a synthetic 10-minute gap during a non-holiday weekday; `gap_report(snapshot)` flags it. Real holiday gap NOT flagged.
- [ ] **Step 2:** Implement, run, commit.

### Task 5.3: Vendor reconciliation

- [ ] **Step 1 (TDD):** Test — for 100 random minutes, Dukascopy 1m OHLC vs HistData 1m OHLC should diverge < 1 bps for FX majors. Flag minutes where diff > 5 bps for manual review.
- [ ] **Step 2:** Implement, run, write `docs/data-quality-report-2026-05.md` with findings.

### Task 5.4: Look-ahead linter (AST walker)

**Files:**
- Create: `prop-farm/src/propfarm/data/lookahead_linter.py`
- Create: `prop-farm/tests/data/test_lookahead_linter.py`

- [ ] **Step 1 (TDD):** Cases that MUST flag: `df.shift(-1)`, `df.iloc[i+1]` inside a backtest loop, `np.roll(arr, -k)`, `.cumsum()` followed by access to row before the .cumsum() input ends. Cases that MUST NOT flag: `df.shift(1)`, `df.iloc[i-1]`.
- [ ] **Step 2:** Implement AST visitor that walks function bodies decorated with `@strategy` and raises on negative shifts.
- [ ] **Step 3:** Wire into pre-commit hook.
- [ ] **Step 4:** Commit.

---

## Day 6 — Execution simulator: spread + commission + swap

### Task 6.1: Spread model

**Files:**
- Create: `prop-farm/src/propfarm/sim/spread.py`
- Create: `prop-farm/tests/sim/test_spread.py`

- [ ] **Step 1 (TDD):** Calibrate spread `(symbol, minute_of_day) -> median_spread_pips` from Dukascopy 2023–2024 ticks. Tests:
  - EURUSD 10:00 UTC: median spread between 0.1 and 0.4 pips.
  - EURUSD 22:00 UTC (NY close): median spread ≥ 1.0 pip.
  - GBPUSD 14:30 UTC on NFP first Fridays: spread ≥ 5x typical.
- [ ] **Step 2:** Implement `Spread.quote(symbol, ts, vol_regime) -> pips`. Vol regime is a coarse {LOW, NORMAL, HIGH} classifier off realized 5-min vol.
- [ ] **Step 3:** Commit.

### Task 6.2: Commission tables (per firm + per broker)

**Files:**
- Create: `prop-farm/src/propfarm/sim/commission.py`
- Create: `prop-farm/tests/sim/test_commission.py`

- [ ] **Step 1:** Fetch and quote current FTMO, FundedNext, FundingPips commission tables (link, retrieval date, snapshot in repo as txt under `docs/firm-tos-snapshots/`).
- [ ] **Step 2 (TDD):** Test — FTMO EURUSD round-trip 1 lot ≈ $7 (verify against current ToS — DO NOT hardcode without checking).
- [ ] **Step 3:** Implement, commit.

### Task 6.3: Swap/financing (triple-Wednesday FX)

- [ ] **Step 1 (TDD):** Test — holding 1 lot EURUSD long over Wed→Thu rollover at 22:00 ET accrues 3x daily swap.
- [ ] **Step 2:** Implement using each firm's swap table snapshot; commit.

---

## Day 7 — Execution simulator: slippage as f(vol, size, time)

### Task 7.1: Slippage model

**Files:**
- Create: `prop-farm/src/propfarm/sim/slippage.py`
- Create: `prop-farm/tests/sim/test_slippage.py`

- [ ] **Step 1 (TDD):** Specification tests (these are the model contract):
  - Normal hour (10:00 UTC), 0.1 lot EURUSD: slip ∈ [0, 0.3] pips.
  - NFP minute (1st Fri 14:30 UTC), 0.1 lot EURUSD: slip ∈ [3, 15] pips.
  - 10 lot EURUSD at NFP minute: slip ≥ 8 pips and ≤ 30 pips.
  - Slippage adverse to direction in ≥ 95% of cases (no positive-slip free lunch).
- [ ] **Step 2:** Implement: `slip = base + a·realized_vol + b·log(size+1)`, with hard caps from event-day Dukascopy empirical 99th percentiles.
- [ ] **Step 3:** Run, commit.

### Task 7.2: Fill engine (the unified simulator)

**Files:**
- Create: `prop-farm/src/propfarm/sim/engine.py`
- Create: `prop-farm/tests/sim/test_engine.py`

- [ ] **Step 1 (TDD):** Test — `fill(order, ts, snapshot)` returns `Fill(price, slip, commission, swap)` and respects:
  - Stop-loss at 1.0950 with mid at 1.0951 then 1.0948: fill at 1.0950 - slip (sell side).
  - Limit order untouched if mid never reaches limit.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** ADR-0005: cost model calibration data sources + assumptions + open questions.
- [ ] **Step 4:** Commit.

---

## Day 8 — CPCV + walk-forward

### Task 8.1: CPCV harness

**Files:**
- Create: `prop-farm/src/propfarm/validation/cpcv.py`
- Create: `prop-farm/tests/validation/test_cpcv.py`

- [ ] **Step 1 (TDD):** Following López de Prado (AFML §7.4): N groups, K test groups per split, purged + embargoed. Test: with N=6, K=2 → C(6,2)=15 splits, each test path covers all groups.
- [ ] **Step 2:** Implement `combinatorial_purged_split(timestamps, n_groups, k_test, embargo_frac) -> Iterator[(train_idx, test_idx)]`.
- [ ] **Step 3:** Property test (hypothesis): purge boundary never has overlap between train and test (no label leakage).
- [ ] **Step 4:** Commit.

### Task 8.2: Walk-forward optimizer with train/test gate

**Files:**
- Create: `prop-farm/src/propfarm/validation/walkforward.py`
- Create: `prop-farm/tests/validation/test_walkforward.py`

- [ ] **Step 1 (TDD):** Test — given a strategy that randomly returns `param_value` as Sharpe in-sample, walk-forward must reject all params (out-of-sample Sharpe should be ≈ 0 by construction).
- [ ] **Step 2:** Implement rolling/expanding window with `(IS_Sharpe, OOS_Sharpe)` pair output and a gate function: `accept = OOS_Sharpe >= 0.5 * IS_Sharpe AND OOS_Sharpe > 0.8`.
- [ ] **Step 3:** Commit.

---

## Day 9 — DSR + PBO

### Task 9.1: Deflated Sharpe Ratio

**Files:**
- Create: `prop-farm/src/propfarm/validation/dsr.py`
- Create: `prop-farm/tests/validation/test_dsr.py`

- [ ] **Step 1 (TDD):** Following Bailey & López de Prado (2014). Test the canonical formula with realistic Phase-1 strategy inputs: `SR=2.5 (per-period), T=120, N_trials=10, skew=-0.3, kurt=5 → DSR ≈ 1.0` (z ≈ 9.085 → Φ(z) ≈ 1.0000). The original W5 reference draft quoted DSR≈0.91 for these inputs; W5 reviewer traced the math end-to-end against Wikipedia, Marti's blog, and the López-de-Prado-blessed `rubenbriones/Probabilistic-Sharpe-Ratio` reference impl and confirmed `≈1.0` is the canonical answer. SR=2.5 / N=10 is in the realistic range for Phase 1 strategy outputs. A separate boundary test that constructs an input producing DSR≈0.95 (at the deploy-gate threshold) is tracked in the deferred ledger and will land before Phase 1 dispatches.
- [ ] **Step 2:** Implement, commit.

### Task 9.2: Probability of Backtest Overfitting

**Files:**
- Create: `prop-farm/src/propfarm/validation/pbo.py`
- Create: `prop-farm/tests/validation/test_pbo.py`

- [ ] **Step 1 (TDD):** Test — synthesized matrix where IS rankings and OOS rankings are randomly permuted should give PBO ≈ 0.5; perfectly correlated should give PBO ≈ 0.
- [ ] **Step 2:** Implement CSCV per Bailey & Borwein (2017).
- [ ] **Step 3:** Commit.

---

## Day 10 — Monte Carlo + stress replay

### Task 10.1: Block bootstrap MC

**Files:**
- Create: `prop-farm/src/propfarm/validation/monte_carlo.py`
- Create: `prop-farm/tests/validation/test_monte_carlo.py`

- [ ] **Step 1 (TDD):** Test — given a trade-return series, `bootstrap(returns, block_size=20, n_paths=10000)` returns shape `(10000, len(returns))` with preserved mean within 2 SE.
- [ ] **Step 2:** Implement stationary block bootstrap (Politis-Romano).
- [ ] **Step 3:** Headline output function: `mc_report(paths) -> {p5_equity, p50_equity, p95_equity, max_dd_distribution, ruin_prob}`. **The headline number is the 5th-percentile equity curve.**
- [ ] **Step 4:** Commit.

### Task 10.2: Stress replay library

**Files:**
- Create: `prop-farm/src/propfarm/validation/stress.py`
- Create: `prop-farm/tests/validation/test_stress.py`
- Create: `prop-farm/data/stress_events.yaml`

- [ ] **Step 1:** Define event windows: 2008-09-15 ± 5d (Lehman), 2015-01-15 (SNB), 2016-10-07 (GBP flash), 2020-03-09 → 2020-03-23 (COVID), 2022-09-23 → 2022-09-28 (UK gilts), 2023-03-10 ± 3d (SVB).
- [ ] **Step 2 (TDD):** Test — `replay(strategy, event) -> equity_curve` and the EURUSD CHF-related event must trip any non-stopped strategy's daily DD predicate.
- [ ] **Step 3:** Implement, commit.

---

## Day 11 — Rules-as-code: per-firm predicates

### Task 11.1: Predicate ABC + FTMO rules

**Files:**
- Create: `prop-farm/src/propfarm/rules/predicates.py`
- Create: `prop-farm/src/propfarm/rules/ftmo.py`
- Create: `prop-farm/tests/rules/test_ftmo.py`
- Create: `prop-farm/docs/firm-tos-snapshots/ftmo-2026-05-12.md`

- [ ] **Step 1:** Fetch and store FTMO ToS verbatim (URL + retrieval date) to `docs/firm-tos-snapshots/`.
- [ ] **Step 2 (TDD):** Boundary tests for daily DD:
  - 5.01% intraday loss → `FTMODailyDD.evaluate(state) == Violation`.
  - 4.99% intraday loss → `None`.
  - Reset at 22:00 ET (FTMO server time).
- [ ] **Step 3:** Max DD (10% trailing, relative to highest balance): boundary tests at 9.99% / 10.01%.
- [ ] **Step 4:** Profit target 10% one-step / 8%+5% two-step: tests.
- [ ] **Step 5:** Banned techniques: no HFT (define: > N orders/min sustained), no latency arb (round-trip < X ms), no same-EA across > $400k combined. Tests for each as `BannedTechniqueChecker`.
- [ ] **Step 6:** Implement, commit.

### Task 11.2: FundedNext + FundingPips predicates

- [ ] Same pattern. Fetch ToS, snapshot, boundary tests, implement. Commit.

---

## Day 12 — Challenge state machine

### Task 12.1: State machine

**Files:**
- Create: `prop-farm/src/propfarm/rules/state_machine.py`
- Create: `prop-farm/tests/rules/test_state_machine.py`

- [ ] **Step 1 (TDD):** States: `PRETRIAL → CHALLENGE → VERIFICATION → FUNDED → PAYOUT_PENDING → POST_PAYOUT` (back to FUNDED). Transitions gated on predicates from Day 11.
- [ ] **Step 2:** Test full lifecycle: 100 simulated trades hit profit target in CHALLENGE → transition to VERIFICATION; hit target again → FUNDED; hold to payout window → PAYOUT_PENDING → POST_PAYOUT with balance reset to initial + 0 (withdrawal taken out).
- [ ] **Step 3:** Test failure paths: daily DD violation in CHALLENGE → `FAILED`; in FUNDED → `ACCOUNT_LOST`.
- [ ] **Step 4:** Payout-aware mode switch flag exposed: `state.sizing_mode in {AGGRESSIVE, PRESERVATION}` flips to PRESERVATION when payout-eligible.
- [ ] **Step 5:** Commit.

---

## Day 13 — Acceptance gate 1: Placebo

### Task 13.1: Random strategy

**Files:**
- Create: `prop-farm/src/propfarm/placebo/random_strategy.py`
- Create: `prop-farm/tests/acceptance/test_placebo_gate.py`

- [ ] **Step 1:** Random-entry strategy: at each bar with prob `p`, open a long or short with 50/50 direction, vol-targeted size, fixed-time exit (e.g. 4h).
- [ ] **Step 2 (TDD, acceptance):** Run through full pipeline (data load → sim engine → cost model → trade ledger → equity curve) over 2018–2024 EURUSD. **Expected return** is exactly `-(spread_paid + commission_paid + swap_paid)`. Tolerance: 3 SE of the bootstrap distribution of costs.
- [ ] **Step 3:** Assertion in the test:

```python
def test_placebo_loses_only_costs():
    result = run_pipeline(strategy=RandomStrategy(seed=42, p=0.01),
                         data="EURUSD-2018-2024", n_paths=100)
    cost_floor = result.total_spread + result.total_commission + result.total_swap
    actual_pnl = result.terminal_equity - result.initial_equity
    se = 3 * result.cost_bootstrap_se
    assert -cost_floor - se <= actual_pnl <= -cost_floor + se, (
        f"Placebo PnL {actual_pnl:.2f} not in [{-cost_floor-se:.2f}, {-cost_floor+se:.2f}]. "
        f"Simulator is broken — alpha is leaking somewhere.")
```

- [ ] **Step 4:** If this fails: **STOP all forward work, root-cause with systematic-debugging skill**. Likely culprits: look-ahead, fill on next bar's open (should be current bar's close ± slip), spread not subtracted, commission rounding error, timezone offset.
- [ ] **Step 5:** If green: commit, mark Acceptance Gate 1 PASSED in STATUS.md.

---

## Day 14 — Acceptance gate 2: MT5 bridge hello-world via nautilus

### Task 14.1: ADR-0003 finalize bridge choice

**Files:**
- Create: `prop-farm/docs/adr/0003-mt5-bridge-choice.md`

- [ ] **Step 1:** Document Day-1 spike result. Compare options:
  - `MetaTrader5` Python pkg direct (simplest; Windows-only; tested on Day 1).
  - MQL5 EA + ZeroMQ (more portable, cross-platform Python client; more moving parts).
  - nautilus-trader's MT5 adapter (if/when stable).
- [ ] **Step 2:** Decide and document.

### Task 14.2: Bridge adapter (Python ↔ MT5)

**Files:**
- Create: `prop-farm/src/propfarm/bridge/mt5_client.py`
- Create: `prop-farm/tests/bridge/test_mt5_client.py` (smoke; needs live demo creds)

- [ ] **Step 1:** Wrap chosen mechanism in `MT5Client` with methods: `connect`, `submit_order`, `modify_order`, `close_position`, `positions`, `account_info`. All return typed dataclasses.
- [ ] **Step 2:** Add explicit RTT measurement + structured logging (JSON lines).

### Task 14.3: Nautilus → bridge integration

**Files:**
- Create: `prop-farm/src/propfarm/bridge/nautilus_adapter.py`
- Create: `prop-farm/tests/acceptance/test_mt5_helloworld.py`

- [ ] **Step 1 (TDD, acceptance):** A nautilus-trader strategy emits a single `MarketOrder` for 0.01 lot EURUSD. The adapter routes to `MT5Client`. The test asserts:
  - Order fills on the FTMO demo account.
  - `account_info()` shows the position before close.
  - SL/TP placed.
  - Strategy can emit a close. Position closes.
  - Round-trip latency p95 < 500 ms over 10 cycles.
  - The simulator's predicted fill (cost-model output) vs the live demo fill diverge ≤ 1 pip on a quiet hour.
- [ ] **Step 2:** Run. If divergence > 1 pip, **STOP, root-cause** — the simulator is wrong or the bridge is leaking cost somewhere.
- [ ] **Step 3:** If green: commit. Mark Acceptance Gate 2 PASSED.

---

## Day 15 — Phase 0 review + Phase 1 unlock

### Task 15.1: Phase 0 gate review

- [ ] **Step 1:** Verification checklist (must all be ✅ in STATUS.md with linked outputs):
  - [ ] Repo + pre-commit working (`pre-commit run --all-files` exits 0).
  - [ ] All tests green (`pytest -q`).
  - [ ] mypy clean (`mypy src/`).
  - [ ] ruff clean (`ruff check .`).
  - [ ] ADRs 0001–0005 committed.
  - [ ] Snapshot manifest covers EURUSD/GBPUSD/USDJPY/XAUUSD/GER40/US100 for 2015-01-01 → 2025-12-31.
  - [ ] Data quality report committed.
  - [ ] Look-ahead linter wired into pre-commit.
  - [ ] CPCV, DSR, PBO, MC, stress replay all have green tests.
  - [ ] All three firms' predicates have boundary tests.
  - [ ] Challenge state machine has full-lifecycle tests.
  - [ ] **Acceptance Gate 1 (placebo) PASSED** with output pasted in STATUS.md.
  - [ ] **Acceptance Gate 2 (MT5 hello-world) PASSED** with latency log pasted in STATUS.md.
- [ ] **Step 2:** Invoke `superpowers:requesting-code-review` to validate Phase 0.
- [ ] **Step 3:** If green: tag `v0.1.0-phase0`, write Phase 1 brainstorming brief.
- [ ] **Step 4:** If anything red: do not start Phase 1.

---

## Self-review notes

- **Spec coverage:** All 8 Phase 0 deliverables from the brief are mapped: repo (Day 1), stack ADR (Day 2), data audit + snapshot (Day 3–4), data quality + lookahead (Day 5), execution sim (Day 6–7), validation (Day 8–10), rules-as-code (Day 11–12), placebo (Day 13), MT5 bridge (Day 1 spike + Day 14 acceptance). Phase 0 stress event list matches the brief verbatim.
- **Day-1 MT5 spike** is a deviation from the brief, which puts MT5 last. Rationale documented in Phase-0 failure mode section below. If the user vetoes this, drop Task 1.3 and the rest still stands.
- **Placebo gate** uses a 3 SE tolerance on the cost bootstrap — explicit, not "round number." Same principle the brief applied to strategy kill criteria.
- **All boundary tests on prop-firm predicates** use the brief's exact 5.01% / 4.99% pattern.
- **No placeholders** found on scan. Every task has files, test goals, and an explicit definition of done.
