"""Probability of Backtest Overfitting (PBO) via Combinatorially-Symmetric
Cross-Validation — Task 9.2.

Why this module exists
----------------------
Sharpe ratios obtained from picking the **best** of ``N_params`` parameter
sets on an in-sample backtest are systematically inflated even when the
underlying strategies have zero edge: with enough trials, *some* parameter
configuration will look stellar by chance. The Probability of Backtest
Overfitting (PBO), introduced by Bailey, Borwein, López de Prado and Zhu
(2017, "The Probability of Backtest Overfitting"), estimates the
probability that the parameter set ranked **first** by in-sample (IS)
Sharpe will perform **below the median** on out-of-sample (OOS) data.

The Phase-3 deploy gate for the prop-farm system uses ``PBO < 0.5``
together with ``DSR > 0.95``; the two together form the bulk of the
"ready to deploy" verdict. A PBO close to 1 means the IS-best param is
no better (and likely worse) than a coin flip OOS — the strategy family
is overfit, and any single parameter choice from it is essentially noise.

Algorithm: Combinatorially-Symmetric Cross-Validation (CSCV)
------------------------------------------------------------
Input is a Sharpe matrix ``M`` of shape ``(N_periods, N_params)`` where
``M[t, k]`` is the realized Sharpe of param ``k`` over the ``t``-th
walk-forward sub-period. The matrix is produced upstream by walk-forward
or CPCV; this module is data-agnostic about that step.

For every combination of ``N_periods // 2`` rows treated as the IS set
(the remaining rows are OOS):

1. ``IS_mean[k]`` = mean Sharpe of param ``k`` over the IS rows.
2. ``OOS_mean[k]`` = mean Sharpe of param ``k`` over the OOS rows.
3. ``k*`` = ``argmax(IS_mean)`` — the param a researcher would pick.
4. Rank ``k*`` against all other params by their ``OOS_mean``. Convert to
   the **OOS-relative-rank** ``r = (rank + 1) / (N_params + 1)`` where
   ``rank`` is the 0-indexed position of ``k*`` in the ascending OOS
   ordering (i.e. ``rank = 0`` means ``k*`` is OOS-worst,
   ``rank = N_params - 1`` means ``k*`` is OOS-best). The
   ``+1 / +1`` open-interval offset (the canonical Bailey-Borwein
   convention) keeps ``r ∈ (0, 1)`` so the logit transform never blows up
   even before clipping.
5. ``logit(r) = log(r / (1 - r))``. ``PBO`` = fraction of combinations
   where ``logit(r) < 0`` ↔ ``r < 0.5`` ↔ IS-best param landed below
   OOS median.

With ``N_periods = 16`` (the recommended setting; the symmetric ``8/8``
split is statistically rich), there are ``C(16, 8) = 12,870``
combinations — manageable in pure numpy.

Design choices
--------------
* **Sharpe matrix in, not raw returns.** PBO is downstream of the
  walk-forward aggregator. We accept the matrix so this module can be
  tested with synthetic matrices that exercise the algorithm corners
  (perfect correlation, anti-correlation, pure noise) independently of
  any strategy or return generator. A helper
  :func:`sharpes_from_returns_and_grid` builds the matrix from a 1-D
  return series + a moving-average lookback grid for the regime tests.
* **Symmetric split only.** Bailey-Borwein require ``|IS| == |OOS|`` for
  the logit-rank distribution to be symmetric around zero under the null
  (random rankings). We enforce ``N_periods`` even with a clear error.
* **Open-interval rank formula.** ``(rank + 1) / (N_params + 1)``
  guarantees ``r ∈ (0, 1)`` so ``log(r/(1-r))`` is always finite. A
  belt-and-braces clip at ``ε = 1 / (N_params * 100)`` defends against
  any future variant of the formula that might produce a boundary value.
* **Pydantic frozen result with both raw arrays.** The downstream PBO
  diagnostic plot (logit-distribution histogram) needs the per-combo
  rank and logit; we expose both so the caller never has to re-run the
  12,870-combination loop just to render a chart.
* **Numpy + scipy.special only.** Matches the W5 contract.

Public API
----------
* :class:`PboResult`
* :func:`probability_of_backtest_overfitting`
* :func:`sharpes_from_returns_and_grid`
* :func:`evaluate`
"""

from __future__ import annotations

from itertools import combinations
from math import comb
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

__all__ = [
    "PboResult",
    "evaluate",
    "probability_of_backtest_overfitting",
    "sharpes_from_returns_and_grid",
]


