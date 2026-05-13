# Spread calibration recording — VPS runbook

Goal: capture ≥24 hours of live tick-level bid/ask snapshots from an MT5
demo terminal on the Windows VPS, project them into the
`SpreadCalibrationEntry` shape consumed by
`src/propfarm/sim/spread.py`, and flip the per-symbol calibration
`confidence` flag from `"uncertain"` to `"high"`.

The seed values shipped in `propfarm.sim.spread.CALIBRATIONS` are
educated guesses from publicly-observable retail-broker tapes. They are
**not** suitable for any live-account sizing decision — Gate 2B
(sim-vs-live fill recording) refuses to certify against an `"uncertain"`
calibration, and this runbook is the path that lifts that refusal.

This runbook is the **schema contract** between the live-capture script
(a separate, later one-off — `scripts/record_spreads.py`, not part of
Task 6.1) and the spread model. Anyone running the capture MUST produce
files that conform to the schema in §3 below.

---

## 1. Why we record live, not synthesise

The spread model has four parameters per symbol that we cannot reliably
infer from public data:

| Parameter | Why public data fails |
|---|---|
| `baseline_bps` | Retail aggregators publish "indicative" spreads, not what FTMO/FundedNext/FundingPips actually quote. |
| `session_open_multiplier` | The 07:00 UTC widening on FTMO MT5 is broker-specific; LP behaviour differs by 2-4x across firms. |
| `decay_half_life_min` | Public tapes are 1-min OHLC at best — the post-open decay shape needs sub-minute resolution to fit cleanly. |
| `weekend_reopen_multiplier` | Sunday 22:00 UTC liquidity is firm-specific and dependent on the LP mix that night. |

Synthesising these from a public model would risk silently mis-pricing
the largest *time-varying* simulator cost — the exact failure mode the
placebo gate (Gate 1) is designed to catch but cannot fully insure
against without a calibration anchor.

## 2. What to record

Per-symbol, per-minute snapshots of the live MT5 quote book, for the six
symbols in `propfarm.data.quality.SUPPORTED_SYMBOLS`:

* `EURUSD`, `GBPUSD`, `USDJPY`
* `XAUUSD`
* `GER40`, `US100`

Capture window:

* **Minimum**: 24 contiguous hours covering at least one London open
  (07:00 UTC), one NY open (12:00 or 13:00 UTC depending on DST), and
  one Tokyo open (23:00 UTC). 24 hours is sufficient to fit `baseline`
  + at least one decay curve per symbol.
* **Recommended**: 5 weekdays + 1 weekend reopen (Sunday 22:00 UTC).
  This is what flips the per-symbol `confidence` flag from `"uncertain"`
  to `"high"` (see §6).
* **Pre-flight**: a 10-minute dry-run on Friday 12:00-12:10 UTC to
  validate the recording script's permissions, parquet writer, and
  filename conventions before the full capture starts.

## 3. Output schema (the contract)

Each recording session writes one parquet file per symbol per UTC
calendar date:

```
data/raw/spread_snapshots/{symbol}_{YYYY-MM-DD}.parquet
```

Example paths after a full capture week:

```
data/raw/spread_snapshots/EURUSD_2026-05-13.parquet
data/raw/spread_snapshots/EURUSD_2026-05-14.parquet
data/raw/spread_snapshots/XAUUSD_2026-05-13.parquet
data/raw/spread_snapshots/US100_2026-05-13.parquet
```

### 3.1 Column schema

Each file is a polars/pyarrow parquet with the following columns. The
schema is the contract: any field rename or type change here invalidates
every downstream calibration consumer until tests in
`tests/sim/test_spread.py` are updated to match.

| Column | Type | Description |
|---|---|---|
| `ts_utc` | `datetime[ns, UTC]` | Tz-aware UTC timestamp. Tz **must not** be dropped — naive datetimes are rejected by every downstream consumer. Snapshot at 1-minute grid, aligned to the minute (00 seconds, 0 microseconds). |
| `symbol` | `str` (categorical) | One of `SUPPORTED_SYMBOLS`. Redundant with the filename; included so that joining multiple files keeps the symbol explicit. |
| `bid` | `float64` | Best bid at `ts_utc`, in the symbol's native price unit (e.g. 1.0850 for EURUSD, 3475.20 for XAUUSD, 18750.5 for GER40). |
| `ask` | `float64` | Best ask at `ts_utc`, same unit as `bid`. Must satisfy `ask >= bid`. |
| `spread_pips` | `float64` | `(ask - bid) / pip_size[symbol]`. FX pip is 0.0001 except USDJPY which is 0.01; for indices and metals the pip is the smallest tick (1.0 for GER40, 0.01 for XAUUSD). Recorded for human readability and to cross-check the bps derivation. |
| `spread_bps` | `float64` | `(ask - bid) / mid * 10000`, where `mid = (bid + ask) / 2`. This is the unit the spread model consumes. |
| `is_market_open` | `bool` | `propfarm.data.quality.is_market_open(symbol, ts_utc)`. Pre-computed at capture time so the recording can be inspected without needing the propfarm package. Rows with `is_market_open=False` are kept for diagnostics but excluded from calibration fits. |
| `is_news_window` | `bool` | If a news-calendar module is wired into the capture script, set `True` when the timestamp falls inside a flagged event. **For Task 6.1 deliverables this column is always `False`** — Task 6.1 does not consume news timestamps, and seeding the column to all-`False` keeps the schema forward-compatible with a later news-calendar overlay. |
| `mt5_session_id` | `str` | Identifies the MT5 terminal session that produced the row (e.g. `"ftmo-demo-2026-05-13T07:00:00Z"`). Carried forward so an audit can reconstruct which terminal each row came from. |

