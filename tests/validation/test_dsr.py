"""Tests for ``propfarm.validation.dsr`` — Bailey & Lopez de Prado DSR.

The contract these tests pin:

* ``n_trials`` is a **required** keyword argument with no default. The
  whole point of DSR is correcting for selection bias, and silently
  defaulting to ``1`` would defeat that.
* DSR lies in ``[0, 1]`` for every fixture regime.
* DSR is **monotonically non-increasing** in ``n_trials`` for the same
  return series (more trials => more selection bias => less confidence).
* The kurtosis correction penalizes fat tails at fixed Sharpe and
  fixed skewness (Mertens' variance term grows in the kurtosis).
* The canonical fixture's SHA256 is pinned and consumed without
  regeneration.
* Edge cases (single-observation, zero-variance) raise/return-NaN per
  the module's contract.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from scipy.stats import (  # type: ignore[import-untyped]
    kurtosis as scipy_kurt,
)
from scipy.stats import (
    skew as scipy_skew,
)

from propfarm.validation.dsr import (
    DsrResult,
    deflated_sharpe_ratio,
    evaluate,
    expected_max_sharpe_stdnormal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "fixtures" / "synthetic_returns.parquet"
EXPECTED_SHA256 = "f937ab719140ddd4f14d29be876de225c44df069bf4038a877e1987b9b226ff9"
REGIMES = ("trending", "mean_reverting", "choppy", "fat_tailed")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def returns_by_regime() -> dict[str, np.ndarray]:
    """Load synthetic returns once per test module, keyed by regime."""
    df = pq.read_table(FIXTURE_PATH).to_pandas()  # type: ignore[no-untyped-call]
    return {r: df.loc[df["regime"] == r, "ret"].to_numpy() for r in REGIMES}


# --------------------------------------------------------------------------- #
# Contract: n_trials is required and the fixture is pinned
# --------------------------------------------------------------------------- #
def test_n_trials_is_required(returns_by_regime: dict[str, np.ndarray]) -> None:
    """``n_trials`` MUST be keyword-only and have no default. Calling
    without it must raise ``TypeError`` — silently defaulting to 1 would
    hide the selection-bias correction the entire DSR exists for."""
    r = returns_by_regime["trending"]
    with pytest.raises(TypeError):
        deflated_sharpe_ratio(r)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        evaluate(r)  # type: ignore[call-arg]


def test_fixture_sha256_pinned() -> None:
    """SHA256 of the canonical parquet bytes must match the value stamped
    into this test file. If this drifts, the fixture was regenerated
    out-of-band and every Wave-5 result is suspect until reconciled."""
    h = hashlib.sha256()
    with FIXTURE_PATH.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    assert h.hexdigest() == EXPECTED_SHA256, (
        f"fixture drifted: expected {EXPECTED_SHA256}, got {h.hexdigest()}"
    )


# --------------------------------------------------------------------------- #
# Core math sanity
# --------------------------------------------------------------------------- #
def test_expected_max_sharpe_against_known_n() -> None:
    """E[max{SR_n}] under the null is the expected maximum of N standard
    normals. With N=1 there is no selection bias, so it must be 0
    exactly; for N>1 it grows monotonically and is positive."""
    assert expected_max_sharpe_stdnormal(1) == 0.0
    # Bailey-LdP closed-form values used by AFML §11.5:
    e10 = expected_max_sharpe_stdnormal(10)
    e100 = expected_max_sharpe_stdnormal(100)
    e1000 = expected_max_sharpe_stdnormal(1000)
    assert e10 > 0.0
    assert e100 > e10
    assert e1000 > e100
    # Approximate numerical values (verified by hand against Bailey-LdP).
    assert e10 == pytest.approx(1.5746, abs=1e-3)
    assert e100 == pytest.approx(2.5306, abs=1e-3)


def test_skewness_kurtosis_computed_correctly(
    returns_by_regime: dict[str, np.ndarray],
) -> None:
    """The result's ``skewness`` and ``kurtosis`` must equal scipy's
    ``skew(bias=False)`` and ``kurtosis(fisher=True, bias=False)``
    respectively. This pins the moment conventions the rest of the
    module relies on (Fisher excess kurtosis on the result; the
    formula internally adds 3 to recover absolute Pearson kurtosis)."""
    for regime, r in returns_by_regime.items():
        res = deflated_sharpe_ratio(r, n_trials=10)
        assert res.skewness == pytest.approx(float(scipy_skew(r, bias=False)), rel=1e-12), (
            f"skewness mismatch on regime {regime!r}"
        )
        assert res.kurtosis == pytest.approx(
            float(scipy_kurt(r, fisher=True, bias=False)), rel=1e-12
        ), f"kurtosis mismatch on regime {regime!r}"


# --------------------------------------------------------------------------- #
# Domain & monotonicity properties
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("regime", REGIMES)
def test_dsr_in_unit_interval(regime: str, returns_by_regime: dict[str, np.ndarray]) -> None:
    """A probability must live in ``[0, 1]`` for every regime, every
    realistic ``n_trials``."""
    for n_trials in (1, 10, 100, 1000):
        res = deflated_sharpe_ratio(returns_by_regime[regime], n_trials=n_trials)
        assert 0.0 <= res.dsr <= 1.0, f"DSR out of [0,1] for {regime} N={n_trials}"


def test_dsr_decreases_with_n_trials(
    returns_by_regime: dict[str, np.ndarray],
) -> None:
    """Strictly the **point** of deflation: with more trials the
    confidence must drop. We test on ``trending`` (where the effect is
    pronounced because the SR is materially positive)."""
    r = returns_by_regime["trending"]
    dsr_n1 = deflated_sharpe_ratio(r, n_trials=1).dsr
    dsr_n1000 = deflated_sharpe_ratio(r, n_trials=1000).dsr
    assert dsr_n1000 < dsr_n1, (
        f"deflation failed: DSR(N=1000)={dsr_n1000:.4f} should be < DSR(N=1)={dsr_n1:.4f}"
    )

    # And the relationship is monotonic across an intermediate value.
    dsr_n100 = deflated_sharpe_ratio(r, n_trials=100).dsr
    assert dsr_n1 >= dsr_n100 >= dsr_n1000


def test_dsr_per_regime(returns_by_regime: dict[str, np.ndarray]) -> None:
    """At ``n_trials=10`` on the canonical fixture the regime ordering
    must reflect signal strength: ``trending`` has the strongest
    observed Sharpe and must therefore have the highest DSR; the two
    near-zero-mean regimes (``choppy``, ``mean_reverting``) sit far
    below it."""
    dsr_values = {r: deflated_sharpe_ratio(returns_by_regime[r], n_trials=10).dsr for r in REGIMES}
    assert dsr_values["trending"] > dsr_values["choppy"]
    assert dsr_values["trending"] > dsr_values["mean_reverting"]
    assert dsr_values["trending"] > dsr_values["fat_tailed"]


# --------------------------------------------------------------------------- #
# Fat-tail penalty — isolated to the kurtosis term in Mertens' variance
# --------------------------------------------------------------------------- #
def test_dsr_fat_tail_penalty() -> None:
    """At **fixed** observed Sharpe and skewness, increasing kurtosis
    must strictly decrease DSR. We call the pure-math core directly so
    SR, skew and T are held perfectly fixed and only the kurtosis term
    varies. The penalty enters Mertens' variance correction through
    the ``(gamma_4 - 1) / 4 * SR^2`` summand in the denominator, so it
    is only visible when SR^2 is non-negligible AND the DSR is not
    saturated near 0 or 1.
    """
    from propfarm.validation.dsr import _dsr_from_moments

    # Choose (SR_period, T, N) so the DSR sits in the unsaturated
    # middle of the distribution where the kurtosis term materially
    # moves the answer.
    sr_period = 0.2
    n_obs = 200
    skewness = 0.0
    n_trials = 10

    dsr_normal, _ = _dsr_from_moments(
        sharpe_period=sr_period,
        n_obs=n_obs,
        skewness=skewness,
        kurtosis_absolute=3.0,  # Normal-tail baseline
        n_trials=n_trials,
    )
    dsr_fat, _ = _dsr_from_moments(
        sharpe_period=sr_period,
        n_obs=n_obs,
        skewness=skewness,
        kurtosis_absolute=10.0,  # Heavy-tailed
        n_trials=n_trials,
    )

    assert 0.05 < dsr_normal < 0.95, "test calibration: DSR must not be saturated"
    assert dsr_fat < dsr_normal, (
        f"fat-tail penalty broken: DSR_fat={dsr_fat:.6f} >= DSR_normal={dsr_normal:.6f}"
    )

    # End-to-end sanity: on the canonical fixture the ``fat_tailed``
    # regime's deflated Sharpe should at minimum not exceed the
    # ``trending`` regime's (which has materially higher SR + lower
    # kurtosis).
    df = pq.read_table(FIXTURE_PATH).to_pandas()  # type: ignore[no-untyped-call]
    r_fat = df.loc[df["regime"] == "fat_tailed", "ret"].to_numpy()
    r_trend = df.loc[df["regime"] == "trending", "ret"].to_numpy()
    res_fat = deflated_sharpe_ratio(r_fat, n_trials=100)
    res_trend = deflated_sharpe_ratio(r_trend, n_trials=100)
    assert res_fat.dsr < res_trend.dsr


# --------------------------------------------------------------------------- #
# Reference example from the W5 plan
# --------------------------------------------------------------------------- #
def test_dsr_against_published_example() -> None:
    """Plan §Test 4 canonical reference: ``SR=2.5, T=120, N_trials=10,
    skew=-0.3, kurt=5`` → ``DSR ≈ 1.0``. SR=2.5 / N=10 is in the realistic
    range for Phase 1 strategy outputs; the strategy is overwhelmingly
    significant even after deflation for ten trial backtests.

    Hand-traced under the canonical Bailey-LdP formula (per-period SR=2.5;
    absolute Pearson kurt=5):

        E[max stdnormal | N=10] = (1-gamma) * Phi^-1(0.9)
                                + gamma * Phi^-1(1 - 1/(10e))
                                ~= 1.5746

        denom = sqrt(1 - (-0.3)*2.5 + (5-1)/4*2.5**2)
              = sqrt(1 + 0.75 + 6.25) = sqrt(8) ~= 2.8284

        z = (2.5 * sqrt(119) - 1.5746) / 2.8284
          = (27.275 - 1.5746) / 2.8284 ~= 9.085

        DSR = Phi(9.085) ~= 1.0

    (An earlier draft of the plan quoted DSR≈0.91 for these inputs; the
    W5 reviewer traced against Wikipedia, Marti's blog, and the
    López-de-Prado-blessed rubenbriones reference impl and confirmed
    ~1.0 is correct. Plan amended 2026-05-12 to option (b): keep
    SR=2.5 inputs as realistic Phase-1 values, fix the expected.)

    A boundary test constructing an input that lands DSR ≈ 0.95
    (at the deploy-gate threshold) is tracked in the deferred ledger
    and will land before Phase 1 dispatches.
    """
    # Synthesize via the pure-math core to avoid finite-sample moment
    # recovery noise. The test pins formula correctness, not estimator
    # accuracy on a small sample.
    from propfarm.validation.dsr import _dsr_from_moments

    dsr, e_max = _dsr_from_moments(
        sharpe_period=2.5,
        n_obs=120,
        skewness=-0.3,
        kurtosis_absolute=5.0,
        n_trials=10,
    )
    # Hand-traced canonical values.
    assert e_max == pytest.approx(1.5746, abs=1e-3)
    # Tight pin on DSR ≈ 1.0. Per user-approved plan amendment (option b),
    # the >= 0.99 band is the canonical answer; a future contributor
    # attempting to "fix" the math toward 0.91 will fail here.
    assert dsr >= 0.99, (
        f"DSR={dsr:.4f} for the reference inputs disagrees with the "
        "canonical Bailey-LdP formula; expected >= 0.99. "
        "See module docstring for hand-trace."
    )


# --------------------------------------------------------------------------- #
# Edge cases / degenerate inputs
# --------------------------------------------------------------------------- #
def test_evaluate_raises_on_insufficient_data() -> None:
    """A single observation can't have a sample std, let alone a Sharpe.
    The entry point must reject it explicitly, not return NaN."""
    with pytest.raises(ValueError):
        evaluate(np.array([1.0]), n_trials=1)
    with pytest.raises(ValueError):
        evaluate(np.array([]), n_trials=1)


def test_evaluate_degenerate_returns_nan_dsr() -> None:
    """All-zero returns => zero variance => undefined Sharpe. By
    convention the result has NaN DSR (NOT 0.5), so the deploy gate
    explicitly fails closed instead of accidentally passing on a
    pathological input."""
    res = evaluate(np.zeros(1000), n_trials=1)
    assert isinstance(res, DsrResult)
    assert math.isnan(res.dsr)
    assert math.isnan(res.sharpe_observed)


def test_evaluate_rejects_invalid_n_trials() -> None:
    """``n_trials`` must be a positive integer."""
    r = np.random.default_rng(0).normal(size=500)
    with pytest.raises(ValueError):
        evaluate(r, n_trials=0)
    with pytest.raises(ValueError):
        evaluate(r, n_trials=-3)


def test_evaluate_rejects_invalid_annualization() -> None:
    r = np.random.default_rng(0).normal(size=500)
    with pytest.raises(ValueError):
        evaluate(r, n_trials=1, annualization=0)


# --------------------------------------------------------------------------- #
# Result shape / immutability
# --------------------------------------------------------------------------- #
def test_result_is_frozen(returns_by_regime: dict[str, np.ndarray]) -> None:
    """``DsrResult`` is declared ``frozen=True``; assignment must raise."""
    res = deflated_sharpe_ratio(returns_by_regime["choppy"], n_trials=10)
    with pytest.raises((ValueError, AttributeError, TypeError)):
        res.dsr = 0.5


def test_result_fields_present_and_typed(
    returns_by_regime: dict[str, np.ndarray],
) -> None:
    """All declared fields exist and have the spec'd types."""
    res = deflated_sharpe_ratio(returns_by_regime["trending"], n_trials=10)
    assert isinstance(res.sharpe_observed, float)
    assert isinstance(res.sharpe_threshold, float)
    assert isinstance(res.n_trials, int)
    assert isinstance(res.n_obs, int)
    assert isinstance(res.skewness, float)
    assert isinstance(res.kurtosis, float)
    assert isinstance(res.expected_max_sharpe, float)
    assert isinstance(res.dsr, float)
    assert res.n_trials == 10
    assert res.n_obs == returns_by_regime["trending"].size


def test_evaluate_matches_deflated_sharpe_ratio(
    returns_by_regime: dict[str, np.ndarray],
) -> None:
    """``evaluate`` is the W5 uniform entry point and must be a pure
    pass-through to ``deflated_sharpe_ratio`` — any divergence between
    them is a coordination bug."""
    r = returns_by_regime["fat_tailed"]
    r1 = evaluate(r, n_trials=42, sharpe_threshold=0.5, annualization=252)
    r2 = deflated_sharpe_ratio(r, n_trials=42, sharpe_threshold=0.5, annualization=252)
    assert r1 == r2
