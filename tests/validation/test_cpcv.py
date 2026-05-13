"""Tests for the CPCV harness (Task 8.1).

Covers:
* Combinatorial split count = C(n_groups, k_test).
* Train/test disjointness per split.
* Coverage: every timestamp index appears in >= 1 test set.
* Purge invariant under non-zero label_horizon.
* Embargo invariant under non-zero embargo_frac.
* Behavior on each of the 4 fixture regimes (trending, mean_reverting,
  choppy, fat_tailed) — the serial-dependent regimes (trending,
  mean_reverting) are where purge correctness is load-bearing.
* SHA256 fixture pin.
* Property test (hypothesis) over random valid parameter combinations.
* `evaluate(...)` entry point shape and error semantics.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as hyp

from propfarm.validation.cpcv import (
    CpcvResult,
    CpcvSplit,
    combinatorial_purged_split,
    evaluate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"
SHA256_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.sha256"
REGIMES = ("trending", "mean_reverting", "choppy", "fat_tailed")
SERIAL_REGIMES = ("trending", "mean_reverting")


# --------------------------------------------------------------------------- #
# Fixture loader — verifies SHA256 on every call. Test-local: the canonical
# helper would live in propfarm.data.fixtures if we needed it cross-module,
# but right now CPCV is the first W5 consumer to land. Keep it inline so
# parallel W5 agents don't race on a shared helper file.
# --------------------------------------------------------------------------- #
def _verify_fixture_sha256() -> None:
    """Raise RuntimeError if the parquet bytes don't match the pinned hash."""
    expected = SHA256_PATH.read_text().strip()
    h = hashlib.sha256()
    with FIXTURE_PATH.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"fixture sha256 drift! expected={expected} actual={actual} — "
            "do NOT regenerate; investigate why the parquet bytes diverged."
        )


@pytest.fixture(scope="module")
def long_df() -> pd.DataFrame:
    """Load canonical fixture once, hash-verified."""
    _verify_fixture_sha256()
    df: pd.DataFrame = pq.read_table(FIXTURE_PATH).to_pandas()  # type: ignore[no-untyped-call]
    return df


@pytest.fixture(scope="module")
def regime_returns(long_df: pd.DataFrame) -> dict[str, np.ndarray]:
    """One numpy array per regime, in fixture order."""
    return {
        r: long_df.loc[long_df["regime"] == r, "ret"].to_numpy(dtype=np.float64) for r in REGIMES
    }


# --------------------------------------------------------------------------- #
# 1. n_splits is combinatorial.
# --------------------------------------------------------------------------- #
def test_n_splits_is_combinatorial() -> None:
    splits = list(combinatorial_purged_split(n_obs=1000, n_groups=6, k_test=2))
    assert len(splits) == math.comb(6, 2) == 15


# --------------------------------------------------------------------------- #
# 2. Train / test disjoint per split.
# --------------------------------------------------------------------------- #
def test_train_test_disjoint_per_split() -> None:
    for split in combinatorial_purged_split(n_obs=1000, n_groups=6, k_test=2):
        overlap = np.intersect1d(split.train_idx, split.test_idx)
        assert overlap.size == 0, f"overlap found: {overlap[:5]}..."


# --------------------------------------------------------------------------- #
# 3. Every index covered by at least one test set.
# --------------------------------------------------------------------------- #
def test_every_index_covered() -> None:
    n_obs = 1000
    seen: set[int] = set()
    for split in combinatorial_purged_split(n_obs=n_obs, n_groups=6, k_test=2):
        seen.update(int(i) for i in split.test_idx)
    assert seen == set(range(n_obs))


