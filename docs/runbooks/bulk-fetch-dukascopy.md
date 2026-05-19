# Bulk-fetch Dukascopy (Task 3.3 — last Phase-0 code gap)

`scripts/bulk_fetch_dukascopy.py` populates the on-disk Dukascopy raw cache
that `propfarm.data.ingest` consumes. This runbook is the operator-facing
companion: when to run it, how long it takes, how to resume after an
interruption, and how it chains into the rest of the data pipeline.

Cross-link: `docs/runbooks/gate-2b-fill-recording.md` is the other
operator-side Phase-0 script (live-broker fill recording on the Windows
VPS). Both are "one-off, overnight, tail-f'able" — the patterns here apply
verbatim to both.

## Cache-layout contract (DO NOT BREAK)

The script writes:

```
{raw-root}/{SYMBOL}/{YYYY}/{MM_zero_indexed:02d}/{DD:02d}/{HH:02d}h_ticks.bi5
```

Worked example: EURUSD 2024-03-15 10:00 UTC → `data/raw/dukascopy/EURUSD/2024/02/15/10h_ticks.bi5`
(March is `02` here because Dukascopy's URL convention is 0-indexed:
January = `00`, February = `01`, March = `02`, …, December = `11`).

This is **the same layout** `propfarm.data.ingest._hour_ts_from_path()` parses
back. The contract is locked by both:

* `tests/scripts/test_bulk_fetch_dukascopy.py::test_bulk_fetch_writes_to_correct_cache_layout`
  (the bulk fetcher writes the path), and
* `tests/data/test_ingest.py` (the ingest module reads the same path).

If you change the layout in one, change it in both, or `ingest_to_snapshot.py`
will silently skip every file the bulk fetcher writes.

## Chained workflow

```
scripts/bulk_fetch_dukascopy.py     # this script — raw .bi5 → on-disk cache
        ↓
scripts/ingest_to_snapshot.py       # .bi5 → content-hashed Parquet snapshots
        ↓
Phase-1 EDA notebooks               # consume the snapshots
```

Step 1 (this script) is the **only** step that hits the Dukascopy network.
Once the raw cache is on disk, Step 2 is fully local — re-ingestion never
re-hits Dukascopy, which is the entire reason this script writes raw
`.bi5` instead of going straight to parquet.

## Standard Phase-1 invocation

```bash
python scripts/bulk_fetch_dukascopy.py \
    --symbol EURUSD --symbol GBPUSD \
    --year-min 2015 --year-max 2025
```

This is the Phase-1 EDA scope (two FX majors, 11 calendar years). Expected:

* **~25-35 GB on disk** when complete (~2.5 GB/symbol/year, compressed
  `.bi5` payload + empty-hour markers).
* **3-8 hours wall-clock** at the default `--sleep-ms 100`. The wide range
  reflects Dukascopy CDN variance — observed 0.15s/hour on a cached
  request, 0.6s/hour on a cold one.
* **Resumable**: re-run the same command to continue an interrupted fetch.

## Overnight pattern: `tail -f` from anywhere

The script logs one INFO line per completed day to stdout, plus a year
summary and a grand-total line at the end. Redirect to a file and tail it:

```bash
python scripts/bulk_fetch_dukascopy.py \
    --symbol EURUSD --symbol GBPUSD \
    --year-min 2015 --year-max 2025 \
    > logs/bulk_fetch_$(date -u +%Y%m%d_%H%M%S).log 2>&1 &

# from any terminal:
tail -f logs/bulk_fetch_*.log
```

Per-day log line format:

```
2026-05-19 12:34:56 INFO  EURUSD 2024-03-15 fetched=18 cached=4 empty=2 failed=0 bytes=15,234 elapsed=2.4s
```

* `fetched` — hours newly downloaded with non-zero content this run.
* `cached` — hours already on disk (skipped, no network).
* `empty` — hours that came back zero-byte (weekend, holiday, pre-2010
  sparse hour). Written as 0-byte files so the next run skips them.
* `failed` — hours where every retry raised. **No file is written** for
  these; the next run will retry. `failed > 0` for several consecutive
  days = Dukascopy outage; check `https://datafeed.dukascopy.com` manually.
* `bytes` — total bytes written this day (non-empty hours).

## Resume after partial failure

The script does **two things at startup**:

1. `rglob("*.bi5")` walk under `{raw-root}/{SYMBOL}/` for every requested
   symbol. Builds an in-memory set of `(symbol, year, month, day, hour)`
   tuples already on disk.
2. Print the ETA banner showing `hours to fetch` (work remaining) and
   `skipping N already-cached` (work already done).

Then the fetch loop **skips** every tuple in the set. A run that completed
8 of 10 hours and then died will resume by fetching exactly the remaining
2 on the next invocation. **Failed hours are not skipped** — they have no
file on disk, so they're treated as "still to fetch".

Quick check that resume is doing the right thing: run with `--dry-run`
after an interruption and confirm `hours to fetch` matches your
expectation.

### ETA banner vs observed wall-clock

The startup ETA banner uses a conservative **0.3s/hour** constant (sleep
+ Dukascopy RTT ceiling) for arithmetic. Real CDN latency averages
~0.05-0.15s for cached hours and ~0.3-0.6s for cold hours; total
observed wall-clock is typically **3-5× faster** than the banner's
estimate. For the 2 sym × 11 yr × 100ms scope the banner reads ~21h
but the empirical overnight runs land at 3-8h — this is not a bug,
the banner is deliberately the upper bound so the operator never
under-budgets the run.

## Disk-space budget

| symbols × years | size |
| --- | --- |
| 1 × 1 year | ~1.5 GB |
| 2 × 1 year | ~3 GB |
| 2 × 11 years (Phase-1 default) | ~25-35 GB |
| 6 × 11 years (all Phase-0 symbols) | ~80-100 GB |

Most of the size is the raw 20-byte-record LZMA-compressed payload. Empty
hours (weekends) cost 0 bytes each but add inode count — at 6 symbols ×
11 years × 8760 hours/year ≈ 580k files, plan for an ext4/APFS-friendly
filesystem.

## CLI flags

| Flag | Default | Notes |
| --- | --- | --- |
| `--symbol` | required (repeatable) | Validated against `SUPPORTED_SYMBOLS`. |
| `--year-min` | 2015 | Inclusive lower bound. |
| `--year-max` | current UTC year | Inclusive upper bound. |
| `--raw-root` | `data/raw/dukascopy` | Created if absent. |
| `--sleep-ms` | 100 | Per-request sleep. Range [50, 500]. |
| `--max-retries` | 3 | Per-hour retry budget on `DukascopyError`. |
| `--dry-run` | — | Print the ETA estimate and exit. |

## ETA formula

Printed at startup:

```
ETA ≈ hours_to_fetch × (sleep_ms / 1000 + 0.3)
```

The `0.3s` term is the empirical Dukascopy hour-fetch latency (median);
real wall-clock varies ±50% based on CDN load. Use the ETA to decide
whether to start the run before bed or before lunch, not to set a hard
deadline.

## Known good test invocation

```bash
python scripts/bulk_fetch_dukascopy.py \
    --symbol EURUSD --year-min 2024 --year-max 2024 --dry-run
```

This should print the ETA banner and exit 0 without hitting the network.
If it doesn't, the script is broken — escalate before kicking off an
overnight run.