### 3.2 Sample row (illustrative)

```
ts_utc            symbol  bid     ask     spread_pips spread_bps is_market_open is_news_window mt5_session_id
2026-05-13 07:00  EURUSD  1.0851  1.0852  1.0         0.0922     true           false          ftmo-demo-2026-05-13T00:00:00Z
2026-05-13 07:01  EURUSD  1.0852  1.08525 0.5         0.0461     true           false          ftmo-demo-2026-05-13T00:00:00Z
2026-05-13 07:05  EURUSD  1.0853  1.08533 0.3         0.0277     true           false          ftmo-demo-2026-05-13T00:00:00Z
```

### 3.3 Sampling rate

* **1-minute grid**, aligned to UTC minute boundaries. Finer grids
  (e.g. 1-second) over-sample noise that the spread model's exponential-
  decay curve does not consume; 1-minute is the lowest-information-loss
  grid the calibration fit consumes.
* If the recording script samples at higher resolution (e.g. tick-by-
  tick), it must aggregate to the 1-min grid with the **median** of
  bid/ask within each minute. Median is preferable to mean because the
  occasional 1-tick outlier (a flash-fill on a sparse LP) would bias
  the mean baseline upward.

### 3.4 Vendor-convention catch — pip size

MT5's "pip" convention varies. We assume the following:

| Symbol | Assumed pip size | Source |
|---|---|---|
| EURUSD, GBPUSD | 0.0001 | 5-digit price quote ÷ 10. |
| USDJPY | 0.01 | 3-digit JPY quote ÷ 10. |
| XAUUSD | 0.01 | Cent on the dollar-per-ounce price. |
| GER40 | 1.0 | One index point. |
| US100 | 1.0 | One index point. |

The recording script MUST hardcode this mapping (or re-derive from
`mt5.symbol_info(symbol).point * 10`) and emit `spread_pips` accordingly.
Mismatches between the recording-time pip convention and the
calibration-time pip convention are silent — they corrupt the
diagnostic column but not the `spread_bps` column, which is what the
model consumes. The runbook reviewer will request a pip-convention
cross-check during sign-off.

## 4. Recording workflow (high level)

The recording script lives at `scripts/record_spreads.py` and is a
**deferred task** — it is not part of Task 6.1's scope. Task 6.1 ships
the schema, the parquet path convention, and the
`SpreadCalibrationEntry` Pydantic model. The script that produces the
parquet files lands separately.

When the script is written, it must:

1. RDP into the production VPS (same host as the MT5 spike — see
   `docs/runbooks/mt5-spike-runbook.md`).
2. Activate a Python 3.12+ venv with `MetaTrader5` and `pyarrow`
   installed. The propfarm package itself must NOT be installed on the
   VPS — the recording script writes parquets and the calibration fit
   reads them on the implementer's local machine.
3. Open the FTMO MT5 demo terminal, log in, confirm all six symbols
   stream quotes in the Market Watch panel.
4. Run `python scripts/record_spreads.py --duration 24h --out
   data/raw/spread_snapshots/`. The script polls
   `mt5.symbol_info_tick(symbol)` on a 1-minute heartbeat and writes
   one parquet per symbol per UTC date.
5. At end of capture, verify file sizes with
   `python scripts/inspect_spread_snapshots.py --dir
   data/raw/spread_snapshots/`. Expected ~1440 rows/symbol/24h (1 row
   per UTC minute for a fully-open symbol; closed-session minutes
   still produce rows with `is_market_open=False`).

## 5. Conversion to `SpreadCalibrationEntry` (DEFERRED)

A separate `calibrate_from_recordings(snapshots_dir) ->
dict[str, SpreadCalibrationEntry]` function projects the parquet files
into the typed registry that ships with the spread module. **This
function is NOT in Task 6.1's scope** — it is deferred to the post-
capture follow-up because:

1. It needs the actual recorded data to test against (chicken-and-egg
   with this runbook).