# --------------------------------------------------------------------------- #
# 4. Purging removes boundary overlap.
# --------------------------------------------------------------------------- #
def test_purging_removes_boundary_overlap() -> None:
    horizon = 5
    for split in combinatorial_purged_split(
        n_obs=1000, n_groups=6, k_test=2, embargo_frac=0.0, label_horizon=horizon
    ):
        if split.train_idx.size == 0:
            continue
        # For every train index, distance to nearest test index must be > horizon.
        # Vectorized: take the diff matrix lazily by sorting and using searchsorted.
        test_sorted = np.sort(split.test_idx)
        # For each train idx, nearest left/right test idx.
        positions = np.searchsorted(test_sorted, split.train_idx)
        # Left neighbor: positions - 1 (where valid).
        left_valid = positions > 0
        right_valid = positions < test_sorted.size
        # Distances to neighbors (inf where no neighbor exists).
        dist_left = np.where(
            left_valid,
            split.train_idx - test_sorted[np.clip(positions - 1, 0, test_sorted.size - 1)],
            np.iinfo(np.int64).max,
        )
        dist_right = np.where(
            right_valid,
            test_sorted[np.clip(positions, 0, test_sorted.size - 1)] - split.train_idx,
            np.iinfo(np.int64).max,
        )
        min_dist = np.minimum(dist_left, dist_right)
        assert int(min_dist.min()) > horizon, (
            f"purge violated: nearest train→test distance {int(min_dist.min())} <= {horizon}"
        )


# --------------------------------------------------------------------------- #
# 5. Embargo removes post-test indices.
# --------------------------------------------------------------------------- #
def test_embargo_removes_post_test() -> None:
    n_obs = 1000
    embargo_frac = 0.02  # → 20 indices
    embargo_size = int(embargo_frac * n_obs)
    assert embargo_size == 20
    for split in combinatorial_purged_split(
        n_obs=n_obs, n_groups=6, k_test=2, embargo_frac=embargo_frac, label_horizon=0
    ):
        # Identify contiguous test runs.
        test_sorted = np.sort(split.test_idx)
        diffs = np.diff(test_sorted)
        break_pos = np.flatnonzero(diffs > 1)
        run_ends = np.concatenate((test_sorted[break_pos], test_sorted[-1:])) + 1
        train_set = set(int(i) for i in split.train_idx)
        for run_end in run_ends:
            forbidden = range(int(run_end), min(n_obs, int(run_end) + embargo_size))
            offending = [i for i in forbidden if i in train_set]
            # Note: indices inside `forbidden` that are themselves part of
            # another test run are excluded from train_set already; we only
            # require that none of them leak into training.
            assert not offending, (
                f"embargo leak after run ending at {run_end}: indices {offending[:5]} in training"
            )


# --------------------------------------------------------------------------- #
# 6. Fixture load + per-regime split sanity.
# --------------------------------------------------------------------------- #
def test_fixture_load_and_split_per_regime(regime_returns: dict[str, np.ndarray]) -> None:
    for regime, r in regime_returns.items():
        result = evaluate(r, n_groups=6, k_test=2, embargo_frac=0.01, label_horizon=0)
        assert result.n_splits == math.comb(6, 2) == 15, regime
        assert len(result.splits) == 15, regime
        for split in result.splits:
            overlap = np.intersect1d(split.train_idx, split.test_idx)
            assert overlap.size == 0, (regime, overlap[:5])
        # Mean (train + test) <= n_obs because of purge + embargo gap.
        mean_train = np.mean([s.train_idx.size for s in result.splits])
        mean_test = np.mean([s.test_idx.size for s in result.splits])
        assert mean_train + mean_test <= result.n_total, regime


# --------------------------------------------------------------------------- #
# 7. SHA256 pinned.
# --------------------------------------------------------------------------- #
def test_fixture_sha256_pinned() -> None:
    # The loader is the assertion; if it doesn't raise, the hash matched.
    _verify_fixture_sha256()


