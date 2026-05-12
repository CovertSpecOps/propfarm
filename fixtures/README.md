# Canonical synthetic returns fixture

This directory holds the **single source of truth** for synthetic return
series consumed by every validation-math agent in Wave 5 (CPCV, walk-forward,
DSR, PBO, Monte Carlo block bootstrap).

## Hard rule for downstream agents

**Do NOT regenerate.** Read `synthetic_returns.parquet`, filter by the
`regime` column, and use the returns as-is. Property tests in
`tests/fixtures/test_synthetic_returns.py` verify regime properties; if those
tests pass against the parquet on disk, the fixture is correct.

If your Wave 5 agent code regenerates its own returns, the reviewer will
reject the change. The whole point of pinning one fixture is that every
agent's results are comparable on the same underlying data.

## Files

| File | Purpose |
|---|---|
| `synthetic_returns.parquet` | Long-form fixture, 20,000 rows total. |
| `synthetic_returns.sha256` | SHA256 hex digest of the parquet bytes (pinning manifest). |
| `README.md` | This document. |

## Schema

| Column | Type | Units / Notes |
|---|---|---|
| `ts` | `timestamp[ns]` | Business-day timestamp (no weekends). Starts 2010-01-04. |
| `regime` | `string` | One of `trending`, `mean_reverting`, `choppy`, `fat_tailed`. |
| `ret` | `float64` | Single-period log return (daily). |

Rows are stored in long form: 5,000 observations per regime x 4 regimes =
**20,000 rows**. Filter by `regime` in your consumer.

## Regime definitions

| Regime | Generator | Annualized mu | Annualized sigma | Key property |
|---|---|---|---|---|
| `trending` | i.i.d. Normal(mu_d, sigma_d) | +8% | 12% | Positive drift, mean detectable above zero. |
| `mean_reverting` | AR(1) with rho = -0.3, Normal innovations | 0% | 12% | Lag-1 autocorrelation near -0.3. |
| `choppy` | i.i.d. Normal(0, sigma_d) | 0% | 15% | No drift, no autocorrelation, pure noise. |
| `fat_tailed` | Student-t with nu=5, scaled to sigma | 0% | 15% | Excess kurtosis (theoretical = 6 for nu=5). |

Annualization factor: sqrt(252) for vol; 252 for mean.

### Student-t df note (nu=5, not nu=4)

The Wave 5 spec originally suggested Student-t with `nu=4`, but Student-t
with `nu=4` has *infinite* theoretical kurtosis (`6/(nu-4)` requires
`nu>4`). That makes a sample-kurtosis lower-bound property test unstable.
We use **`nu=5`** so the theoretical excess kurtosis is finite (= 6) while
keeping a strong heavy-tail signal. This is documented in the generator
docstring and locked by the property test.

## Seed

`numpy.random.default_rng(seed=20260512)`. **Hardcoded, immutable.** The
value 20260512 is the date the fixture was first committed (YYYYMMDD). The
generator writes the parquet with deterministic settings
(`use_dictionary=False`, `write_statistics=False`, `compression="zstd"`,
`compression_level=3`), so re-running the script produces byte-identical
output.

## How to regenerate

You should not need to. If a deliberate spec change is required, bump the
seed or version, regenerate, and update the SHA256 manifest:

```bash
# from repo root
python scripts/generate_synthetic_returns.py
# Verify the new bytes (the generator already updates the .sha256 file):
shasum -a 256 fixtures/synthetic_returns.parquet
cat fixtures/synthetic_returns.sha256
```

Then re-run the property tests:

```bash
pytest tests/fixtures/test_synthetic_returns.py -v
```

## How to verify integrity

From the repo root:

```bash
# Compare on-disk parquet bytes to the pinned manifest:
shasum -a 256 fixtures/synthetic_returns.parquet | awk '{print $1}'
cat fixtures/synthetic_returns.sha256
```

The two hashes must match. If they don't, the fixture has drifted and any
Wave 5 result computed against it is invalid until the discrepancy is
explained. The property test
`test_sha256_matches_manifest` enforces this automatically.

## Observed sample statistics (seed = 20260512)

For reference and quick sanity-checking:

| regime | n | ann_mu | ann_vol | lag-1 ac | excess kurt |
|---|---:|---:|---:|---:|---:|
| trending | 5000 | +0.0502 | 0.1222 | +0.0181 | 0.1003 |
| mean_reverting | 5000 | -0.0054 | 0.1186 | -0.2872 | 0.0896 |
| choppy | 5000 | -0.0105 | 0.1491 | -0.0202 | 0.0080 |
| fat_tailed | 5000 | +0.0158 | 0.1506 | +0.0102 | 2.8779 |

These exact values are reproducible from the seed — if you regenerate and
they differ, something is wrong.
