"""Combinatorial Purged Cross-Validation (CPCV) harness — Task 8.1.

Why this module exists
----------------------
Naive k-fold cross-validation is invalid for serially-dependent financial
return series. López de Prado (`AFML §7.4`) introduces **Combinatorial
Purged Cross-Validation** to repair two specific leakage modes that
silently inflate out-of-sample Sharpe in a backtest:

1. **Label-overlap leakage** (`purging`). If a label at index ``t``
   depends on observations through ``t + horizon`` (e.g. a 5-bar forward
   return, a triple-barrier label), then training on indices that fall
   inside the test set's label window leaks future information into
   the model. The CPCV purge removes any training index whose label
   window intersects a test index.

2. **Serial-dependence leakage** (`embargo`). Even with non-overlapping
   labels, serial correlation in returns means an observation
   immediately after a test block carries information *learned during*
   the test block. The CPCV embargo removes the next
   ``floor(embargo_frac * n_obs)`` indices from training after each
   contiguous test group.

Naively splitting the four-regime fixture (which includes ``trending``
and ``mean_reverting`` series with measurable lag-1 autocorrelation)
without purge + embargo would let the validator pass strategies that
are exploiting nothing but the leak. The CPCV implementation here is
the load-bearing input to the downstream PBO and DSR gates: if purging
is broken or silently a no-op, the entire Phase-3 deploy bar becomes
meaningless.

Design choices
--------------
* **Contiguous time-ordered groups.** The fixture is time-ordered (the
  ``ts`` column is monotonic). Groups are contiguous slices so a single
  embargo window cleanly excludes "the indices right after this test
  block." Non-contiguous groups would force a per-group embargo region
  and complicate the boundary geometry without changing the statistics.
* **Combinatorial coverage.** With ``n_groups=N`` and ``k_test=K``,
  ``C(N, K)`` distinct test sets are generated. Every timestamp index
  appears in ``C(N-1, K-1)`` of them, which guarantees full coverage
  (asserted via the ``coverage`` field on :class:`CpcvResult`).
* **Numpy-only.** No pandas/polars dependency in the iterator. Caller
  passes a 1-D numpy array of returns; the iterator works on integer
  indices and the embargo / purge arithmetic stays in pure numpy.
* **Pydantic frozen result.** :class:`CpcvSplit` and :class:`CpcvResult`
  are immutable so the caller cannot accidentally mutate a split they
  iterate over multiple times.

Public API
----------
* :class:`CpcvSplit`
* :class:`CpcvResult`
* :func:`combinatorial_purged_split`
* :func:`evaluate`
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import combinations
from math import comb, floor
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

__all__ = [
    "CpcvResult",
    "CpcvSplit",
    "combinatorial_purged_split",
    "evaluate",
]


# --------------------------------------------------------------------------- #
# Module-level defaults — chosen to match López de Prado AFML §7.4 worked
# example (n_groups=6, k_test=2 → C(6,2) = 15 splits).
# --------------------------------------------------------------------------- #

#: Default number of contiguous time-ordered groups. AFML §7.4 worked example.
DEFAULT_N_GROUPS: Final[int] = 6

#: Default number of groups assigned to the test set per split. AFML §7.4.
DEFAULT_K_TEST: Final[int] = 2

#: Default embargo fraction. 1% of total length is the AFML rule-of-thumb.
DEFAULT_EMBARGO_FRAC: Final[float] = 0.01

#: Default label horizon. Zero means "labels are point-in-time and require
#: no purge"; the embargo still applies.
DEFAULT_LABEL_HORIZON: Final[int] = 0


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
class CpcvSplit(BaseModel):
    """One ``(train_idx, test_idx)`` pair produced by the CPCV iterator.

    Attributes
    ----------
    train_idx : numpy.ndarray
        1-D ``int64`` array of training indices in ascending order. Has
        been purged of any index within ``label_horizon`` timesteps of a
        test index, and has had the post-test-group embargo region
        removed.
    test_idx : numpy.ndarray
        1-D ``int64`` array of test indices in ascending order. The union
        of ``k_test`` contiguous groups (possibly non-adjacent groups
        themselves; adjacent groups inside the same split merge into one
        contiguous test block for embargo purposes).
    group_combination : tuple[int, ...]
        Which ``N``-groups were assigned to ``test`` in this split. Sorted
        ascending; length equals ``k_test``. Diagnostic only.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    train_idx: np.ndarray
    test_idx: np.ndarray
    group_combination: tuple[int, ...]


