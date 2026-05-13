# Gate 2B comparison runbook

This runbook covers the **second half** of Gate 2B: comparing the captured
live fills against the simulator and rendering a verdict. The **first
half** — running `scripts/record_fills.py` on the Windows VPS to produce
the capture parquet — has its own runbook at
[`gate-2b-fill-recording.md`](gate-2b-fill-recording.md). Run that first.

## Prerequisites

1. The capture parquet exists at
   `data/raw/fill_recordings/{run_id}.parquet` (gitignored binary). The
   sibling manifest at `data/raw/fill_recordings/{run_id}.json` confirms
   the schema version and the attempted-vs-filled counts.
2. The recording session captured **at least 100 successful fills**. The
   t-test for systematic bias is well-conditioned at n ≥ 30; the per-symbol
   p95 threshold is meaningful at n ≥ 20 per symbol; 100 fills total
   absorbs the expected ~25% reject rate from limit-outside / news-window
   draws and still leaves a usable sample.
3. The venv is activated: `source .venv/bin/activate` from the repo root.

## Run

```sh
python scripts/run_gate_2b.py \
    --capture-parquet data/raw/fill_recordings/{run_id}.parquet
```

Options:

* `--output-dir <path>` — override where the residuals parquet + markdown
  report land. Default: same directory as the capture parquet.
* `--execution-latency-ms <float>` — pin the fill engine's execution
  latency. When omitted, the harness derives this from the median of the
  captured `broker_latency_ms` column over successfully-filled rows. The
  override is useful when:
  * You measured the bridge RTT separately and want the sim to operate
    on the broker-side time axis (subtract bridge RTT from the median
    capture latency).
  * You want to A/B-test the verdict at different latency assumptions.

Exit code:

* `0` → verdict is `PASS`. Cost models match live. **Wave 6d (stress
  replay) is unblocked.**
* `1` → verdict is `FAIL` or `INVESTIGATE`. See "Verdict interpretation"
  below.
* `2` → the gate could not run (missing parquet, schema mismatch, empty
  capture, etc.). The stderr message identifies the cause.

## Inspect

Two output artifacts land next to the capture parquet (or under
`--output-dir`):

* `{run_id}_residuals.parquet` — one row per capture row, with the
  per-field residuals (`live - sim`) and the retcode-match flag. Use
  polars / DuckDB to slice by symbol / order-type / hour-of-day.
* `{run_id}_report.md` — operator-facing markdown including the
  **MarketState reconstruction audit** table, per-field residual
  distributions, the per-symbol thresholds, and the verdict with failure
  reasons.

## Verdict interpretation

| Verdict       | Action                                                                                                                                                         |
|---------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `PASS`        | Cost models are calibrated. Proceed to Wave 6d (stress replay, Task 10.2).                                                                                     |
| `INVESTIGATE` | Per-symbol thresholds held but a t-test detected systematic bias on at least one residual field. Read the residual distributions; decide whether to recalibrate or accept. |
| `FAIL`        | At least one per-symbol fill-price p95 OR the spread p95 exceeded its threshold. **Recalibrate the cost models. Do NOT widen Gate 2B tolerances.**             |

A `FAIL` is the gate doing its job — the cost models (`propfarm.sim.spread`,
`propfarm.sim.slippage`) carry `confidence="uncertain"` on every shipped
calibration entry exactly because we expected to recalibrate against the
recorded reality. The numbers to edit are in `CALIBRATIONS` /
`PROD_CALIBRATIONS` inside those modules. After editing, rerun the gate
on the same capture — if it now passes, the calibration converged. Repeat
until pass.

**Do not widen the gate thresholds to make a FAIL pass.** The per-symbol
0.5-pip / 5-pip values are the user-mandated definition of "Phase 0
acceptably calibrated". Loosening them to dodge a FAIL is exactly the
class of silent-drift the gate exists to prevent.

## How to read the MarketState reconstruction audit table

The report's first table looks like:

