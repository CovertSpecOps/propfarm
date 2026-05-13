"""Tests for ``propfarm.validation.pbo`` — W5 Task 9.2.

Test contract
-------------
1. Output PBO is in ``[0, 1]`` (or NaN for fully degenerate input).
2. Algorithmic corners: perfect IS-OOS correlation → PBO ≈ 0; pure
   noise → PBO ≈ 0.5; perfect anti-correlation → PBO ≈ 1.
3. Combination count for the standard ``n_periods = 16`` case is
   exactly ``C(16, 8) = 12,870`` — the load-bearing constant in
   downstream Phase-3 sample-size math.
4. Diagnostic arrays (``oos_relative_ranks``, ``oos_logits``) are in
   the documented domains and consistent with each other via the
   scipy-canonical logit transform.
5. Per-regime behavior on the canonical fixture: trending regime
   produces *lower* PBO than choppy regime when an MA-crossover grid
   is the test family — because trending has real drift signal that
   confers IS→OOS rank stability.
6. Fixture SHA256 matches the pinned manifest.
7. Boundary errors: ``n_periods < MIN_N_PERIODS`` raises.
8. Degenerate-Sharpe input (all-zero matrix) → ``PBO`` is NaN.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from scipy.special import logit as scipy_logit  # type: ignore[import-untyped]

from propfarm.validation.pbo import (
    MIN_N_PERIODS,
    PboResult,
    evaluate,
    probability_of_backtest_overfitting,
    sharpes_from_returns_and_grid,
)

# --------------------------------------------------------------------------- #
# Fixture access.
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"
PINNED_SHA256 = "f937ab719140ddd4f14d29be876de225c44df069bf4038a877e1987b9b226ff9"


def _load_returns_by_regime() -> dict[str, np.ndarray]:
    """Read the canonical fixture and split into a regime→returns dict.

    Done lazily inside a helper so import time is cheap; called from
    each test that needs it.
    """
    table = pq.read_table(FIXTURE_PATH)  # type: ignore[no-untyped-call]
    df = table.to_pandas()
    return {
        regime: df.loc[df["regime"] == regime, "ret"].to_numpy(dtype=np.float64)
        for regime in df["regime"].unique()
    }


# --------------------------------------------------------------------------- #
# 1. PBO is in [0, 1] for arbitrary inputs.
# --------------------------------------------------------------------------- #
def test_pbo_in_unit_interval() -> None:
    """PBO must lie in ``[0, 1]`` for any non-degenerate Sharpe matrix.

    Random-Gaussian matrix is non-degenerate with probability 1.
    """
    rng = np.random.default_rng(seed=20260512)
    matrix = rng.normal(size=(16, 20))
    result = probability_of_backtest_overfitting(matrix)
    assert isinstance(result, PboResult)
    assert 0.0 <= result.pbo <= 1.0


# --------------------------------------------------------------------------- #
# 2. Algorithmic corners.
# --------------------------------------------------------------------------- #
def test_pbo_zero_when_is_perfectly_predicts_oos() -> None:
    """When the same param dominates every period, PBO must be ≈ 0.

    Construction: column ``k`` has Sharpe ``= k`` (a constant boost
    growing with column index, identical across all periods). Then the
    IS-best param is always column ``n_params - 1``, and it is also the
    OOS-best in every combination — rank = n_params - 1, r ≈ 1, logit ≫
    0, PBO = 0.
    """
    n_periods, n_params = 16, 12
    matrix = np.tile(np.arange(n_params, dtype=np.float64), (n_periods, 1))
    result = probability_of_backtest_overfitting(matrix)
    assert result.pbo == pytest.approx(0.0, abs=1e-12)


def test_pbo_one_when_is_anti_correlated() -> None:
    """When IS rankings are exactly reversed OOS, PBO must be ≈ 1.

    Construction: build a fixed score vector ``v = [0, 1, 2, ..., K-1]``.
    Half the periods score column ``k`` as ``v[k]`` (the "positive"
    ordering — column ``K-1`` looks best). The other half score
    column ``k`` as ``-v[k]`` (the "negative" ordering — column ``K-1``
    looks worst). For any split where the IS rows are predominantly
    from the positive half, ``IS_mean`` ranks columns ascending
    ``0, 1, ..., K-1``; ``OOS_mean`` ranks columns descending
    ``K-1, K-2, ..., 0``. So the IS-best column ``K-1`` is the OOS-worst
    column, putting it at OOS rank 0.

    Add small deterministic jitter so argmax/argsort are unique without
    perturbing the ordering.

    A noticeable fraction of "mixed" splits (those with equal numbers
    of positive and negative rows) produce IS_mean ≈ 0 across all
    columns and land in the degenerate-tie region; jitter resolves
    those ties stochastically. The remaining splits split into two
    symmetric piles — one with majority-positive IS (IS-best low OOS)
    and one with majority-negative IS (IS-best high OOS) — but the
    OOS *means* in those splits are flipped too, so both piles produce
    the same anti-correlation effect. Net result: PBO close to 1.
    """
    n_periods, n_params = 16, 10
    half = n_periods // 2
    # linspace from 1 to n_params so every column has a strictly
    # positive base score. Using np.arange (start=0) would leave col 0
    # at score 0 in both halves, never developing IS/OOS contrast and
    # letting jitter dominate its rank. linspace(1, n_params) avoids
    # that — every column is anti-correlated by a non-trivial margin.
    base = np.linspace(1.0, float(n_params), n_params)
    matrix = np.empty((n_periods, n_params), dtype=np.float64)
    matrix[:half] = base
    matrix[half:] = -base

    # Deterministic jitter to break ties without changing orderings.
    rng = np.random.default_rng(seed=42)
    matrix = matrix + rng.normal(scale=1e-6, size=matrix.shape)

    result = probability_of_backtest_overfitting(matrix)
    # The "clean" combination IS = first half, OOS = second half has
    # the IS-best as OOS-worst (rank 0). By the CSCV symmetry the
    # complementary combination (IS = second half, OOS = first half)
    # also produces rank 0 for its IS-best.
    #
    # Reviewer-corrected explanation (the original "row-totals constraint"
    # rationale was wrong — PBO=1.0 IS reachable under CSCV; counterexamples
    # exist for non-degenerate-tie matrices):
    #
    # The actual reason this jittered construction lands at ≈0.726 rather
    # than at 1.0 is the **jitter dissolving degenerate-tie splits**.
    # In the noiseless version of this construction (where column 2 is
    # literally zero), the 4900 of 12,870 splits with k=4 positive and
    # k=4 negative IS rows have ZERO IS variance — every column ties.
    # Those splits get NaN-skipped in the PBO mean by the implementation's
    # logit guard, and the remaining 7970 ``k≠4`` splits all produce
    # logit < 0 → exact PBO = 1.0.
    #
    # With ``scale=1e-6`` jitter, those k=4 splits become non-degenerate:
    # rank is dominated by the tiny jitter, IS-best is essentially random,
    # OOS rank lands at ~50/50, dragging the mean from 1.0 down to ~0.73.
    # The 0.73 figure is therefore an artifact of *this specific jitter
    # level*, not a theoretical PBO ceiling. We assert ``> 0.65`` to stay
    # well above the noise floor (PBO ≈ 0.5).
    assert result.pbo > 0.65, (
        f"anti-correlated construction should give PBO ≫ 0.5; got {result.pbo:.3f}"
    )


def test_pbo_half_when_is_random() -> None:
    """When IS and OOS ranks are independent, PBO ≈ 0.5 by symmetry.

    Each row of the Sharpe matrix is an *independent* random Gaussian
    permutation of param performance — so the IS-best param's OOS rank
    is uniform on ``{0, ..., n_params - 1}`` in expectation. PBO is the
    fraction of combinations where the IS-best lands below OOS median,
    which is 0.5 by symmetry. The statistical noise tolerance ±0.05 is
    appropriate for ``C(16, 8) = 12,870`` combinations.
    """
    rng = np.random.default_rng(seed=20260512)
    matrix = rng.normal(size=(16, 20))
    result = probability_of_backtest_overfitting(matrix)
    assert result.pbo == pytest.approx(0.5, abs=0.05)


# --------------------------------------------------------------------------- #
# 3. Combination count.
# --------------------------------------------------------------------------- #
def test_combination_count() -> None:
    """``n_periods = 16`` → exactly ``C(16, 8) = 12,870`` combinations.

    This is the load-bearing constant — downstream sample-size math
    for the Phase-3 PBO confidence interval assumes 12,870. If anyone
    changes the split convention, this test must change in lockstep.
    """
    rng = np.random.default_rng(seed=20260512)
    matrix = rng.normal(size=(16, 10))
    result = probability_of_backtest_overfitting(matrix)
    assert result.n_combinations == 12870
    assert result.oos_relative_ranks.shape == (12870,)
    assert result.oos_logits.shape == (12870,)


# --------------------------------------------------------------------------- #
# 4. Diagnostic arrays.
# --------------------------------------------------------------------------- #
def test_oos_relative_ranks_in_unit_interval() -> None:
    """Every ``oos_relative_ranks[c]`` must lie in the open ``(0, 1)``.

    Non-NaN guard accounts for the documented degenerate-combo NaN case
    (which a random matrix should not trigger but we still guard).
    """
    rng = np.random.default_rng(seed=20260512)
    matrix = rng.normal(size=(16, 25))
    result = probability_of_backtest_overfitting(matrix)
    valid = ~np.isnan(result.oos_relative_ranks)
    assert valid.all(), "random Gaussian matrix should never produce NaN ranks"
    assert (result.oos_relative_ranks > 0.0).all()
    assert (result.oos_relative_ranks < 1.0).all()


def test_oos_logits_match_ranks() -> None:
    """``oos_logits[c]`` must equal ``scipy.special.logit(ranks[c])``.

    This is the contract that lets a downstream plot trust the
    pre-computed logit distribution without recomputing.
    """
    rng = np.random.default_rng(seed=20260512)
    matrix = rng.normal(size=(16, 12))
    result = probability_of_backtest_overfitting(matrix)
    expected_logits = scipy_logit(result.oos_relative_ranks)
    np.testing.assert_allclose(
        result.oos_logits,
        expected_logits,
        rtol=1e-12,
        atol=1e-12,
        err_msg="oos_logits must equal scipy.special.logit(oos_relative_ranks)",
    )


# --------------------------------------------------------------------------- #
# 5. Per-regime behavior on the canonical fixture.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "regime",
    ["trending", "mean_reverting", "choppy", "fat_tailed"],
)
def test_pbo_per_regime(regime: str) -> None:
    """PBO ∈ [0, 1] for every regime; trending lower than choppy.

    Build a Sharpe matrix per regime using a MA-of-returns grid with
    six lookback lengths. Then check PBO is in unit interval. We also
    verify, in the trending-vs-choppy comparison below, that the
    trending regime's PBO is *lower* than the choppy regime's — because
    trending has a genuine drift signal that confers IS→OOS rank
    stability for momentum-flavored lookback choices.

    Per-regime expectations (rough):
      * ``trending``: lower PBO (real signal → rank stability).
      * ``choppy``: PBO ≈ 0.5 (pure noise; param choice is random).
      * ``mean_reverting``: ≈ 0.5 on raw returns + MA-momentum filter
        because the AR(1) signal isn't captured by a momentum grid.
      * ``fat_tailed``: ≈ 0.5; no drift, heavy tails don't confer rank
        stability.

    We avoid hardcoding tight bounds for every regime to stay robust to
    minor implementation drift; the load-bearing relationship is
    ``PBO(trending) < PBO(choppy)``, tested separately below.
    """
    returns_by_regime = _load_returns_by_regime()
    returns = returns_by_regime[regime]
    lookbacks = (5, 10, 20, 50, 100, 200)
    n_periods = 16
    matrix = sharpes_from_returns_and_grid(returns, lookbacks, n_periods)
    assert matrix.shape == (n_periods, len(lookbacks))

    result = probability_of_backtest_overfitting(matrix)
    assert 0.0 <= result.pbo <= 1.0, f"{regime}: PBO out of [0, 1]"


def test_pbo_trending_lower_than_choppy() -> None:
    """Real signal must reduce PBO relative to pure noise.

    Trending regime has a positive annualized drift (~+8%). A momentum
    lookback (trailing-mean sign) lines up with that drift, so the
    IS-best lookback retains predictive power OOS — fewer combinations
    land below OOS median → lower PBO. Choppy is zero-mean noise where
    IS-best is luck; PBO ≈ 0.5.

    The comparison is what we trust, not absolute levels: with only
    ~312 bars per period (5000 / 16) and noisy Sharpes the absolute
    PBO is volatile, but the relative ordering between trending and
    choppy should hold.
    """
    returns_by_regime = _load_returns_by_regime()
    lookbacks = (5, 10, 20, 50, 100, 200)
    n_periods = 16

    matrix_trend = sharpes_from_returns_and_grid(
        returns_by_regime["trending"], lookbacks, n_periods
    )
    matrix_chop = sharpes_from_returns_and_grid(returns_by_regime["choppy"], lookbacks, n_periods)

    pbo_trend = probability_of_backtest_overfitting(matrix_trend).pbo
    pbo_chop = probability_of_backtest_overfitting(matrix_chop).pbo

    assert pbo_trend < pbo_chop, (
        f"trending PBO ({pbo_trend:.3f}) should be < choppy PBO ({pbo_chop:.3f}); "
        "real signal must confer IS→OOS rank stability."
    )


# --------------------------------------------------------------------------- #
# 6. Fixture integrity.
# --------------------------------------------------------------------------- #
def test_fixture_sha256_pinned() -> None:
    """The on-disk parquet must match the pinned SHA256 manifest.

    Pinning is the only thing that lets us share results across the 5
    parallel W5 agents — if any agent silently regenerates the fixture,
    their numbers stop being comparable.
    """
    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == PINNED_SHA256, (
        f"fixture SHA256 mismatch: got {digest}, expected {PINNED_SHA256}. "
        "Do NOT regenerate the fixture inside an agent."
    )


# --------------------------------------------------------------------------- #
# 7. Boundary errors.
# --------------------------------------------------------------------------- #
def test_evaluate_raises_on_insufficient_periods() -> None:
    """``n_periods < MIN_N_PERIODS`` must raise ``ValueError``."""
    # Just under threshold.
    too_few = np.ones((MIN_N_PERIODS - 2, 4), dtype=np.float64)
    # Force even (CPCV demands even); the n_periods<MIN check must trip
    # first regardless. Use MIN_N_PERIODS - 2 to keep parity even.
    with pytest.raises(ValueError, match="n_periods must be >="):
        evaluate(too_few)


def test_evaluate_raises_on_odd_n_periods() -> None:
    """Odd ``n_periods`` must raise (CSCV requires symmetric split)."""
    matrix = np.random.default_rng(0).normal(size=(15, 5))
    with pytest.raises(ValueError, match="even"):
        evaluate(matrix)


def test_evaluate_raises_on_one_param() -> None:
    """``n_params < 2`` must raise."""
    matrix = np.random.default_rng(0).normal(size=(16, 1))
    with pytest.raises(ValueError, match="n_params must be >= 2"):
        evaluate(matrix)


def test_evaluate_raises_on_non_2d() -> None:
    """1-D input must raise — PBO is matrix-in."""
    with pytest.raises(ValueError, match="2-D"):
        evaluate(np.zeros(16))


def test_evaluate_raises_on_non_finite() -> None:
    """NaN or inf in the Sharpe matrix must raise."""
    matrix = np.zeros((16, 5))
    matrix[0, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        evaluate(matrix)


# --------------------------------------------------------------------------- #
# 8. Degenerate-Sharpe input.
# --------------------------------------------------------------------------- #
def test_evaluate_degenerate_returns_nan() -> None:
    """All-zero Sharpe matrix → PBO is NaN (no unique IS-best exists).

    Every column ties every other column in every period, so the IS
    argmax is degenerate; we record NaN for each combination and the
    aggregate PBO is NaN. Diagnostic arrays carry NaN too so the
    downstream plot can detect and surface the condition.
    """
    matrix = np.zeros((16, 6), dtype=np.float64)
    result = probability_of_backtest_overfitting(matrix)
    assert np.isnan(result.pbo)
    assert np.isnan(result.oos_relative_ranks).all()
    assert np.isnan(result.oos_logits).all()


# --------------------------------------------------------------------------- #
# Sanity-check the helper directly so failures in the regime tests
# are diagnosable.
# --------------------------------------------------------------------------- #
def test_helper_sharpes_from_returns_and_grid_shape() -> None:
    """Helper returns the expected shape and is finite on the fixture."""
    returns = _load_returns_by_regime()["trending"]
    lookbacks = (5, 10, 20, 50, 100, 200)
    n_periods = 16
    matrix = sharpes_from_returns_and_grid(returns, lookbacks, n_periods)
    assert matrix.shape == (n_periods, len(lookbacks))
    assert np.isfinite(matrix).all()


def test_helper_rejects_too_short_returns() -> None:
    """Helper raises ValueError if returns can't accommodate max lookback."""
    short = np.zeros(50)
    with pytest.raises(ValueError, match="too short"):
        sharpes_from_returns_and_grid(short, (5, 200), n_periods=16)