# --------------------------------------------------------------------------- #
# Module-level constants.
# --------------------------------------------------------------------------- #

#: Minimum number of walk-forward periods needed for a meaningful CSCV.
#: The symmetric ``N/2`` split must produce at least 2 IS + 2 OOS rows; any
#: less and the rankings are degenerate (a 1-row IS gives the same
#: argmax as picking the IS-best from a single sample).
MIN_N_PERIODS: Final[int] = 4

#: Annualization factor for daily returns. Used only by the helper
#: :func:`sharpes_from_returns_and_grid` so the synthetic Sharpe matrices
#: built for the regime tests are on a familiar scale.
ANNUALIZATION_FACTOR: Final[float] = float(np.sqrt(252.0))


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
class PboResult(BaseModel):
    """Output of :func:`evaluate` and :func:`probability_of_backtest_overfitting`.

    Attributes
    ----------
    pbo : float
        Probability of Backtest Overfitting ∈ ``[0, 1]``. Defined as the
        fraction of CSCV combinations in which the IS-best parameter
        landed in the lower half of the OOS performance distribution.
        ``NaN`` if every combination produces a degenerate ranking (e.g.
        all-zero Sharpe matrix where every param ties every other param —
        the median itself is undefined).
    n_periods : int
        Number of walk-forward periods (rows) in the input Sharpe matrix.
    n_params : int
        Number of parameter sets (columns) in the input Sharpe matrix.
    n_combinations : int
        ``C(n_periods, n_periods // 2)``. With ``n_periods = 16`` →
        ``12,870``.
    oos_relative_ranks : numpy.ndarray
        1-D ``float64`` array of length ``n_combinations``. Element ``c``
        is ``(rank + 1) / (n_params + 1)`` where ``rank`` is the
        0-indexed position of the IS-best parameter in the ascending
        OOS-mean Sharpe ordering for combination ``c``. Always in
        ``(0, 1)`` by construction; ``NaN`` for combinations whose IS
        Sharpes are all identical (no unique argmax) or whose OOS
        Sharpes are all identical (no rank possible).
    oos_logits : numpy.ndarray
        1-D ``float64`` array of length ``n_combinations``. Element ``c``
        is ``log(r / (1 - r))`` where ``r = oos_relative_ranks[c]``. By
        the symmetry of CSCV under the null (random rankings) this
        distribution is symmetric around zero; PBO is the area to the
        left of zero. Exposed so a downstream diagnostic plot can render
        the distribution without re-running PBO.

    Notes
    -----
    ``model_config`` sets ``frozen=True`` to make the result immutable,
    and ``arbitrary_types_allowed=True`` so pydantic accepts the numpy
    arrays without coercion.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pbo: float
    n_periods: int
    n_params: int
    n_combinations: int
    oos_relative_ranks: np.ndarray
    oos_logits: np.ndarray


# --------------------------------------------------------------------------- #
# Core algorithm
# --------------------------------------------------------------------------- #
def probability_of_backtest_overfitting(
    sharpe_matrix: np.ndarray,
) -> PboResult:
    """Compute PBO via Bailey-Borwein-LdP-Zhu (2017) CSCV.

    Steps
    -----
    1. Validate input shape and parity (``n_periods`` must be even and
       ``>= MIN_N_PERIODS``; ``n_params >= 2``).
    2. Enumerate all ``C(n_periods, n_periods // 2)`` symmetric splits
       of the rows into IS and OOS sets.
    3. For each split:

       a. ``IS_mean[k]`` = mean of column ``k`` over IS rows.
       b. ``OOS_mean[k]`` = mean of column ``k`` over OOS rows.
       c. ``k*`` = ``argmax(IS_mean)``. If ``IS_mean`` is constant
          (all-zero matrix, every param ties), record ``NaN``.
       d. Rank ``k*`` against the OOS-mean ordering. We use **ascending**
          rank so ``rank = 0`` means "worst OOS" and
          ``rank = n_params - 1`` means "best OOS". If OOS-mean is
          constant, record ``NaN``.
       e. ``r = (rank + 1) / (n_params + 1)`` ∈ ``(0, 1)``.

    4. Clip ``r`` to ``(ε, 1 - ε)`` with ``ε = 1 / (n_params * 100)``,
       per Bailey-Borwein guard against degenerate boundary values.
    5. ``logit = log(r / (1 - r))``.
    6. ``PBO = mean(logit < 0)`` over all non-NaN combinations. If every
       combination is NaN (degenerate input), return ``PBO = NaN``.

    Parameters
    ----------
    sharpe_matrix : numpy.ndarray
        2-D ``float`` array of shape ``(n_periods, n_params)``. Element
        ``[t, k]`` is the realized Sharpe of parameter ``k`` over
        walk-forward period ``t``.

    Returns
    -------
    PboResult

    Raises
    ------
    ValueError
        If ``sharpe_matrix`` is not 2-D, if ``n_periods < MIN_N_PERIODS``,
        if ``n_periods`` is odd (CSCV requires an even split), if
        ``n_params < 2``, or if the matrix contains non-finite values
        (``NaN``/``inf``) outside of all-zero degenerate detection.
    """
    arr = np.asarray(sharpe_matrix, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"sharpe_matrix must be 2-D, got shape {arr.shape}")

    n_periods, n_params = arr.shape
    if n_periods < MIN_N_PERIODS:
        raise ValueError(
            f"n_periods must be >= {MIN_N_PERIODS}, got {n_periods}. "
            "CSCV needs at least 2 IS + 2 OOS rows for a non-degenerate ranking."
        )
    if n_periods % 2 != 0:
        raise ValueError(f"n_periods must be even (symmetric CSCV split); got {n_periods}.")
    if n_params < 2:
        raise ValueError(
            f"n_params must be >= 2, got {n_params}. PBO compares the IS-best "
            "against the rest of the param grid; one column is trivially rank 0."
        )
    if not np.all(np.isfinite(arr)):
        raise ValueError(
            "sharpe_matrix contains non-finite values (NaN or inf). "
            "Filter or impute upstream before computing PBO."
        )

    half = n_periods // 2
    all_rows = np.arange(n_periods)
    n_combinations = comb(n_periods, half)

    eps = 1.0 / (n_params * 100.0)

    oos_relative_ranks = np.empty(n_combinations, dtype=np.float64)
    oos_logits = np.empty(n_combinations, dtype=np.float64)

    for c_idx, is_combo in enumerate(combinations(range(n_periods), half)):
        is_rows = np.asarray(is_combo, dtype=np.int64)
        # OOS rows are the set complement; ordered ascending.
        oos_rows = np.setdiff1d(all_rows, is_rows, assume_unique=True)

        is_mean = arr[is_rows].mean(axis=0)
        oos_mean = arr[oos_rows].mean(axis=0)

        # Degenerate-combo detection. If every IS or every OOS column-mean
        # is identical, the argmax / rank is undefined; record NaN and
        # carry on.
        if np.ptp(is_mean) == 0.0 or np.ptp(oos_mean) == 0.0:
            oos_relative_ranks[c_idx] = np.nan
            oos_logits[c_idx] = np.nan
            continue

        # IS-best param: ties broken by numpy's left-most argmax (stable).
        k_star = int(np.argmax(is_mean))

        # Ascending OOS rank of k_star. `argsort` gives the indices in
        # increasing-OOS-mean order; `searchsorted`-equivalent below uses
        # the fact that rank = number of params with strictly smaller
        # OOS_mean, plus half the count of ties with k_star. We use the
        # canonical ordinal rank to keep results deterministic.
        oos_order = np.argsort(oos_mean, kind="stable")
        # Position of k_star in the ascending order:
        rank = int(np.flatnonzero(oos_order == k_star)[0])

        # Open-interval relative rank in (0, 1).
        r = (rank + 1.0) / (n_params + 1.0)
        # Belt-and-braces clip — formula above already keeps r ∈ (0, 1),
        # but explicit clip protects against future formula edits.
        r_clipped = float(np.clip(r, eps, 1.0 - eps))

        oos_relative_ranks[c_idx] = r_clipped
        oos_logits[c_idx] = float(np.log(r_clipped / (1.0 - r_clipped)))

    valid = ~np.isnan(oos_logits)
    pbo = float("nan") if not valid.any() else float(np.mean(oos_logits[valid] < 0.0))

    return PboResult(
        pbo=pbo,
        n_periods=n_periods,
        n_params=n_params,
        n_combinations=n_combinations,
        oos_relative_ranks=oos_relative_ranks,
        oos_logits=oos_logits,
    )


# --------------------------------------------------------------------------- #
# Helper: Sharpe matrix from returns + MA-crossover lookback grid.
# --------------------------------------------------------------------------- #
def sharpes_from_returns_and_grid(
    returns: np.ndarray,
    lookbacks: tuple[int, ...],
    n_periods: int,
) -> np.ndarray:
    """Build a ``(n_periods, len(lookbacks))`` Sharpe matrix for testing.

    Slices ``returns`` into ``n_periods`` contiguous, equal-length
    windows. For each window and each ``lookback`` value in the grid,
    simulates a deterministic moving-average filter strategy:

    * Build a centred-zero signal ``s_t = sign(mean(returns[t-L:t]))``
      from the prior ``L`` bars of returns (a momentum / MA-of-returns
      indicator).
    * Strategy returns ``r_t = s_{t-1} * returns[t]`` — lagged by one bar
      so there is no look-ahead.
    * Annualized Sharpe ``= sqrt(252) * mean(r) / std(r, ddof=1)``,
      ``0`` if ``std == 0`` (constant strategy).

    The point of this helper is to give the regime tests a deterministic
    Sharpe matrix derived from the fixture's returns, so we can ask
    whether PBO behaves as expected per regime (trending should have
    real signal → lower PBO than choppy).

    Parameters
    ----------
    returns : numpy.ndarray
        1-D array of per-bar returns. Length must be at least
        ``n_periods * (max(lookbacks) + 2)`` so every window has enough
        history for the longest lookback.
    lookbacks : tuple[int, ...]
        Strictly positive integer MA lookback lengths. Each becomes one
        column of the output matrix.
    n_periods : int
        Number of contiguous time windows to split ``returns`` into.

    Returns
    -------
    numpy.ndarray
        Float64 array of shape ``(n_periods, len(lookbacks))``.

    Raises
    ------
    ValueError
        If ``returns`` is not 1-D, ``lookbacks`` is empty, any lookback
        is < 1, ``n_periods < 1``, or ``returns`` is too short for the
        requested grid.
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    if len(lookbacks) == 0:
        raise ValueError("lookbacks must contain at least one element.")
    if any(lb < 1 for lb in lookbacks):
        raise ValueError(f"lookbacks must all be >= 1; got {lookbacks}")
    if n_periods < 1:
        raise ValueError(f"n_periods must be >= 1, got {n_periods}")

    max_lb = max(lookbacks)
    window_size = arr.size // n_periods
    if window_size < max_lb + 2:
        raise ValueError(
            f"returns too short: window_size={window_size} but max(lookbacks)+2="
            f"{max_lb + 2}. Increase returns length or decrease n_periods."
        )

    matrix = np.empty((n_periods, len(lookbacks)), dtype=np.float64)
    for t in range(n_periods):
        window = arr[t * window_size : (t + 1) * window_size]
        for k, lb in enumerate(lookbacks):
            # Trailing mean of returns over the prior `lb` bars, taking
            # the sign as our position. Lag by 1 so today's position
            # depends on returns up to yesterday — no look-ahead.
            cumsum = np.cumsum(window)
            trailing_sum = cumsum[lb:] - cumsum[:-lb]
            signal = np.sign(trailing_sum)
            # Lagged signal applied to the contemporaneous return.
            strat = signal[:-1] * window[lb + 1 :]
            std = strat.std(ddof=1)
            if std == 0.0 or not np.isfinite(std):
                matrix[t, k] = 0.0
            else:
                matrix[t, k] = ANNUALIZATION_FACTOR * strat.mean() / std

    return matrix


# --------------------------------------------------------------------------- #
# Top-level entry point — W5 uniform contract.
# --------------------------------------------------------------------------- #
def evaluate(sharpe_matrix: np.ndarray) -> PboResult:
    """Top-level W5 entry point. Thin wrapper around
    :func:`probability_of_backtest_overfitting`.

    Notes
    -----
    Unlike CPCV / walk-forward / DSR / Monte Carlo, which take a 1-D
    returns array, PBO takes a 2-D Sharpe matrix because the matrix is
    the *output* of walk-forward, not its input. Construct the matrix
    upstream (by running a parameter grid through the walk-forward
    harness and recording the per-period Sharpe of each param) and pass
    it here. For testing, :func:`sharpes_from_returns_and_grid` builds
    such a matrix from a 1-D return series + a lookback grid.

    Parameters
    ----------
    sharpe_matrix : numpy.ndarray
        2-D ``(n_periods, n_params)`` Sharpe matrix.

    Returns
    -------
    PboResult
    """
    return probability_of_backtest_overfitting(sharpe_matrix)