2. The fit's curve shape — exponential vs. piecewise-linear — should
   be chosen after looking at the real data, not before.
3. The acceptance criteria for "the fit is good enough" (R² ≥ 0.9?
   residual heteroskedasticity check?) are best decided when we see
   the real residuals.

For now the function is a stub-shaped placeholder in the deferred
ledger. When implemented, it will:

1. Filter rows where `is_market_open=False` and `is_news_window=True`.
2. Compute `baseline_bps` as the median `spread_bps` across the
   sub-window `[open + 60 min, open + (next open - 60 min)]` for each
   session-pair — i.e. the "mid-session" period that the model assumes
   is unaffected by either of the bracketing opens.
3. For each session open, fit `factor(t) = 1 + (M - 1) * 0.5 ^ (t /
   half_life)` to the spread tape for `t ∈ [0, 60 min]`. Return the
   `(M, half_life)` pair as `session_open_multiplier` and
   `decay_half_life_min`. If multiple sessions are captured (London,
   NY, Tokyo), average the per-session fits; the spread model uses a
   single `session_open_multiplier` per symbol today.
4. Same fit for the Sunday 22:00 UTC reopen → `weekend_reopen_multiplier`.
5. Leave `news_multiplier` at the seed value — calibrating it requires
   the news-calendar overlay which lands in a later task.

## 6. Confidence-flag transition

A `SpreadCalibrationEntry` flips from `confidence="uncertain"` to
`confidence="high"` only when ALL of the following hold:

1. ≥5 weekdays of data covering all three major session opens
   (London 07:00 UTC, NY 12:00/13:00 UTC, Tokyo 23:00 UTC).
2. ≥1 Sunday 22:00 UTC reopen captured.
3. The fitted exponential-decay curve has R² ≥ 0.9 against the
   recorded post-open spread tape, with no obvious heteroskedasticity
   in the residuals (visual inspection of residual plot by the
   implementer is sufficient; a formal Breusch-Pagan test is overkill
   for this scale of fit).
4. The fitted `baseline_bps` lies within 50% of the seed value (a
   *sanity check* against silent calibration-pipeline bugs that would
   produce, say, a 10x-too-large baseline; a 50% deviation is large
   enough to absorb genuine broker-to-broker differences and small
   enough to catch obvious bugs).

If any of these fail for a symbol, that symbol's entry stays
`"uncertain"` and Gate 2B continues refusing to certify against it.
That is the safe failure mode — the placebo gate uses `"uncertain"`
spreads as an upper bound and will simply be conservative.

## 7. What this runbook intentionally does NOT cover

* **News-calendar wiring.** Task 6.1 accepts a `news_window: bool`
  flag and multiplies. Deciding *when* the flag is `True` (which
  events count, what the pre/post window is around each event) is the
  news-calendar module's job in a separate task. The recording schema
  reserves `is_news_window` as a column so the post-news-calendar
  capture can flip rows to `True` without a schema migration.
* **Slippage calibration.** Slippage is a separate model
  (Task 7.1, `src/propfarm/sim/slippage.py`) with its own recording
  flow. Slippage needs *executed* order data, not just bid/ask
  snapshots — the recording workflows are not mergeable without a
  separate order-execution overlay.
* **Holiday handling.** The recording will naturally show
  `is_market_open=False` rows on the Jan 1 / Dec 25 / Dec 26 full-
  closes. The calibration fit filters these out (see §5 step 1) so no
  holiday-specific column is needed.
* **The recording script itself.** As called out in §4, the script
  ships as a separate deferred deliverable. This runbook documents
  what the script must produce, not how it must work internally.

---

## Acceptance checklist (recorder sign-off)

When the recording is complete, the recorder confirms in `STATUS.md`:

* [ ] Six parquet files per UTC date, one per symbol.
* [ ] Each file has the columns listed in §3.1, with the types listed.
* [ ] `ts_utc` is tz-aware UTC, aligned to the minute.
* [ ] `spread_bps = (ask - bid) / ((bid + ask) / 2) * 10000` (spot-checked
  on ≥10 random rows).
* [ ] Row count per file is within 5% of the expected `1440 ×
  fraction_of_day_market_was_open`.
* [ ] `is_market_open` column matches
  `propfarm.data.quality.is_market_open(symbol, ts_utc)` for all rows
  (spot-checked on ≥10 random rows).
* [ ] Capture covered ≥1 London open, ≥1 NY open, ≥1 Tokyo open, and
  (if the recommended 5-day window was hit) ≥1 Sunday 22:00 UTC reopen.

When all checked, the recorder hands off to the calibrator (likely the
same person, in a fresh session) to run the calibration-fit follow-up
and flip the appropriate `confidence` flags in
`propfarm.sim.spread.CALIBRATIONS`.
