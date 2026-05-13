"""Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

The Deflated Sharpe Ratio (DSR) corrects an observed Sharpe ratio for
**selection bias** from running multiple backtests, plus for the
non-normality (skew, kurtosis) and finite sample length of the return
series. It returns a probability in ``[0, 1]`` that the strategy's true
Sharpe exceeds the deflated benchmark.

References
----------
* Bailey, D. H., & Lopez de Prado, M. (2014).
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
  Overfitting, and Non-Normality." Journal of Portfolio Management.
* Lopez de Prado, M. (2018). "Advances in Financial Machine Learning"
  (AFML), Chapter 11, especially Section 11.5 and Code Listing 11.4.
* Mertens, E. (2002). Variance of the IID estimator in Lo (2002).

Mathematical conventions
------------------------
The DSR formula in Bailey-LdP requires every Sharpe to be in the **same
per-period units**. The published deflation threshold

    E[max{SR_n}]  approx=  (1 - gamma) * Phi^{-1}(1 - 1/N)
                          + gamma * Phi^{-1}(1 - 1/(N * e))

is the expected maximum of ``N`` independent standard normals (Euler
constant ``gamma ~ 0.5772``). That is in **standard-normal quantile
units**, not per-period Sharpe units. The two are connected by the
standard error of the Sharpe estimator under the null, which is
``1 / sqrt(T - 1)`` for IID Normal returns.

Concretely, we can equivalently write the DSR test statistic either as

    z = (SR_hat - SR_0) * sqrt(T - 1) / sqrt(denom)

with ``SR_0 = E[max_stdnormal] / sqrt(T - 1)`` (both in per-period
units), or as the mathematically identical

    z = (SR_hat * sqrt(T - 1) - E[max_stdnormal]) / sqrt(denom)

with ``SR_hat`` in per-period units (this is what AFML Listing 11.4
implements, and what we use below). The denominator is the
Mertens-Lo-AFML variance correction

    denom = 1 - gamma_3 * SR_hat + (gamma_4 - 1)/4 * SR_hat**2

where ``gamma_3`` is sample skewness (Fisher) and ``gamma_4`` is the
**absolute (Pearson)** kurtosis (``= 3`` for a Normal). The DSR is
``Phi(z)``.

For the user-facing :class:`DsrResult` we expose ``sharpe_observed``,
``sharpe_threshold`` and ``expected_max_sharpe`` in **annualized**
units so they are directly comparable to the headline Sharpe a human
reader sees on a tearsheet. The internal math is done at the native
per-period frequency.

Deviation from the spec's quoted reference example
--------------------------------------------------
The Phase-0 plan quotes a reference DSR of approximately ``0.91`` for
``SR_hat = 2.5, T = 120, N_trials = 10, skew = -0.3, kurt = 5``. Under
the canonical Bailey-LdP formula above with ``SR_hat`` treated as a
per-period Sharpe (the only interpretation under which the formula is
self-consistent), the true value is ``Phi(~7.5) approx= 1.0`` — i.e.
the strategy is overwhelmingly significant even after deflation.
The plan author flags this caveat and we resolve it by trusting the
formula. See :func:`_dsr_from_moments` for the closed-form reproducer
that downstream tests pin.

Public API
----------
* :func:`deflated_sharpe_ratio` — given a return series and an
  explicit ``n_trials``, return a :class:`DsrResult`.
* :func:`evaluate` — uniform W5 entry point with the same shape.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict
from scipy.stats import kurtosis, norm, skew  # type: ignore[import-untyped]

__all__ = [
    "DsrResult",
    "deflated_sharpe_ratio",
    "evaluate",
    "expected_max_sharpe_stdnormal",
]


# Euler-Mascheroni constant: lim_{n -> infinity} (H_n - ln(n)).
_EULER_MASCHERONI: float = 0.5772156649015328606


class DsrResult(BaseModel):
    """Immutable result record for a Deflated Sharpe Ratio evaluation.

    Attributes are annualized where they refer to a Sharpe-like quantity,
    so a human reading a tearsheet sees comparable units.
    """

    model_config = ConfigDict(frozen=True)

    sharpe_observed: float
    """The observed annualized Sharpe ratio of the strategy (SR_hat)."""

    sharpe_threshold: float
    """The user-supplied benchmark Sharpe to clear, annualized
    (commonly ``0``)."""

    n_trials: int
    """The number of backtest trials this strategy's SR was selected
    from. Larger ``n_trials`` => more selection bias => lower DSR."""

    n_obs: int
    """The number of return observations (sample length T)."""

    skewness: float
    """Sample skewness ``gamma_3`` of the return series (Fisher,
    unbiased). Negative skew penalizes DSR via Mertens' variance term."""

    kurtosis: float
    """Sample **excess** (Fisher) kurtosis of the return series (=0 for
    a Normal). Heavy tails penalize DSR via Mertens' variance term."""

    expected_max_sharpe: float
    """The selection-bias deflation, annualized to match
    ``sharpe_observed``: it represents the expected best annualized
    Sharpe among ``n_trials`` independent null strategies of length
    ``n_obs``."""

    dsr: float
    """``P(true SR > sharpe_threshold | observed)`` after deflation,
    in ``[0, 1]``. ``NaN`` if the input is degenerate (zero variance)."""


