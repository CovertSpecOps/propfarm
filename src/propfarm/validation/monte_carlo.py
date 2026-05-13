"""Monte Carlo block-bootstrap engine — Task 10.1.

Why this module exists
----------------------
A backtest produces *one* realized equity path. That path is the
intersection of the strategy with the *single observed sample* of market
history. Even a strategy with a real edge will exhibit a fat-tailed
*distribution* of equity outcomes when its trade returns are resampled —
and survival-first prop trading cares about the **left tail** of that
distribution, not the median. The Phase-3 deploy gate reads the
**5th-percentile equity curve** across >=10,000 bootstrap paths because
"loses funded capital on a bad draw" is what kills the business, not
"underperforms the median on an average draw."

A naive i.i.d. bootstrap (sampling each return independently) **destroys
serial dependence**: positive autocorrelation in a trending strategy
gets washed out, AR(1) mean-reversion gets washed out, volatility
clustering gets washed out. The result is a *too-optimistic* left tail
because the resampled paths are smoother than reality. The Politis &
Romano (1994) **stationary block bootstrap** fixes this by resampling
*blocks* of consecutive observations whose lengths are geometrically
distributed with a configurable mean. Block resampling preserves any
serial dependence whose lag is smaller than the block length.

The load-bearing decision is the block size. Hardcoding ``block_size=20``
or ``block_size=50`` will silently destroy serial dependence on any
series whose autocorrelation horizon is shorter (i.i.d. noise) or
longer (slowly-decaying AR(1)) than the hardcoded value. The
**Politis & White (2004) automatic optimal block length** estimator
chooses ``b`` from the empirical autocorrelation of the input series.
This module uses Politis-White as the **default** so the bootstrap
behaves correctly across the four canonical regimes in the fixture
without per-regime tuning.

Implementation notes
--------------------
* **Politis-Romano stationary bootstrap.** At each step, with
  probability ``p = 1/block_size``, jump to a uniformly-random index;
  otherwise advance by one. Series treated as circular (modulo
  ``n_obs``). Each bootstrap path has the same length as the input.

* **Politis-White optimal block length.** Implemented inline
  (no ``arch`` dependency) per Politis & White (2004) §3, using:

  1. Empirical autocorrelations ``rho_hat(k)`` from ``np.correlate``.
  2. Adaptive truncation lag ``M``: smallest ``m`` such that
     ``|rho_hat(k)| < c * sqrt(log10(n) / n)`` for ``K_n``
     consecutive lags ``k`` after ``m``, then ``M = 2 * m``.
     (``c = 2``, ``K_n = max(5, ceil(sqrt(log10(n))))`` per the
     paper.)
  3. Flat-top lag-window kernel ``lambda(t)``:
     ``1`` for ``|t|<=0.5``, ``2*(1-|t|)`` for ``0.5<|t|<=1``,
     ``0`` otherwise.
  4. For the *stationary* bootstrap:
     ``g_hat = sum_{|k|<=M} lambda(k/M) * |k| * R(k)``,
     ``d_SB = 2 * G_hat^2`` where ``G_hat = R(0) + 2 * sum_{1<=k<=M}
     lambda(k/M) * R(k)``,
     ``b_opt = (2 * g_hat^2 / d_SB)^(1/3) * n^(1/3)``.
     ``R(k)`` is the **autocovariance**, not autocorrelation. The
     ``2 * g_hat^2 / d_SB`` form is the stationary-bootstrap-specific
     constant from Politis-White Table 1.
  5. If the series is statistically indistinguishable from i.i.d.
     (no significant autocorrelation), ``M`` collapses to 0 and the
     estimator returns ``1.0`` (no block resampling needed).
  6. Capped at ``n / 2`` to avoid pathological inputs returning a
     block size larger than half the series.

* **Equity curves.** Cumulative product of ``(1 + r_t)`` starting from
  ``initial_capital``. ``np.cumprod`` — not ``np.cumsum``; the latter
  is the additive-log-return convention and would mis-state compounding
  drawdowns by a meaningful margin on multi-year horizons.

* **Max drawdown per path.** ``(equity / running_max - 1).min()``. The
  ``p5`` / ``p50`` / ``p95`` of this distribution across paths goes
  into the report.

* **Ruin probability.** ``mean(min(equity_path) <= initial_capital *
  (1 - ruin_threshold))``. Default threshold 0.20 (20% loss = ruin)
  matches the FTMO-style overall-DD limit; configurable so other
  firms' thresholds can be plugged in.

Public API
----------
* :class:`McReport`
* :func:`politis_white_optimal_block_length`
* :func:`stationary_block_bootstrap`
* :func:`evaluate`
"""

