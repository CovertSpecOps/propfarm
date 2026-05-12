"""Generate the canonical synthetic returns fixture for the prop-farm project.

This script produces ``fixtures/synthetic_returns.parquet``, a single long-form
parquet file containing four synthetic return regimes used by every Wave 5
validation-math agent (CPCV, walk-forward, DSR, PBO, Monte Carlo block
bootstrap). All downstream consumers MUST read this file rather than
regenerating returns locally; the SHA256 manifest pins the exact bytes.

Regimes (5000 daily observations each, business days starting 2010-01-04):

* ``trending``        — i.i.d. Normal with annualized mu=+8%, sigma=12%.
* ``mean_reverting``  — AR(1) with rho=-0.3 around zero mean, sigma=12%.
* ``choppy``          — i.i.d. Normal(0, sigma=15%); pure noise.
* ``fat_tailed``      — Student-t with nu=5, scaled to sigma=15%.

Student-t degrees-of-freedom note: the spec contemplated nu=4 but Student-t
with nu=4 has *infinite* theoretical kurtosis (kurtosis = 6/(nu-4) requires
nu>4), which makes a sample-based property test unstable. We use nu=5 so the
theoretical excess kurtosis is finite (= 6), keeping the heavy-tail signal
strong while letting the test assert a sane lower bound.

Seed (20260512) is hardcoded; running this script twice produces byte-
identical output. A side-car ``synthetic_returns.sha256`` file pins the
expected hash so downstream tests detect silent drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-not-found]
import pyarrow.parquet as pq  # type: ignore[import-not-found]

# --------------------------------------------------------------------------- #
# Configuration — DO NOT CHANGE WITHOUT BUMPING THE FIXTURE VERSION.
# --------------------------------------------------------------------------- #
SEED: int = 20260512
N_PER_REGIME: int = 5000
START_DATE: str = "2010-01-04"
ANNUALIZATION: int = 252  # trading days per year
STUDENT_T_DF: int = 5  # nu; see module docstring for the nu=4 vs nu=5 choice

REGIME_ORDER: tuple[str, ...] = ("trending", "mean_reverting", "choppy", "fat_tailed")

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURE_PATH: Path = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"
SHA256_PATH: Path = REPO_ROOT / "fixtures" / "synthetic_returns.sha256"


# --------------------------------------------------------------------------- #
# Per-regime generators. Each returns a 1-D float64 ndarray of length ``n``.
# --------------------------------------------------------------------------- #
def gen_trending(rng: np.random.Generator, n: int) -> np.ndarray:
    """Geometric-Brownian-ish: i.i.d. Normal with positive drift."""
    mu_ann, sigma_ann = 0.08, 0.12
    mu_d = mu_ann / ANNUALIZATION
    sigma_d = sigma_ann / np.sqrt(ANNUALIZATION)
    return rng.normal(loc=mu_d, scale=sigma_d, size=n).astype(np.float64)


def gen_mean_reverting(rng: np.random.Generator, n: int) -> np.ndarray:
    """AR(1) with rho=-0.3 around zero mean. Annualized sigma=12%."""
    rho = -0.3
    sigma_ann = 0.12
    sigma_d = sigma_ann / np.sqrt(ANNUALIZATION)
    # Innovation variance chosen so the stationary variance matches sigma_d**2:
    # Var(r) = sigma_eps**2 / (1 - rho**2)  =>  sigma_eps = sigma_d * sqrt(1 - rho**2)
    sigma_eps = sigma_d * np.sqrt(1.0 - rho**2)
    eps = rng.normal(loc=0.0, scale=sigma_eps, size=n)
    r = np.empty(n, dtype=np.float64)
    # Seed r[0] from the stationary distribution N(0, sigma_d^2):
    r[0] = rng.normal(loc=0.0, scale=sigma_d)
    for t in range(1, n):
        r[t] = rho * r[t - 1] + eps[t]
    return r


def gen_choppy(rng: np.random.Generator, n: int) -> np.ndarray:
    """Pure noise: i.i.d. Normal(0, sigma). Annualized sigma=15%."""
    sigma_ann = 0.15
    sigma_d = sigma_ann / np.sqrt(ANNUALIZATION)
    return rng.normal(loc=0.0, scale=sigma_d, size=n).astype(np.float64)


def gen_fat_tailed(rng: np.random.Generator, n: int) -> np.ndarray:
    """Student-t with nu=5 (finite kurtosis), scaled to annualized sigma=15%."""
    nu = STUDENT_T_DF
    sigma_ann = 0.15
    sigma_d = sigma_ann / np.sqrt(ANNUALIZATION)
    # Raw Student-t has variance nu/(nu-2). Scale so sample target std = sigma_d.
    raw = rng.standard_t(df=nu, size=n)
    raw_std_theoretical = float(np.sqrt(nu / (nu - 2.0)))
    scaled: np.ndarray = (raw / raw_std_theoretical * sigma_d).astype(np.float64)
    return scaled


# --------------------------------------------------------------------------- #
# Assembly + write.
# --------------------------------------------------------------------------- #
def build_dataframe(rng: np.random.Generator) -> pd.DataFrame:
    """Generate all four regimes and return a long-form DataFrame."""
    ts = pd.bdate_range(start=START_DATE, periods=N_PER_REGIME)

    generators = {
        "trending": gen_trending,
        "mean_reverting": gen_mean_reverting,
        "choppy": gen_choppy,
        "fat_tailed": gen_fat_tailed,
    }

    frames: list[pd.DataFrame] = []
    for regime in REGIME_ORDER:
        ret = generators[regime](rng, N_PER_REGIME)
        frames.append(pd.DataFrame({"ts": ts, "regime": regime, "ret": ret}))

    df = pd.concat(frames, ignore_index=True)
    # Stable column order + explicit dtypes for byte-reproducible parquet.
    df["regime"] = df["regime"].astype("string")
    df["ret"] = df["ret"].astype("float64")
    return df[["ts", "regime", "ret"]]


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write the dataframe to parquet with deterministic settings."""
    schema = pa.schema(
        [
            pa.field("ts", pa.timestamp("ns")),
            pa.field("regime", pa.string()),
            pa.field("ret", pa.float64()),
        ]
    )
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=3,
        use_dictionary=False,  # keep bytes stable across runs
        write_statistics=False,  # statistics can leak nondeterminism
        data_page_size=1 << 20,
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    rng = np.random.default_rng(seed=SEED)
    df = build_dataframe(rng)
    write_parquet(df, FIXTURE_PATH)
    digest = sha256_file(FIXTURE_PATH)
    SHA256_PATH.write_text(digest + "\n", encoding="utf-8")
    n_regimes = df["regime"].nunique()
    n_rows = len(df)
    print(
        f"written {n_rows} rows in {n_regimes} regimes to "
        f"fixtures/synthetic_returns.parquet (sha256={digest[:12]}...)"
    )


if __name__ == "__main__":
    main()