def expected_max_sharpe_stdnormal(n_trials: int) -> float:
    """Bailey-LdP closed-form approximation to ``E[max{Z_1,...,Z_N}]``
    where the ``Z_i`` are independent standard normals.

        E[max{Z}] approx= (1 - gamma) * Phi^{-1}(1 - 1/N)
                       + gamma * Phi^{-1}(1 - 1/(N * e))

    where ``gamma`` is the Euler-Mascheroni constant. For ``n_trials <= 1``
    we return ``0.0`` (no selection bias when only one trial was run).

    This is the quantity Bailey-LdP §11.5 calls ``E[max_n{SR_n}]``
    expressed in **standard-normal quantile units**. To put it in
    per-period-Sharpe units, divide by ``sqrt(T - 1)``.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1; got {n_trials}")
    if n_trials == 1:
        return 0.0
    n = float(n_trials)
    term1 = (1.0 - _EULER_MASCHERONI) * float(norm.ppf(1.0 - 1.0 / n))
    term2 = _EULER_MASCHERONI * float(norm.ppf(1.0 - 1.0 / (n * math.e)))
    return term1 + term2


def _dsr_from_moments(
    sharpe_period: float,
    n_obs: int,
    skewness: float,
    kurtosis_absolute: float,
    n_trials: int,
) -> tuple[float, float]:
    """Pure-math DSR core. All inputs are at native per-period frequency.

    Parameters
    ----------
    sharpe_period
        The observed Sharpe ratio at the **native per-period frequency**
        (mean / sample-std of returns, no annualization multiplier).
    n_obs
        Number of return observations (T).
    skewness
        Fisher sample skewness (``gamma_3``).
    kurtosis_absolute
        Pearson **absolute** kurtosis (``gamma_4``, equal to 3 for a
        Normal). NOT excess kurtosis.
    n_trials
        Number of backtest trials this SR was selected from.

    Returns
    -------
    dsr, e_max_std
        ``dsr`` is the probability in ``[0, 1]``. ``e_max_std`` is the
        deflation threshold in **standard-normal quantile units**, which
        callers convert to annualized-Sharpe units for display.
    """
    if n_obs < 2:
        raise ValueError(f"need at least 2 observations; got n_obs={n_obs}")

    e_max_std = expected_max_sharpe_stdnormal(n_trials)

    # Mertens variance correction (sigma^2 of the SR estimator) at native
    # per-period frequency. For Normal returns this collapses to
    # ``1 + 0.5 * SR^2`` (skew=0, kurt=3).
    variance_term = (
        1.0
        - skewness * sharpe_period
        + (kurtosis_absolute - 1.0) / 4.0 * sharpe_period * sharpe_period
    )
    if variance_term <= 0.0:
        # The Mertens correction can go non-positive for extreme skew/kurt
        # combinations; the standard error is then undefined. Return NaN.
        return float("nan"), e_max_std

    # The deflated t-statistic. Equivalent to
    #   ((SR_period - e_max_std/sqrt(T-1)) * sqrt(T-1)) / sqrt(variance_term).
    z_num = sharpe_period * math.sqrt(n_obs - 1) - e_max_std
    z_den = math.sqrt(variance_term)
    dsr = float(norm.cdf(z_num / z_den))
    return dsr, e_max_std


def deflated_sharpe_ratio(
    returns: npt.ArrayLike,
    *,
    n_trials: int,
    sharpe_threshold: float = 0.0,
    annualization: int = 252,
) -> DsrResult:
    """Deflated Sharpe Ratio per Bailey & Lopez de Prado (2014).

    Parameters
    ----------
    returns
        1-D sequence of per-period returns (e.g. daily). Must have at
        least 2 elements; raises :class:`ValueError` otherwise.
    n_trials
        **REQUIRED, keyword-only.** The number of independent backtest
        trials this strategy's Sharpe was selected from. There is no
        default — silently defaulting to ``1`` would defeat the entire
        purpose of the deflation.
    sharpe_threshold
        Benchmark **annualized** Sharpe the strategy must beat
        (default ``0``). The DSR is the probability the true Sharpe
        exceeds this threshold after deflation.
    annualization
        Periods per year for annualizing the displayed Sharpe (default
        ``252`` for daily returns). The math is done at the native
        per-period frequency; annualization only rescales display-side
        fields in :class:`DsrResult`.

    Returns
    -------
    DsrResult
        Frozen result record. If the return series has zero variance,
        ``dsr`` is ``NaN`` (degenerate input). Otherwise ``dsr`` is in
        ``[0, 1]``.
    """
    arr = np.asarray(returns, dtype=np.float64).reshape(-1)
    n_obs = arr.size
    if n_obs < 2:
        raise ValueError(f"deflated_sharpe_ratio: need at least 2 return observations; got {n_obs}")
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1; got {n_trials}")
    if annualization < 1:
        raise ValueError(f"annualization must be >= 1; got {annualization}")

    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    ann_sqrt = math.sqrt(annualization)

    # We expose excess kurtosis for human readability but use absolute
    # kurtosis (gamma_4 = excess + 3) inside the Bailey-LdP formula.
    skew_val = float(skew(arr, bias=False))
    kurt_excess = float(kurtosis(arr, fisher=True, bias=False))
    kurt_absolute = kurt_excess + 3.0

    if sigma <= 0.0 or not math.isfinite(sigma):
        # Degenerate input (e.g. all zeros). Return a NaN DSR so callers
        # can branch explicitly; SR_hat is also NaN.
        return DsrResult(
            sharpe_observed=float("nan"),
            sharpe_threshold=float(sharpe_threshold),
            n_trials=int(n_trials),
            n_obs=int(n_obs),
            skewness=skew_val if math.isfinite(skew_val) else float("nan"),
            kurtosis=kurt_excess if math.isfinite(kurt_excess) else float("nan"),
            expected_max_sharpe=float("nan"),
            dsr=float("nan"),
        )

    sharpe_period = mu / sigma
    sharpe_ann = sharpe_period * ann_sqrt

    # Combine the user benchmark with the selection-bias deflation. The
    # user threshold is annualized; convert to per-period units so it
    # composes with the per-period Mertens math.
    threshold_period = float(sharpe_threshold) / ann_sqrt

    dsr, e_max_std = _dsr_from_moments(
        sharpe_period=sharpe_period - threshold_period,
        n_obs=n_obs,
        skewness=skew_val,
        kurtosis_absolute=kurt_absolute,
        n_trials=n_trials,
    )

    # Annualized deflation threshold for display: E[max stdnormal] is in
    # std-normal quantile units; divide by sqrt(T-1) to get per-period
    # Sharpe units, then annualize.
    e_max_ann = (e_max_std / math.sqrt(n_obs - 1)) * ann_sqrt

    return DsrResult(
        sharpe_observed=sharpe_ann,
        sharpe_threshold=float(sharpe_threshold),
        n_trials=int(n_trials),
        n_obs=int(n_obs),
        skewness=skew_val,
        kurtosis=kurt_excess,
        expected_max_sharpe=e_max_ann,
        dsr=dsr,
    )


def evaluate(
    returns: npt.ArrayLike,
    *,
    n_trials: int,
    sharpe_threshold: float = 0.0,
    annualization: int = 252,
) -> DsrResult:
    """Uniform W5 ``evaluate()`` entry point — a thin pass-through to
    :func:`deflated_sharpe_ratio`. Kept distinct so every validation
    sub-module exposes the same surface (CPCV, walk-forward, DSR,
    PBO, Monte Carlo) for the Phase-3 deploy gate to iterate.
    """
    return deflated_sharpe_ratio(
        returns,
        n_trials=n_trials,
        sharpe_threshold=sharpe_threshold,
        annualization=annualization,
    )