from __future__ import annotations

from math import ceil, sqrt
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

__all__ = [
    "McReport",
    "evaluate",
    "politis_white_optimal_block_length",
    "stationary_block_bootstrap",
]


# --------------------------------------------------------------------------- #
# Module-level defaults
# --------------------------------------------------------------------------- #

#: Plan headline: >=10,000 paths is the deploy-gate convergence target.
DEFAULT_N_PATHS: Final[int] = 10_000

#: 20% loss = ruin. Matches FTMO-style overall drawdown limit. Configurable.
DEFAULT_RUIN_THRESHOLD: Final[float] = 0.20

#: Default initial capital. Unit-normalized so equity curves are interpretable
#: as cumulative gross return multipliers.
DEFAULT_INITIAL_CAPITAL: Final[float] = 1.0

#: Deterministic default seed (matches sibling W5 modules' convention).
DEFAULT_SEED: Final[int] = 20260512

#: Politis & White (2004) constants for the adaptive truncation rule.
#: c is the multiplier on sqrt(log10(n)/n) defining the "small autocorr"
#: band; K_n_min is the floor on the number of consecutive lags below
#: that band required to declare m. Both follow the paper's Section 3
#: recommendations.
_PW_C: Final[float] = 2.0
_PW_K_N_MIN: Final[int] = 5


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
class McReport(BaseModel):
    """Output of :func:`evaluate` — headline percentiles + diagnostics.

    Attributes
    ----------
    n_paths : int
        Number of bootstrap paths simulated.
    n_obs : int
        Observation count per path (== ``len(returns)``).
    block_size : int
        Realized expected block size used by the stationary bootstrap.
        Mean of a geometric distribution with parameter ``p = 1/block_size``.
    block_size_source : str
        One of ``"politis_white_auto"`` (default path: estimator chose),
        ``"user_override"`` (caller passed an explicit value), or
        ``"default"`` (reserved for callers that supply ``block_size=0``;
        currently unused but retained in the API for future overrides).
    p5_equity, p50_equity, p95_equity : np.ndarray
        Shape ``(n_obs,)``. The k-th percentile of equity at each time
        step across bootstrap paths. ``p5_equity`` is the headline
        survival curve the deploy gate reads.
    max_dd_p5, max_dd_p50, max_dd_p95 : float
        Percentiles of the per-path max-drawdown distribution. Note that
        ``p5`` here is the *worst* 5th-percentile drawdown (most negative
        number across paths' max drawdowns — i.e. ``np.percentile(dd, 5)``
        where ``dd`` values are <= 0).
    ruin_prob : float
        Empirical probability that any equity path crosses below
        ``initial_capital * (1 - ruin_threshold)``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    n_paths: int
    n_obs: int
    block_size: int
    block_size_source: str
    p5_equity: np.ndarray
    p50_equity: np.ndarray
    p95_equity: np.ndarray
    max_dd_p5: float
    max_dd_p50: float
    max_dd_p95: float
    ruin_prob: float


# --------------------------------------------------------------------------- #
# Politis-White optimal block length
# --------------------------------------------------------------------------- #
def _flat_top_kernel(t: np.ndarray) -> np.ndarray:
    """Politis-White flat-top trapezoid kernel ``lambda(t)``.

    ``lambda(t) = 1`` for ``|t| <= 1/2``,
    ``lambda(t) = 2*(1 - |t|)`` for ``1/2 < |t| <= 1``,
    ``lambda(t) = 0`` for ``|t| > 1``.

    The flat-top shape gives the kernel the higher-order bias-reduction
    property that the optimal-block-length estimator depends on; a
    Bartlett (triangular) kernel would also work but converges slower.
    """
    abs_t = np.abs(t)
    out: np.ndarray = np.zeros_like(abs_t)
    flat = abs_t <= 0.5
    ramp = (abs_t > 0.5) & (abs_t <= 1.0)
    out[flat] = 1.0
    out[ramp] = 2.0 * (1.0 - abs_t[ramp])
    return out


def _empirical_autocov(x: np.ndarray, max_lag: int) -> np.ndarray:
    """Biased empirical autocovariance ``R(0), R(1), ..., R(max_lag)``.

    Biased (divisor ``n``, not ``n - k``) is the standard choice in
    Politis-White; it guarantees positive semi-definiteness of the
    resulting matrix and matches the paper's notation.
    """
    n = x.size
    x_centered = x - x.mean()
    # np.correlate(x, x, mode="full") at lag k corresponds to
    # sum_{t=0}^{n-1-k} x[t] * x[t+k]. Take only the non-negative half.
    full = np.correlate(x_centered, x_centered, mode="full")
    # full[n-1] is lag 0. Slice the non-negative-lag half.
    acov = full[n - 1 : n - 1 + max_lag + 1] / n
    return acov


def _politis_white_truncation_lag(rho: np.ndarray, n: int) -> int:
    """Choose the adaptive truncation lag ``M`` per Politis-White §3.

    Procedure:
      1. Set ``band = c * sqrt(log10(n) / n)``.
      2. Set ``K_n = max(K_n_min, ceil(sqrt(log10(n))))``.
      3. Find the smallest ``m >= 1`` such that ``|rho(m+1)|, ...,
         |rho(m+K_n)|`` are all below ``band``.
      4. Return ``M = 2 * m``.
      5. If no such ``m`` exists within the precomputed range, return
         the maximum available lag (caller will cap further).

    A return of ``0`` means there is no significant autocorrelation at
    *any* lag in the precomputed range — caller treats this as "i.i.d.,
    block_size = 1".
    """
    if n <= 1:
        return 0
    band = _PW_C * sqrt(np.log10(n) / n)
    k_n = max(_PW_K_N_MIN, ceil(sqrt(np.log10(n))))
    max_lag = rho.size - 1  # rho[0] is lag-0 == 1.0

    # The series rho[1], rho[2], ... is what we scan. We need K_n
    # consecutive lags after some m to all lie below `band`.
    abs_rho = np.abs(rho[1:])  # indices here correspond to lags 1..max_lag
    if abs_rho.size == 0:
        return 0

    # Special-case the "all small" series: if even rho[1] is below
    # the band and stays there for K_n lags, m = 0 -> M = 0.
    # (Loop below picks this up at m_candidate=0 if we let it; instead
    # we explicitly start the search at m=0 so M=0 is reachable.)
    for m in range(0, max_lag + 1 - k_n + 1):
        window = abs_rho[m : m + k_n]
        if window.size < k_n:
            break
        if np.all(window < band):
            return 2 * m
    # No m satisfies the criterion within the precomputed lags: use the
    # paper's fallback of M = max_lag (the largest lag we can support).
    return max_lag


def politis_white_optimal_block_length(
    returns: np.ndarray,
    *,
    method: Literal["mean", "variance"] = "mean",
) -> float:
    """Politis & White (2004) automatic optimal block length.

    Returns the expected block length for a **stationary bootstrap**
    given the input series' empirical autocorrelation structure. The
    return value is a ``float`` (not an integer): the stationary
    bootstrap's block lengths are geometrically distributed with mean
    ``b``, so a non-integer mean is the natural output.

    Reference: D. N. Politis and H. White (2004), "Automatic
    Block-Length Selection for the Dependent Bootstrap," *Econometric
    Reviews* 23(1), 53-70. Corrected reprint: Patton, Politis, White
    (2009), same title.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of returns. Must have ``n >= 4`` for the truncation
        rule to be meaningful; below that we return ``1.0``.
    method : {"mean", "variance"}
        Which functional the bootstrap targets. ``"mean"`` is the
        canonical setting (the empirical mean of the bootstrap
        distribution should match the empirical mean of the input). The
        ``"variance"`` choice would target the second moment and is
        retained as a stub for future use; currently both code paths
        return the same value because the stationary-bootstrap formula
        we use is the mean-targeted ``b_SB``. The argument is part of
        the public signature so callers can request variance-targeted
        in the future without an API break.

    Returns
    -------
    float
        Estimated optimal expected block length. Bounded to
        ``[1.0, n / 2]``. Returns ``1.0`` for series with no detectable
        autocorrelation (the i.i.d. degenerate case).

    Raises
    ------
    ValueError
        If ``returns`` is not 1-D or has fewer than 2 observations.
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    n = arr.size
    if n < 2:
        raise ValueError(f"need at least 2 observations, got {n}")
    if n < 4:
        # Too short to estimate anything; return iid-equivalent default.
        return 1.0

    # The mean-vs-variance distinction is the choice of target functional.
    # For mean-targeting (the default) we work directly with x. For
    # variance-targeting one would substitute x' = (x - x.mean())**2 and
    # rerun the same estimator. Politis-White Table 1 lists both. The
    # stationary-bootstrap formula b = (2 * g^2 / d_SB)^(1/3) * n^(1/3)
    # is identical for both; only the input series differs.
    x = (arr - arr.mean()) ** 2 if method == "variance" else arr

    # Precompute autocorrelations up to ceil(min(n/2, 10 * log10(n)))
    # lags. The paper recommends ~ceil(min(n/2, 10*log10(n))) as a
    # generous upper bound; we use the same.
    max_lag = max(1, min(n // 2, ceil(10.0 * np.log10(n))))
    acov = _empirical_autocov(x, max_lag)
    if acov[0] <= 0.0:
        # Degenerate constant series — no autocorrelation defined.
        return 1.0
    rho = acov / acov[0]

    m_lag = _politis_white_truncation_lag(rho, n)
    if m_lag == 0:
        # No significant autocorrelation detected — i.i.d. behavior.
        return 1.0

    # Compute g_hat and G_hat using the flat-top kernel.
    # Lags 1..m_lag (we already have R(0..max_lag); use up to m_lag).
    upper = min(m_lag, max_lag)
    lags = np.arange(1, upper + 1, dtype=np.float64)
    kernel_vals = _flat_top_kernel(lags / m_lag)
    r_pos = acov[1 : upper + 1]
    # g_hat = sum_{|k|<=M} lambda(k/M) |k| R(k)
    #       = 2 * sum_{k=1..M} lambda(k/M) * k * R(k)
    # (R is symmetric; R(0)*0 term contributes 0)
    g_hat = 2.0 * float(np.sum(kernel_vals * lags * r_pos))
    # G_hat = sum_{|k|<=M} lambda(k/M) R(k)
    #       = R(0) + 2 * sum_{k=1..M} lambda(k/M) R(k)
    big_g = acov[0] + 2.0 * float(np.sum(kernel_vals * r_pos))

    # Stationary-bootstrap denominator d_SB = 2 * G_hat^2.
    d_sb = 2.0 * big_g * big_g
    if d_sb <= 0.0 or g_hat == 0.0:
        return 1.0
    b_opt = (2.0 * g_hat * g_hat / d_sb) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    # Bound: never below 1 (i.i.d. floor), never above n/2 (so blocks
    # cannot exceed half the series — a pathological case where the
    # estimator went haywire).
    b_opt = max(1.0, min(b_opt, n / 2.0))
    return float(b_opt)


# --------------------------------------------------------------------------- #
# Stationary block bootstrap (Politis-Romano 1994)
# --------------------------------------------------------------------------- #
def stationary_block_bootstrap(
    returns: np.ndarray,
    *,
    n_paths: int,
    block_size: int | None = None,
    seed: int = DEFAULT_SEED,
) -> np.ndarray:
    """Politis & Romano (1994) stationary block bootstrap.

    Each output path is generated by chaining geometrically-distributed
    blocks of consecutive observations from the input. At each step,
    with probability ``p = 1 / block_size``, the sampler jumps to a
    uniformly-random index; otherwise it advances by one position
    (with wrap-around: the series is treated as circular).

    Block lengths are i.i.d. Geometric(``p``), so the expected block
    length is exactly ``block_size``. Smaller ``block_size`` -> closer
    to i.i.d. resampling (loses serial dependence). Larger ``block_size``
    -> longer blocks, more preserved dependence but smaller effective
    sample size.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of returns to resample.
    n_paths : int
        Number of bootstrap paths to generate. Must be ``>= 1``.
    block_size : int | None
        Expected block length. ``None`` -> call
        :func:`politis_white_optimal_block_length` and use the rounded
        value. Must be ``>= 1`` if given explicitly.
    seed : int
        Deterministic seed for ``np.random.default_rng``.

    Returns
    -------
    np.ndarray
        ``(n_paths, n_obs)`` matrix of bootstrap returns.

    Raises
    ------
    ValueError
        If ``returns`` is not 1-D, ``n_paths < 1``, or
        ``block_size < 1`` (when explicitly provided).
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    n_obs = arr.size
    if n_obs < 2:
        raise ValueError(f"need at least 2 observations to bootstrap, got {n_obs}")
    if n_paths < 1:
        raise ValueError(f"n_paths must be >= 1, got {n_paths}")

    if block_size is None:
        b_float = politis_white_optimal_block_length(arr)
        eff_block_size = max(1, round(b_float))
    else:
        if block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {block_size}")
        eff_block_size = int(block_size)

    rng = np.random.default_rng(seed)
    p = 1.0 / eff_block_size

    # Vectorized construction:
    #   For each (path, t), index[t] = index[t-1] + 1 (mod n) unless
    #   we draw a "jump" at step t, in which case index[t] = uniform.
    # We draw all jumps + all uniform starting points up front, then
    # walk a single Python loop over time (n_obs steps). Each step is
    # an O(n_paths) vectorized numpy update -> total O(n_paths * n_obs).
    # No O(n_paths * n_obs) memory blowup beyond the jumps mask and the
    # output array itself.
    jumps = rng.random((n_paths, n_obs)) < p
    # Always jump at t=0 — the "starting index" of each path is uniform.
    jumps[:, 0] = True
    # Where we jump, the index is a uniform draw. We pre-generate all
    # uniform indices up front (sparse — only `jumps.sum()` of them get
    # used, but indexing arithmetic is cheaper than a conditional draw).
    uniform_indices = rng.integers(0, n_obs, size=(n_paths, n_obs), dtype=np.int64)

    indices = np.empty((n_paths, n_obs), dtype=np.int64)
    # t = 0: every path starts with a uniform jump.
    indices[:, 0] = uniform_indices[:, 0]
    # Iterate over time; per-step update is fully vectorized over paths.
    for t in range(1, n_obs):
        advance = (indices[:, t - 1] + 1) % n_obs
        indices[:, t] = np.where(jumps[:, t], uniform_indices[:, t], advance)

    # Gather: shape (n_paths, n_obs) of bootstrapped returns.
    return arr[indices]


# --------------------------------------------------------------------------- #
# Equity / drawdown / ruin helpers
# --------------------------------------------------------------------------- #
def _equity_curves(
    bootstrap_returns: np.ndarray,
    initial_capital: float,
) -> np.ndarray:
    """Convert bootstrap returns to equity curves.

    Equity[0] = initial_capital * (1 + r_0), Equity[t] = Equity[t-1] *
    (1 + r_t). Cumulative *product*, not sum — compounding matters at
    multi-year horizons.

    Returns shape ``(n_paths, n_obs)``.
    """
    gross = 1.0 + bootstrap_returns
    # Floor at zero — a path that crosses -100% is ruined regardless of
    # what subsequent returns claim. np.cumprod would otherwise let an
    # equity curve cross zero and come back up.
    gross = np.clip(gross, a_min=0.0, a_max=None)
    return initial_capital * np.cumprod(gross, axis=1)


def _max_drawdowns(equity: np.ndarray) -> np.ndarray:
    """Compute the per-path max drawdown ``(equity / running_max - 1).min()``.

    Drawdowns are non-positive: 0 for a monotonically-increasing curve,
    ``-0.5`` for a curve that lost 50% from its running peak. The
    *worst* drawdown is the most negative number across paths.
    """
    running_max = np.maximum.accumulate(equity, axis=1)
    drawdowns = equity / running_max - 1.0
    result: np.ndarray = drawdowns.min(axis=1)
    return result


def _ruin_probability(
    equity: np.ndarray,
    initial_capital: float,
    ruin_threshold: float,
) -> float:
    """Empirical probability of hitting the ruin floor.

    A path is ruined if its *minimum* equity at any time crosses
    ``initial_capital * (1 - ruin_threshold)``. We use the minimum, not
    the terminal value, because firm rules trip the moment the floor is
    crossed — a recovery doesn't save you.
    """
    floor = initial_capital * (1.0 - ruin_threshold)
    path_min = equity.min(axis=1)
    return float(np.mean(path_min <= floor))


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def evaluate(
    returns: np.ndarray,
    *,
    n_paths: int = DEFAULT_N_PATHS,
    block_size: int | None = None,
    ruin_threshold: float = DEFAULT_RUIN_THRESHOLD,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    seed: int = DEFAULT_SEED,
) -> McReport:
    """Run Monte Carlo block bootstrap on ``returns`` and report percentiles.

    The default ``block_size=None`` delegates to
    :func:`politis_white_optimal_block_length` on the *input series* —
    NOT a hardcoded constant. This is the load-bearing decision: a
    hardcoded value would silently destroy serial dependence on series
    whose autocorrelation horizon doesn't match the hardcoded constant.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of per-period returns (e.g. daily simple returns).
        Must have ``n_obs >= 2``.
    n_paths : int
        Number of bootstrap paths. Default 10,000 per the plan's
        headline. Tests may override to ``1000`` for speed (the
        percentile estimates converge well past 1000).
    block_size : int | None
        Expected block length for the stationary bootstrap.

        * ``None`` (default): use Politis-White on the input series.
          The resulting :class:`McReport` has
          ``block_size_source == "politis_white_auto"``.
        * Integer ``>= 1``: use the explicit value (e.g. ``1`` for
          i.i.d. resampling — useful in tests that demonstrate the
          "hardcoded block destroys serial dependence" failure mode).
          ``block_size_source == "user_override"``.
    ruin_threshold : float
        Fraction-of-capital loss that counts as ruin. Default 0.20
        (20% drawdown). Must be in ``(0, 1]``.
    initial_capital : float
        Starting equity. Default 1.0 (unit-normalized).
    seed : int
        Deterministic seed.

    Returns
    -------
    McReport
        Headline percentiles and diagnostic statistics.

    Raises
    ------
    ValueError
        If ``returns`` is not 1-D, has fewer than 2 observations,
        ``n_paths < 1``, ``block_size`` is non-positive when given,
        or ``ruin_threshold`` is outside ``(0, 1]``.
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    if arr.size < 2:
        raise ValueError(f"insufficient data: need at least 2 observations, got {arr.size}")
    if n_paths < 1:
        raise ValueError(f"n_paths must be >= 1, got {n_paths}")
    if not 0.0 < ruin_threshold <= 1.0:
        raise ValueError(f"ruin_threshold must be in (0, 1], got {ruin_threshold}")

    # Resolve block size + source label so the caller can audit how the
    # bootstrap was parameterized. This is the field reviewers should
    # check first: if `block_size_source == "default"` or any hardcoded
    # constant, that's the bug.
    if block_size is None:
        b_float = politis_white_optimal_block_length(arr)
        eff_block_size = max(1, round(b_float))
        block_size_source = "politis_white_auto"
    else:
        if block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {block_size}")
        eff_block_size = int(block_size)
        block_size_source = "user_override"

    # Generate the bootstrap paths.
    boot = stationary_block_bootstrap(
        arr,
        n_paths=n_paths,
        block_size=eff_block_size,
        seed=seed,
    )

    # Equity curves, percentiles across paths (axis=0).
    equity = _equity_curves(boot, initial_capital=initial_capital)
    p5 = np.percentile(equity, 5.0, axis=0)
    p50 = np.percentile(equity, 50.0, axis=0)
    p95 = np.percentile(equity, 95.0, axis=0)

    # Max drawdown distribution.
    dd = _max_drawdowns(equity)
    max_dd_p5 = float(np.percentile(dd, 5.0))
    max_dd_p50 = float(np.percentile(dd, 50.0))
    max_dd_p95 = float(np.percentile(dd, 95.0))

    # Ruin probability.
    ruin = _ruin_probability(
        equity,
        initial_capital=initial_capital,
        ruin_threshold=ruin_threshold,
    )

    return McReport(
        n_paths=int(n_paths),
        n_obs=int(arr.size),
        block_size=int(eff_block_size),
        block_size_source=block_size_source,
        p5_equity=p5,
        p50_equity=p50,
        p95_equity=p95,
        max_dd_p5=max_dd_p5,
        max_dd_p50=max_dd_p50,
        max_dd_p95=max_dd_p95,
        ruin_prob=ruin,
    )