class CpcvResult(BaseModel):
    """Result of :func:`evaluate` — the full list of splits + diagnostics.

    Attributes
    ----------
    n_total : int
        Number of observations the iterator was run on.
    n_groups : int
        Number of contiguous time-ordered groups the obs were split into.
    k_test : int
        Number of groups in each test set.
    n_splits : int
        Combinatorial count = ``C(n_groups, k_test)``.
    embargo_frac : float
        Fraction of ``n_total`` removed from training immediately after
        each contiguous test block.
    splits : tuple[CpcvSplit, ...]
        All splits, in ``itertools.combinations`` order over
        ``range(n_groups)``.
    coverage : dict[int, int]
        ``{timestamp_index: number_of_splits_containing_it_in_test}``. By
        construction every index appears in exactly
        ``C(n_groups - 1, k_test - 1)`` test sets (>=1).
    purge_size : int
        The ``label_horizon`` used. Diagnostic — the number of training
        indices removed around each test boundary equals at most
        ``2 * label_horizon`` per disjoint test block.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    n_total: int
    n_groups: int
    k_test: int
    n_splits: int
    embargo_frac: float
    splits: tuple[CpcvSplit, ...]
    coverage: dict[int, int]
    purge_size: int


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _group_boundaries(n_obs: int, n_groups: int) -> list[tuple[int, int]]:
    """Split ``range(n_obs)`` into ``n_groups`` contiguous half-open slices.

    Remainder rows are distributed to the **earliest** groups (so groups
    0..r-1 each get one extra index, where r = n_obs % n_groups). This
    keeps groups time-ordered, contiguous, and as-equal-as-possible.

    Returns
    -------
    list[tuple[int, int]]
        ``[(start_0, stop_0), (start_1, stop_1), ...]`` half-open slices
        such that ``sum(stop_i - start_i) == n_obs`` and groups partition
        ``range(n_obs)``.
    """
    base, remainder = divmod(n_obs, n_groups)
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for g in range(n_groups):
        size = base + (1 if g < remainder else 0)
        boundaries.append((cursor, cursor + size))
        cursor += size
    return boundaries


def _contiguous_runs(sorted_indices: np.ndarray) -> list[tuple[int, int]]:
    """Group a sorted int array into maximal contiguous half-open runs.

    Used to identify each disjoint test block inside a split so the
    embargo can be applied after each block independently. If two
    chosen groups happen to be adjacent (e.g. groups 2 and 3 in a
    six-group split), they merge into a single contiguous test block
    and the embargo is applied only once at the merged trailing edge.

    Parameters
    ----------
    sorted_indices : np.ndarray
        1-D int array, ascending, no duplicates.

    Returns
    -------
    list[tuple[int, int]]
        ``[(start_0, stop_0), ...]`` half-open intervals covering the
        runs of consecutive integers. Empty input returns ``[]``.
    """
    if sorted_indices.size == 0:
        return []
    diffs = np.diff(sorted_indices)
    # A break starts a new run wherever consecutive indices are >1 apart.
    break_positions = np.flatnonzero(diffs > 1)
    starts = np.concatenate(([0], break_positions + 1))
    stops = np.concatenate((break_positions + 1, [sorted_indices.size]))
    return [
        (int(sorted_indices[s]), int(sorted_indices[e - 1]) + 1)
        for s, e in zip(starts, stops, strict=True)
    ]


# --------------------------------------------------------------------------- #
# Core iterator
# --------------------------------------------------------------------------- #
def combinatorial_purged_split(
    n_obs: int,
    n_groups: int,
    k_test: int,
    embargo_frac: float = DEFAULT_EMBARGO_FRAC,
    *,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
) -> Iterator[CpcvSplit]:
    """Yield CPCV ``(train_idx, test_idx)`` splits over ``range(n_obs)``.

    Algorithm (AFML §7.4):

    1. Partition ``range(n_obs)`` into ``n_groups`` contiguous
       time-ordered groups via :func:`_group_boundaries`.
    2. For each ``C(n_groups, k_test)`` choice of test-group indices:

       a. Build ``test_idx`` = union of the chosen groups.
       b. Build ``train_idx`` = ``range(n_obs)`` minus ``test_idx``.
       c. **Purge**: remove from ``train_idx`` any index within
          ``label_horizon`` timesteps of any test index (both
          directions).
       d. **Embargo**: for each contiguous run in ``test_idx``, remove
          from ``train_idx`` the indices in
          ``[run_end, run_end + floor(embargo_frac * n_obs))``. Applied
          AFTER each run, not before — leakage is post-test
          serial-dependence, not pre-test.

    3. Yield :class:`CpcvSplit` with sorted ``int64`` arrays.

    Parameters
    ----------
    n_obs : int
        Total number of observations. Must satisfy ``n_obs >= n_groups``.
    n_groups : int
        Number of contiguous time-ordered groups. Must be ``>= 2``.
    k_test : int
        Test-group count per split. Must satisfy ``1 <= k_test < n_groups``;
        ``k_test == n_groups`` would leave no training data.
    embargo_frac : float
        Fraction of ``n_obs`` to embargo after each contiguous test run.
        Must satisfy ``0 <= embargo_frac < 1``. Default 0.01.
    label_horizon : int, keyword-only
        Number of timesteps over which a label depends on future
        observations. Indices within this distance of any test index
        are purged from training. Must satisfy
        ``0 <= label_horizon < n_obs``. Default 0.

    Yields
    ------
    CpcvSplit
        One split per combination, in ``itertools.combinations`` order.

    Raises
    ------
    ValueError
        On any invalid parameter (see bounds above).
    """
    if n_obs < 1:
        raise ValueError(f"n_obs must be >= 1, got {n_obs}")
    if n_groups < 2:
        raise ValueError(f"n_groups must be >= 2, got {n_groups}")
    if not 1 <= k_test < n_groups:
        raise ValueError(
            f"k_test must satisfy 1 <= k_test < n_groups; got k_test={k_test}, n_groups={n_groups}"
        )
    if n_obs < n_groups:
        raise ValueError(
            f"n_obs ({n_obs}) must be >= n_groups ({n_groups}) — "
            "each group needs at least one observation."
        )
    if not 0.0 <= embargo_frac < 1.0:
        raise ValueError(f"embargo_frac must be in [0, 1), got {embargo_frac}")
    if not 0 <= label_horizon < n_obs:
        raise ValueError(
            f"label_horizon must be in [0, n_obs); got label_horizon={label_horizon}, n_obs={n_obs}"
        )

    boundaries = _group_boundaries(n_obs, n_groups)
    embargo_size = floor(embargo_frac * n_obs)

    for group_combo in combinations(range(n_groups), k_test):
        # Build test_idx as union of chosen groups' index ranges.
        test_parts = [np.arange(*boundaries[g], dtype=np.int64) for g in group_combo]
        test_idx = np.concatenate(test_parts) if test_parts else np.empty(0, dtype=np.int64)
        test_idx.sort()

        # Boolean mask over [0, n_obs) marking which indices remain in train.
        train_mask = np.ones(n_obs, dtype=bool)
        train_mask[test_idx] = False

        # --- Purge: drop any train index within label_horizon of any test index. ---
        # For each contiguous test run, broaden the kill window by
        # label_horizon on both sides. Doing it per-run keeps the
        # arithmetic vectorized and avoids O(n_obs * n_test) blowup.
        runs = _contiguous_runs(test_idx)
        if label_horizon > 0:
            for run_start, run_stop in runs:
                purge_lo = max(0, run_start - label_horizon)
                purge_hi = min(n_obs, run_stop + label_horizon)
                train_mask[purge_lo:purge_hi] = False

        # --- Embargo: drop next `embargo_size` indices after each run. ---
        if embargo_size > 0:
            for _, run_stop in runs:
                emb_lo = run_stop
                emb_hi = min(n_obs, run_stop + embargo_size)
                # Embargo only kills training indices; test indices in the
                # window (rare but possible when two test runs are
                # separated by <embargo_size training indices) are
                # already False in train_mask, so this assignment is
                # idempotent for them.
                train_mask[emb_lo:emb_hi] = False

        # Anything that's still True is training; test_idx is unchanged.
        train_idx = np.flatnonzero(train_mask).astype(np.int64)

        yield CpcvSplit(
            train_idx=train_idx,
            test_idx=test_idx,
            group_combination=tuple(group_combo),
        )


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def evaluate(
    returns: np.ndarray,
    *,
    n_groups: int = DEFAULT_N_GROUPS,
    k_test: int = DEFAULT_K_TEST,
    embargo_frac: float = DEFAULT_EMBARGO_FRAC,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
) -> CpcvResult:
    """Run CPCV over ``returns`` and return splits + diagnostics.

    This is the W5 uniform entry point: ``evaluate(returns, **kwargs)``
    returns a typed result. CPCV itself is data-agnostic — the returns
    values do not influence the splits, only ``len(returns)`` does — but
    we still take the array (not just the length) so the signature is
    uniform across the W5 modules and so a degenerate-data check at the
    boundary is centralized here rather than re-implemented per consumer.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of returns. Only its length matters for the splitter.
    n_groups : int, keyword-only
        See :func:`combinatorial_purged_split`. Default 6.
    k_test : int, keyword-only
        See :func:`combinatorial_purged_split`. Default 2.
    embargo_frac : float, keyword-only
        See :func:`combinatorial_purged_split`. Default 0.01.
    label_horizon : int, keyword-only
        See :func:`combinatorial_purged_split`. Default 0.

    Returns
    -------
    CpcvResult
        Frozen container holding every split plus the coverage histogram
        and the input parameters echoed back for audit.

    Raises
    ------
    ValueError
        If ``returns`` is not 1-D, or its length is less than ``n_groups``
        (no valid partition possible), or any parameter is out of bounds
        per :func:`combinatorial_purged_split`.

    Notes
    -----
    Degenerate input behavior (all-zero or NaN returns) — the CPCV
    splitter is data-agnostic: the splits depend only on ``len(returns)``.
    An all-zero array of length ``>= n_groups`` returns a valid
    :class:`CpcvResult` (with the usual splits). Only the downstream
    consumer (Sharpe, DSR, PBO) would produce NaN on it. This is
    intentional: CPCV is the partition layer, not the metric layer.
    """
    arr = np.asarray(returns)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")

    n_total = int(arr.size)
    if n_total < n_groups:
        raise ValueError(
            f"insufficient data for CPCV: n_obs={n_total} < n_groups={n_groups}. "
            "Need at least one observation per group."
        )

    splits = tuple(
        combinatorial_purged_split(
            n_obs=n_total,
            n_groups=n_groups,
            k_test=k_test,
            embargo_frac=embargo_frac,
            label_horizon=label_horizon,
        )
    )

    # Coverage histogram: how many splits contain each index in their test set.
    coverage_arr = np.zeros(n_total, dtype=np.int64)
    for s in splits:
        coverage_arr[s.test_idx] += 1
    coverage = {i: int(c) for i, c in enumerate(coverage_arr)}

    return CpcvResult(
        n_total=n_total,
        n_groups=n_groups,
        k_test=k_test,
        n_splits=comb(n_groups, k_test),
        embargo_frac=embargo_frac,
        splits=splits,
        coverage=coverage,
        purge_size=label_horizon,
    )