# --------------------------------------------------------------------------- #
# 8. Serial regimes respect purging — load-bearing invariant.
# --------------------------------------------------------------------------- #
def test_serial_regimes_respect_purging(regime_returns: dict[str, np.ndarray]) -> None:
    horizon = 10
    for regime in SERIAL_REGIMES:
        r = regime_returns[regime]
        for split in combinatorial_purged_split(
            n_obs=r.size,
            n_groups=6,
            k_test=2,
            embargo_frac=0.0,
            label_horizon=horizon,
        ):
            if split.train_idx.size == 0:
                continue
            test_sorted = np.sort(split.test_idx)
            positions = np.searchsorted(test_sorted, split.train_idx)
            left_valid = positions > 0
            right_valid = positions < test_sorted.size
            dist_left = np.where(
                left_valid,
                split.train_idx - test_sorted[np.clip(positions - 1, 0, test_sorted.size - 1)],
                np.iinfo(np.int64).max,
            )
            dist_right = np.where(
                right_valid,
                test_sorted[np.clip(positions, 0, test_sorted.size - 1)] - split.train_idx,
                np.iinfo(np.int64).max,
            )
            min_dist = int(np.minimum(dist_left, dist_right).min())
            assert min_dist > horizon, (
                f"{regime}: purge violated, min train→test distance {min_dist} <= horizon {horizon}"
            )


# --------------------------------------------------------------------------- #
# 9. Property test: random valid parameter combinations.
# --------------------------------------------------------------------------- #
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    n_groups=hyp.integers(min_value=3, max_value=10),
    k_test_pick=hyp.integers(min_value=1, max_value=9),
    n_obs=hyp.integers(min_value=50, max_value=2000),
    label_horizon=hyp.integers(min_value=0, max_value=50),
    embargo_frac=hyp.floats(min_value=0.0, max_value=0.05, allow_nan=False),
)
def test_property_no_self_test_leak(
    n_groups: int,
    k_test_pick: int,
    n_obs: int,
    label_horizon: int,
    embargo_frac: float,
) -> None:
    # Constrain k_test < n_groups.
    k_test = ((k_test_pick - 1) % (n_groups - 1)) + 1
    # Ensure n_obs >= n_groups; label_horizon < n_obs.
    if n_obs < n_groups:
        n_obs = n_groups
    if label_horizon >= n_obs:
        label_horizon = max(0, n_obs - 1)

    seen: set[int] = set()
    for split in combinatorial_purged_split(
        n_obs=n_obs,
        n_groups=n_groups,
        k_test=k_test,
        embargo_frac=embargo_frac,
        label_horizon=label_horizon,
    ):
        # Disjoint.
        overlap = np.intersect1d(split.train_idx, split.test_idx)
        assert overlap.size == 0
        seen.update(int(i) for i in split.test_idx)

        # Purge invariant (only if there is training data AND horizon>0).
        if label_horizon > 0 and split.train_idx.size > 0:
            test_sorted = np.sort(split.test_idx)
            positions = np.searchsorted(test_sorted, split.train_idx)
            left_valid = positions > 0
            right_valid = positions < test_sorted.size
            dist_left = np.where(
                left_valid,
                split.train_idx - test_sorted[np.clip(positions - 1, 0, test_sorted.size - 1)],
                np.iinfo(np.int64).max,
            )
            dist_right = np.where(
                right_valid,
                test_sorted[np.clip(positions, 0, test_sorted.size - 1)] - split.train_idx,
                np.iinfo(np.int64).max,
            )
            min_dist = int(np.minimum(dist_left, dist_right).min())
            assert min_dist > label_horizon, (
                f"property purge fail: min_dist={min_dist} horizon={label_horizon}"
            )

        # Embargo invariant: any index in [run_end, run_end+embargo_size)
        # must not appear in train_idx.
        embargo_size = int(embargo_frac * n_obs)
        if embargo_size > 0:
            test_sorted = np.sort(split.test_idx)
            diffs = np.diff(test_sorted)
            break_pos = np.flatnonzero(diffs > 1)
            run_ends = (
                np.concatenate((test_sorted[break_pos], test_sorted[-1:])) + 1
                if test_sorted.size
                else np.empty(0, dtype=np.int64)
            )
            train_set = set(int(i) for i in split.train_idx)
            for run_end in run_ends:
                for idx in range(int(run_end), min(n_obs, int(run_end) + embargo_size)):
                    assert idx not in train_set, (
                        f"embargo leak idx={idx} run_end={run_end} embargo_size={embargo_size}"
                    )

    # Every index in at least one test set.
    assert seen == set(range(n_obs))


