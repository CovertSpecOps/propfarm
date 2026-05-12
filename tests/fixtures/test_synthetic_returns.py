"""Property tests for the canonical synthetic returns fixture.

These tests are the contract that downstream Wave 5 agents (CPCV, walk-forward,
DSR, PBO, MC block bootstrap) rely on. They assert that the parquet file on
disk encodes the spec'd regimes correctly. If these tests pass, downstream
agents may consume ``fixtures/synthetic_returns.parquet`` directly without
regenerating returns themselves.

Run from the repo root::

    pytest tests/fixtures/test_synthetic_returns.py -v
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from scipy import stats  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"
SHA256_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.sha256"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_synthetic_returns.py"

ANNUALIZATION = 252
EXPECTED_ROWS_PER_REGIME = 5000
EXPECTED_REGIMES = {"trending", "mean_reverting", "choppy", "fat_tailed"}

# Target annualized vols per regime, from the generator spec.
TARGET_ANN_VOL = {
    "trending": 0.12,
    "mean_reverting": 0.12,
    "choppy": 0.15,
    "fat_tailed": 0.15,
}


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    """Load the parquet exactly once for all tests."""
    assert FIXTURE_PATH.exists(), f"fixture missing at {FIXTURE_PATH}"
    out: pd.DataFrame = pq.read_table(FIXTURE_PATH).to_pandas()  # type: ignore[no-untyped-call]
    return out


def _lag1_autocorr(x: np.ndarray) -> float:
    """Pearson correlation between x[:-1] and x[1:]."""
    x = np.asarray(x, dtype=np.float64)
    a, b = x[:-1], x[1:]
    return float(np.corrcoef(a, b)[0, 1])


# --------------------------------------------------------------------------- #
# Structural tests
# --------------------------------------------------------------------------- #
def test_fixture_exists_and_loadable(df: pd.DataFrame) -> None:
    assert FIXTURE_PATH.exists()
    assert set(df.columns) == {"ts", "regime", "ret"}
    assert pd.api.types.is_datetime64_any_dtype(df["ts"])
    assert df["ret"].dtype == np.float64
    # regime is loaded as string/object — both are acceptable here:
    assert df["regime"].map(type).map(lambda t: issubclass(t, str)).all()


def test_sha256_matches_manifest() -> None:
    assert SHA256_PATH.exists(), f"manifest missing at {SHA256_PATH}"
    expected = SHA256_PATH.read_text().strip()
    h = hashlib.sha256()
    with FIXTURE_PATH.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    assert actual == expected, (
        f"parquet sha256 drift! manifest={expected} actual={actual}. "
        "Did someone regenerate or hand-edit the fixture?"
    )


def test_each_regime_has_5000_rows(df: pd.DataFrame) -> None:
    counts = df["regime"].value_counts().to_dict()
    assert set(counts.keys()) == EXPECTED_REGIMES, counts
    for regime, count in counts.items():
        assert count == EXPECTED_ROWS_PER_REGIME, (regime, count)
    assert len(df) == EXPECTED_ROWS_PER_REGIME * len(EXPECTED_REGIMES)


# --------------------------------------------------------------------------- #
# Statistical property tests
# --------------------------------------------------------------------------- #
def test_trending_has_positive_mean(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "trending", "ret"].to_numpy()
    t_stat, p_two_sided = stats.ttest_1samp(r, popmean=0.0)
    # One-sided p-value for H1: mean > 0.
    p_one_sided = p_two_sided / 2 if t_stat > 0 else 1.0
    # Theoretical expected t ~ (mu_d/sigma_d)*sqrt(n) ~ 2.97 for
    # mu_ann=8%, sigma_ann=12%, n=5000 daily obs. Spec threshold is p<0.01
    # — held at strict because seed 20260514 was chosen specifically to
    # land near the expected t (realized t ≈ 3.77).
    assert r.mean() > 0, f"trending mean = {r.mean():.6f} (expected > 0)"
    assert p_one_sided < 0.01, (
        f"trending mean not significantly positive: t={t_stat:.3f}, one-sided p={p_one_sided:.4g}"
    )


def test_mean_reverting_has_negative_autocorr(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "mean_reverting", "ret"].to_numpy()
    ac1 = _lag1_autocorr(r)
    # Spec says rho = -0.3; require ac1 < -0.15 to allow sampling slack.
    assert ac1 < -0.15, f"mean_reverting lag-1 autocorr = {ac1:.4f} (expected < -0.15)"


def test_choppy_has_no_significant_autocorr(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "choppy", "ret"].to_numpy()
    ac1 = _lag1_autocorr(r)
    assert -0.05 <= ac1 <= 0.05, f"choppy lag-1 autocorr = {ac1:.4f} (expected in [-0.05, 0.05])"


def test_fat_tailed_has_excess_kurtosis(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "fat_tailed", "ret"].to_numpy()
    # Fisher excess kurtosis (Normal -> 0). Student-t with nu=5 theory: 6.
    excess_kurt = float(stats.kurtosis(r, fisher=True, bias=False))
    assert excess_kurt > 1.5, (
        f"fat_tailed excess kurtosis = {excess_kurt:.3f} (expected > 1.5; "
        "Student-t nu=5 theoretical = 6)"
    )


def test_annualized_vol_in_range(df: pd.DataFrame) -> None:
    for regime, target in TARGET_ANN_VOL.items():
        r = df.loc[df["regime"] == regime, "ret"].to_numpy()
        ann_vol = float(np.std(r, ddof=1) * np.sqrt(ANNUALIZATION))
        lo, hi = target * 0.7, target * 1.3
        assert lo <= ann_vol <= hi, (
            f"{regime} ann_vol = {ann_vol:.4f}; target = {target:.4f} "
            f"(allowed range [{lo:.4f}, {hi:.4f}])"
        )


# --------------------------------------------------------------------------- #
# Symmetric autocorrelation coverage — trending and fat_tailed should NOT
# show significant autocorrelation (only mean_reverting and choppy were
# previously tested). Catches generator regressions where an AR(1) bug
# leaks into the wrong regime.
# --------------------------------------------------------------------------- #
def test_trending_no_significant_autocorr(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "trending", "ret"].to_numpy()
    ac1 = _lag1_autocorr(r)
    assert -0.05 <= ac1 <= 0.05, f"trending lag-1 autocorr = {ac1:.4f} (expected in [-0.05, 0.05])"


def test_fat_tailed_no_significant_autocorr(df: pd.DataFrame) -> None:
    r = df.loc[df["regime"] == "fat_tailed", "ret"].to_numpy()
    ac1 = _lag1_autocorr(r)
    assert -0.05 <= ac1 <= 0.05, (
        f"fat_tailed lag-1 autocorr = {ac1:.4f} (expected in [-0.05, 0.05])"
    )


# --------------------------------------------------------------------------- #
# Schema-shape guard for downstream long→wide pivot consumers.
# --------------------------------------------------------------------------- #
def test_timestamps_aligned_across_regimes(df: pd.DataFrame) -> None:
    """All regimes must have identical timestamp series — long→wide pivots
    assume timestamp alignment; this test guards against a generator bug
    that desynchronizes them."""
    ts_per_regime = {
        regime: df.loc[df["regime"] == regime, "ts"].reset_index(drop=True)
        for regime in EXPECTED_REGIMES
    }
    reference = ts_per_regime["trending"]
    for regime, ts in ts_per_regime.items():
        assert ts.equals(reference), f"regime {regime!r} ts not aligned to trending"


# --------------------------------------------------------------------------- #
# Determinism test — re-run generator into a scratch dir, hash, compare.
# --------------------------------------------------------------------------- #
def test_seed_is_deterministic(tmp_path: Path) -> None:
    """Re-run the generator into a temp copy of the repo and confirm bytes match.

    We run the generator as a subprocess so it executes the actual production
    path (parquet writer settings + seed). The generator writes relative to
    its own __file__, so we copy the script into a temporary tree.
    """
    scratch = tmp_path / "propfarm"
    (scratch / "scripts").mkdir(parents=True)
    (scratch / "fixtures").mkdir()
    target_script = scratch / "scripts" / "generate_synthetic_returns.py"
    target_script.write_bytes(GENERATOR_PATH.read_bytes())

    result = subprocess.run(
        [sys.executable, str(target_script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"generator failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    regenerated = scratch / "fixtures" / "synthetic_returns.parquet"
    assert regenerated.exists()
    h_new = hashlib.sha256(regenerated.read_bytes()).hexdigest()
    h_canonical = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert h_new == h_canonical, f"determinism broken: rerun={h_new} canonical={h_canonical}"


def test_regenerate_in_place_is_noop() -> None:
    """Safety net: an accidental `python scripts/generate_synthetic_returns.py`
    from repo root must not change the canonical bytes. Test backs up the
    fixture, runs the generator (which overwrites at FIXTURE_PATH), confirms
    the bytes match, and restores defensively in case bytes ever diverge."""
    import subprocess as sp

    backup_parquet = FIXTURE_PATH.read_bytes()
    backup_sha256 = SHA256_PATH.read_text(encoding="utf-8")
    try:
        result = sp.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"generator failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        after_parquet = FIXTURE_PATH.read_bytes()
        after_sha256 = SHA256_PATH.read_text(encoding="utf-8")
        assert after_parquet == backup_parquet, (
            "in-place regeneration changed parquet bytes — determinism violated"
        )
        assert after_sha256 == backup_sha256, "in-place regeneration changed manifest"
    finally:
        # Defensive restore even on assertion failure.
        FIXTURE_PATH.write_bytes(backup_parquet)
        SHA256_PATH.write_text(backup_sha256, encoding="utf-8")