```
| field            | source             | detail                                                                              |
|------------------|--------------------|-------------------------------------------------------------------------------------|
| symbol           | FROM_FILLRECORD    | column 'symbol' copied verbatim                                                     |
| ts_utc           | FROM_FILLRECORD    | column 'request_time_utc' copied verbatim                                           |
| realized_vol_5m  | COMPUTED           | rolling stdev of last 5 same-symbol log-returns × sqrt(362880); value=0.082341      |
| news_window      | DEFAULTED          | Phase 0 has no news calendar; defaulted to False. Operator must manually flag rows. |
| stress_mode      | DEFAULTED          | record_fills.py does not capture stress_mode; defaulted to False.                   |
```

Read every row:

* `FROM_FILLRECORD` rows are 1-to-1 column copies. Zero risk.
* `COMPUTED` rows show the formula and the value for **row 0** of the
  capture. The harness recomputes per-row internally; the audit captures
  the canonical structure.
* `DEFAULTED` rows are the policy assumptions. Two assumptions Phase 0
  bakes in:
  1. **`news_window=False` for every row.** If your 24h capture
     overlapped NFP (first Friday of the month, 12:30 UTC), CPI, FOMC,
     or any central-bank decision, the corresponding rows experienced
     wider spreads and adverse slip that the sim did *not* model. The
     residual distribution will show as adverse-positive bias on those
     rows. Either re-record outside news windows or accept the bias as a
     known limitation. Document either choice.
  2. **`stress_mode=False` for every row.** The live FTMO demo never
     reports a "stress mode" — the simulator's stress amplification only
     kicks in when the strategy code asserts it (Task 10.2 historical
     replay path). A normal capture is the right normal-conditions
     baseline.

If the audit table's `source` column ever contains a `DEFAULTED` row with
empty `detail`, the harness has a bug and the reviewer rejects the
report.

## Determinism contract

`run_gate_2b(capture_parquet_path, execution_latency_ms=X)` produces a
bit-identical `Gate2BReport` and identical output parquet bytes for the
same input parquet + same `X`. The SHA256 of the capture parquet is
pinned into the report so a future audit can confirm the comparison ran
on the same bytes.

If you recompute with a different `execution_latency_ms`, the report is
expected to change — but the *capture* SHA256 stays the same, so the
provenance trail is still intact.

## When to re-record vs recalibrate

| Failure pattern                                                       | Likely cause                                                          | Response                                                                                                       |
|-----------------------------------------------------------------------|-----------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| `fill_price_p95_exceeded` on every symbol                             | sim's `base_pips` slippage is too low globally                        | Recalibrate `propfarm.sim.slippage.CALIBRATIONS` base values. Re-run gate.                                     |
| `fill_price_p95_exceeded` on one symbol only                          | that symbol's `base_pips` / `vol_coef` is off                         | Recalibrate that symbol's entry only.                                                                          |
| `spread_p95_exceeded`                                                 | sim's spread model produces too tight a bid-ask                       | Recalibrate `propfarm.sim.spread` (session-open multipliers, base bps).                                        |
| `systematic_bias:latency_ms` but thresholds pass                      | `execution_latency_ms` differs from live median                       | Pin a per-symbol `execution_latency_ms` override that matches each symbol's median capture latency.            |
| `systematic_bias:fill_price` and capture window overlapped news/event | DEFAULTED `news_window=False` is wrong for those rows                 | Either re-record outside news windows, or manually flag rows and run the gate on the unflagged subset.         |
| Schema mismatch error                                                 | `scripts/record_fills.py` was changed and the parquet predates change | Re-record with the current script. Field-by-field changes are tracked in `FillRecord` and the schema-lock test. |

## Cross-references

* `src/propfarm/gates/gate_2b.py` — the harness module.
* `src/propfarm/sim/fill_engine.py` — the simulator under test. Per-request
  semantics documented at top.
* `scripts/record_fills.py` — the live capture script (`FillRecord` is the
  schema lock).
* `docs/runbooks/gate-2b-fill-recording.md` — first-half runbook.