# --------------------------------------------------------------------------- #
# 10. evaluate() returns a CpcvResult with expected shape.
# --------------------------------------------------------------------------- #
def test_evaluate_returns_cpcvresult() -> None:
    # Test-local synthetic series, NOT a fixture replacement — narrow shape check.
    rng = np.random.default_rng(seed=12345)
    returns = rng.normal(0.0, 0.01, size=1000)
    result = evaluate(returns, n_groups=6, k_test=2, embargo_frac=0.01, label_horizon=5)
    assert isinstance(result, CpcvResult)
    assert result.n_total == 1000
    assert result.n_groups == 6
    assert result.k_test == 2
    assert result.n_splits == 15
    assert result.embargo_frac == 0.01
    assert result.purge_size == 5
    assert isinstance(result.splits, tuple)
    assert len(result.splits) == 15
    assert all(isinstance(s, CpcvSplit) for s in result.splits)
    assert all(s.train_idx.dtype == np.int64 for s in result.splits)
    assert all(s.test_idx.dtype == np.int64 for s in result.splits)
    # Coverage hist sized to n_total.
    assert set(result.coverage.keys()) == set(range(1000))
    # Every index covered (>=1).
    assert min(result.coverage.values()) >= 1
    # Each index appears in exactly C(n_groups - 1, k_test - 1) = C(5, 1) = 5 test sets.
    assert all(v == math.comb(5, 1) for v in result.coverage.values())


# --------------------------------------------------------------------------- #
# 11. evaluate() raises on insufficient data.
# --------------------------------------------------------------------------- #
def test_evaluate_raises_on_insufficient_data() -> None:
    with pytest.raises(ValueError, match="insufficient data"):
        evaluate(np.array([1.0, 2.0]), n_groups=6, k_test=2)


# --------------------------------------------------------------------------- #
# 12. evaluate() on all-zero (degenerate) input still produces a valid result.
#     CPCV is data-agnostic — only downstream metric layers would NaN.
# --------------------------------------------------------------------------- #
def test_evaluate_degenerate_returns_nan_result() -> None:
    zeros = np.zeros(1000, dtype=np.float64)
    result = evaluate(zeros, n_groups=6, k_test=2, embargo_frac=0.01, label_horizon=0)
    assert isinstance(result, CpcvResult)
    assert result.n_total == 1000
    assert result.n_splits == 15
    # Splits are well-formed and cover the index range.
    seen: set[int] = set()
    for s in result.splits:
        seen.update(int(i) for i in s.test_idx)
    assert seen == set(range(1000))
    # Diagnostics populated.
    assert result.purge_size == 0
    assert result.embargo_frac == 0.01
    # Coverage shape sane.
    assert min(result.coverage.values()) >= 1


# --------------------------------------------------------------------------- #
# Bonus: per-regime smoke through evaluate() with non-zero purge + embargo,
# proving the load-bearing combination works on every regime separately.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("regime", REGIMES)
def test_evaluate_per_regime_with_purge_and_embargo(
    regime_returns: dict[str, np.ndarray], regime: str
) -> None:
    r = regime_returns[regime]
    result = evaluate(r, n_groups=6, k_test=2, embargo_frac=0.01, label_horizon=5)
    assert result.n_splits == 15
    assert min(result.coverage.values()) >= 1
    # Disjointness + purge invariant per split.
    for split in result.splits:
        if split.train_idx.size == 0:
            continue
        overlap = np.intersect1d(split.train_idx, split.test_idx)
        assert overlap.size == 0
        test_sorted = np.sort(split.test_idx)
        positions = np.searchsorted(test_sorted, split.train_idx)
        left_valid = positions > 0
        right_valid = positions < test_sorted.size
        dist_left = np.where(
            left_valid,
            split.train_idx - test_sorted[np.clip(positions - 1, 0, test_sorted.size - 1)],
            np.iinfo(np.int64).max,
        )
        dist_right = np.where(
            right_valid,
            test_sorted[np.clip(positions, 0, test_sorted.size - 1)] - split.train_idx,
            np.iinfo(np.int64).max,
        )
        min_dist = int(np.minimum(dist_left, dist_right).min())
        assert min_dist > 5, (regime, min_dist)
