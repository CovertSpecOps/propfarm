"""Tests for ``propfarm.sim.slippage`` (Task 7.1) — order-type-aware slippage model.

The slippage model is the silent cost-leak component of the cost stack: spread
is public, commission is contractual, swap is tabulated — slippage is the
broker-discretion residual. These tests lock the model's contract on five
axes that the reviewer flagged as "must not regress":

1. **Order-type-aware behavior.** Market and stop orders take full slip;
   limit orders take zero slip and expose a separate ``reject_probability``.
2. **Determinism.** ``rng=None`` is byte-identical across calls; seeded
   rngs are reproducible and distinguishable across distinct seeds.
3. **Confidence flag.** Every shipped calibration is ``"uncertain"`` until
   live recording fills the gap.
4. **Stress amplification.** ``stress_mode=True`` produces 5-20 pips even
   on FX majors — the regime that 10.2 stress replay loads.
5. **Size scaling shape.** Sub-linear at retail (0.01 vs 0.1 lot ~ same);
   accelerates beyond 1 lot.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pytest
from pydantic import ValidationError

from propfarm.sim.slippage import (
    CALIBRATIONS,
    MarketState,
    SlippageCalibrationEntry,
    SlippageRequest,
    evaluate,
)

# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #

#: Mid-session EURUSD timestamp: 2024-06-12 10:00 UTC. London/NY overlap,
#: no NY-close window, no DST boundary. The reference "quiet hour" used
#: for the [0, 1] pip baseline test.
_MID_SESSION_TS: datetime = datetime(2024, 6, 12, 10, 0, tzinfo=UTC)


def _eurusd_state(
    *,
    vol: float | None = 0.10,
    news: bool = False,
    stress: bool = False,
    ts: datetime = _MID_SESSION_TS,
) -> MarketState:
    """Build a typical-regime EURUSD MarketState for tests."""
    return MarketState(
        symbol="EURUSD",
        ts_utc=ts,
        realized_vol_5m=vol,
        news_window=news,
        stress_mode=stress,
    )


def _req(
    *,
    side: str = "buy",
    order_type: str = "market",
    size_lots: float = 0.01,
) -> SlippageRequest:
    """Build a SlippageRequest, with pydantic Literal narrowing at the boundary."""
    # The Literal narrowing happens inside the model; mypy is satisfied
    # because pydantic validates and pytest tests run-time semantics.
    return SlippageRequest(
        side=side,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        size_lots=size_lots,
    )


# --------------------------------------------------------------------------- #
# Reviewer-mandated tests — order-type-aware behavior (CRITICAL)
# --------------------------------------------------------------------------- #
def test_market_order_takes_slippage() -> None:
    """0.01 lot EURUSD market at mid-session normal vol → slip in [0, 1] pip.

    The default-magnitude spec: for 0.01-lot retail-size on FX majors at
    normal volatility, expected slip is 0-1 pip. Deviations require
    justification (calibration moved); locking the band here is the
    first line of defense against an accidental coefficient bump.
    """
    state = _eurusd_state()
    request = _req()
    result = evaluate(state, request)
    assert 0.0 <= result.slippage_pips <= 1.0, (
        f"market order slip outside [0, 1] pip: {result.slippage_pips}"
    )
    assert result.reject_probability == 0.0
    assert result.order_type == "market"


def test_limit_order_zero_slippage() -> None:
    """Limit order → slip exactly 0.0, reject_probability > 0.

    The order-type-aware contract: limit orders either fill at the limit
    price or do not fill at all. They take no slip by construction. The
    reject probability is what the fill engine consumes as a Bernoulli
    to decide whether the limit was honored.
    """
    state = _eurusd_state()
    request = _req(order_type="limit")
    result = evaluate(state, request)
    assert result.slippage_pips == 0.0
    assert result.reject_probability > 0.0
    assert result.order_type == "limit"


def test_stop_order_takes_slippage_at_trigger() -> None:
    """Stop order → slip > 0 (treated as market once triggered).

    A triggered stop is functionally a market order at the trigger
    price. The only question is at what worse price; slippage applies
    in full. ``reject_probability`` is 0 — a triggered stop fills.
    """
    state = _eurusd_state()
    request = _req(order_type="stop")
    result = evaluate(state, request)
    assert result.slippage_pips > 0.0
    assert result.reject_probability == 0.0
    assert result.order_type == "stop"


def test_market_and_stop_have_same_slippage_at_identical_inputs() -> None:
    """Market and stop must produce identical slip at identical inputs.

    The reviewer's "order-type-aware (CRITICAL)" criterion includes:
    market and stop share the formula. This test pins that equality so a
    future refactor that introduces a "stop only" branch is caught.
    """
    state = _eurusd_state()
    market_result = evaluate(state, _req(order_type="market"))
    stop_result = evaluate(state, _req(order_type="stop"))
    assert market_result.slippage_pips == stop_result.slippage_pips


# --------------------------------------------------------------------------- #
# Size-scaling axis: sub-linear at retail, accelerates at institutional size
# --------------------------------------------------------------------------- #
def test_size_scaling_sublinear() -> None:
    """0.01 vs 0.1 lot differ by < 0.1 pip (small-size regime).

    At retail-size, ``log(size + 1)`` is approximately linear in size
    with a small slope (~0.01 -> 0.0099, 0.1 -> 0.0953). With
    ``size_coef=0.5`` for EURUSD, the difference is ~0.04 pips — well
    below the 0.1-pip ceiling for the sub-linear regime.
    """
    state = _eurusd_state()
    small = evaluate(state, _req(size_lots=0.01)).slippage_pips
    medium = evaluate(state, _req(size_lots=0.1)).slippage_pips
    diff = abs(medium - small)
    assert diff < 0.1, f"0.01 vs 0.1 lot slip diff = {diff:.4f} pip; expected sub-linear (<0.1 pip)"


def test_size_scaling_larger_at_1_lot() -> None:
    """1.0 lot has materially more slip than 0.01 lot.

    Where "materially" is at least 0.2 pips for EURUSD with
    ``size_coef=0.5``: ``0.5 * (log(2) - log(1.01)) ≈ 0.34`` pips. We
    bound the lower side conservatively to keep the test robust under
    small future re-calibrations.
    """
    state = _eurusd_state()
    small = evaluate(state, _req(size_lots=0.01)).slippage_pips
    one_lot = evaluate(state, _req(size_lots=1.0)).slippage_pips
    diff = one_lot - small
    assert diff >= 0.2, f"1.0 lot vs 0.01 lot slip diff = {diff:.4f} pip; expected >= 0.2 pip"


def test_size_scaling_monotonic() -> None:
    """Slip is non-decreasing in size — no microstructural reversal.

    A monotonicity property test along the size axis. Catches a sign
    flip on ``size_coef`` (would invert the curve) and catches an
    accidental ``log(size - 1)`` typo (would push small-size slip
    negative-into-infinity).
    """
    from itertools import pairwise

    sizes = [0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]
    state = _eurusd_state()
    slips = [evaluate(state, _req(size_lots=s)).slippage_pips for s in sizes]
    for prev, curr in pairwise(slips):
        assert curr >= prev, f"slip not monotonic in size: {slips}"


# --------------------------------------------------------------------------- #
# Vol-scaling axis
# --------------------------------------------------------------------------- #
def test_vol_scaling() -> None:
    """High realized_vol (0.5) → strictly more slip than low (0.05)."""
    low_vol = evaluate(_eurusd_state(vol=0.05), _req())
    high_vol = evaluate(_eurusd_state(vol=0.5), _req())
    assert high_vol.slippage_pips > low_vol.slippage_pips, (
        f"vol scaling broken: high={high_vol.slippage_pips}, low={low_vol.slippage_pips}"
    )


def test_realized_vol_none_uses_default() -> None:
    """``realized_vol_5m=None`` falls back to the module default (10% annualized).

    The default keeps the model usable in unit tests and early placebo
    runs before the realized-vol feature pipeline is wired in. Locking
    the fallback here catches a regression that would silently fill the
    vol_term with 0 — making slip suspiciously low across the board.
    """
    none_state = _eurusd_state(vol=None)
    default_state = _eurusd_state(vol=0.10)
    assert (
        evaluate(none_state, _req()).slippage_pips == evaluate(default_state, _req()).slippage_pips
    )


# --------------------------------------------------------------------------- #
# Stress-mode amplification
# --------------------------------------------------------------------------- #
def test_stress_mode_amplifies() -> None:
    """``stress_mode=True`` → slip ≥ 5 pips on EURUSD (extreme regime).

    The Task 10.2 stress-replay library replays the Lehman / SNB /
    GBP-flash / COVID / UK-gilts / SVB event windows with this flag set.
    A typical-regime EURUSD slip is ~0.5 pip; the spec band for stress
    is 5-20 pips, so 5 pips is the lower bound the test checks.
    """
    stress_state = _eurusd_state(stress=True)
    result = evaluate(stress_state, _req())
    assert result.slippage_pips >= 5.0, f"stress slip = {result.slippage_pips}; expected >= 5 pips"
    assert result.slippage_pips <= 20.0, (
        f"stress slip = {result.slippage_pips}; expected <= 20 pips for EURUSD"
    )


def test_stress_mode_dominates_news_window() -> None:
    """stress_mode=True overrides news_window=True (does not stack).

    The two flags are not summed — stress subsumes news. This test pins
    the override so a future refactor that adds them does not silently
    blow slip past the [5, 20] spec band when both happen to be set.
    """
    stress_only = evaluate(_eurusd_state(stress=True), _req()).slippage_pips
    stress_and_news = evaluate(_eurusd_state(stress=True, news=True), _req()).slippage_pips
    assert stress_only == stress_and_news


def test_news_window_amplifies_but_less_than_stress() -> None:
    """news_window=True → slip > baseline but < stress slip."""
    baseline = evaluate(_eurusd_state(), _req()).slippage_pips
    news = evaluate(_eurusd_state(news=True), _req()).slippage_pips
    stress = evaluate(_eurusd_state(stress=True), _req()).slippage_pips
    assert baseline < news < stress


# --------------------------------------------------------------------------- #
# Determinism axis
# --------------------------------------------------------------------------- #
def test_determinism_no_rng() -> None:
    """rng=None → identical SlippageResult across 100 calls."""
    state = _eurusd_state()
    request = _req()
    first = evaluate(state, request)
    for _ in range(99):
        result = evaluate(state, request)
        assert result.slippage_pips == first.slippage_pips
        assert result.reject_probability == first.reject_probability
        assert result.components == first.components


def test_determinism_with_rng_same_seed() -> None:
    """Same seed → identical SlippageResult.

    Each call drives a fresh ``Generator`` from the same seed, so the
    one rng.uniform draw inside :func:`evaluate` consumes the same
    bytes both times.
    """
    state = _eurusd_state()
    request = _req()
    rng_a = np.random.default_rng(seed=42)
    rng_b = np.random.default_rng(seed=42)
    result_a = evaluate(state, request, rng=rng_a)
    result_b = evaluate(state, request, rng=rng_b)
    assert result_a.slippage_pips == result_b.slippage_pips
    assert result_a.components == result_b.components


def test_determinism_with_rng_different_seeds() -> None:
    """Different seeds → distinguishable SlippageResult.

    With ``_NOISE_HALF_WIDTH_PIPS = 0.05`` and seeds 42 vs 7, the
    uniform draws differ and the slip values diverge.
    """
    state = _eurusd_state()
    request = _req()
    result_42 = evaluate(state, request, rng=np.random.default_rng(seed=42))
    result_7 = evaluate(state, request, rng=np.random.default_rng(seed=7))
    assert result_42.slippage_pips != result_7.slippage_pips


def test_determinism_with_rng_locks_two_distinct_seeds() -> None:
    """Two locked seeds produce two locked noise values.

    The reviewer's "tests lock behavior on at least two distinct seeds"
    requirement. We don't lock the *exact* slip value (that would
    fragilize the test under any coefficient bump) but we do lock that
    each seed produces a result with a noise component matching the
    fresh-rng signature.
    """
    state = _eurusd_state()
    request = _req()
    for seed in (42, 7):
        rng = np.random.default_rng(seed=seed)
        expected_noise = float(rng.uniform(-0.05, 0.05))
        # Reset the rng to consume the same byte stream inside evaluate.
        rng = np.random.default_rng(seed=seed)
        result = evaluate(state, request, rng=rng)
        assert result.components["noise"] == pytest.approx(expected_noise)


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_unknown_symbol_raises() -> None:
    """Unknown symbol → ValueError (not silent default fill)."""
    state = MarketState(
        symbol="ZZZ",
        ts_utc=_MID_SESSION_TS,
        realized_vol_5m=0.10,
    )
    request = _req()
    with pytest.raises(ValueError, match="unknown symbol"):
        evaluate(state, request)


def test_unknown_order_type_raises() -> None:
    """order_type='bizarre' → pydantic ValidationError at request build.

    Pydantic enforces the Literal on the model; we never reach
    :func:`evaluate` with a bogus order type. The reviewer's spec
    accepts either ValueError or ValidationError; we get the latter.
    """
    with pytest.raises(ValidationError):
        SlippageRequest(
            side="buy",
            order_type="bizarre",  # type: ignore[arg-type]
            size_lots=0.01,
        )


def test_unknown_side_raises() -> None:
    """side='maybe' → pydantic ValidationError. Companion guard to order type."""
    with pytest.raises(ValidationError):
        SlippageRequest(
            side="maybe",  # type: ignore[arg-type]
            order_type="market",
            size_lots=0.01,
        )


def test_negative_size_raises() -> None:
    """Negative size_lots → ValueError. Programmer error, not silently 0."""
    state = _eurusd_state()
    request = _req(size_lots=0.01)
    # The Request itself allows negative (no field constraint), but
    # evaluate() defends. Build an invalid request via construction.
    bad_request = SlippageRequest(side="buy", order_type="market", size_lots=-0.01)
    with pytest.raises(ValueError, match="size_lots must be non-negative"):
        evaluate(state, bad_request)
    # And the valid request still works.
    evaluate(state, request)


def test_naive_datetime_raises() -> None:
    """Naive datetime in MarketState.ts_utc → ValueError at evaluate time."""
    naive_ts = datetime(2024, 6, 12, 10, 0)  # No tzinfo
    state = MarketState(
        symbol="EURUSD",
        ts_utc=naive_ts,
        realized_vol_5m=0.10,
    )
    with pytest.raises(ValueError, match="tz-aware"):
        evaluate(state, _req())


def test_negative_realized_vol_raises() -> None:
    """Negative realized_vol_5m → ValueError at evaluate time."""
    state = MarketState(
        symbol="EURUSD",
        ts_utc=_MID_SESSION_TS,
        realized_vol_5m=-0.05,
    )
    with pytest.raises(ValueError, match="realized_vol_5m must be non-negative"):
        evaluate(state, _req())


# --------------------------------------------------------------------------- #
# Calibration integrity
# --------------------------------------------------------------------------- #
def test_all_calibrations_marked_uncertain() -> None:
    """Every shipped calibration entry is ``confidence='uncertain'``.

    Until Gate-2B fill recording calibrates these against live MT5 demo
    fills, no entry may be marked ``"high"``. This is the W3/W4 pattern
    enforced module-wide: a ``"high"``-marked calibration is a green
    light for downstream live-account sizing decisions; we don't have
    that evidence yet.
    """
    for symbol, entry in CALIBRATIONS.items():
        assert entry.confidence == "uncertain", (
            f"{symbol} calibration is {entry.confidence!r}; expected 'uncertain' "
            f"until Gate-2B live recording fills the gap."
        )


def test_calibration_covers_all_supported_symbols() -> None:
    """Every symbol in SUPPORTED_SYMBOLS has a calibration entry."""
    from propfarm.data.quality import SUPPORTED_SYMBOLS

    missing = set(SUPPORTED_SYMBOLS) - set(CALIBRATIONS.keys())
    assert not missing, f"calibrations missing for: {sorted(missing)}"


def test_calibration_stress_multipliers_in_spec_band() -> None:
    """FX stress mult ~10-20, indices ~5. Locks the spec.

    The reviewer's "default magnitude" criterion: FX gets the big
    amplifier (~15), indices get the smaller one (~5), gold sits in the
    middle. Locking this band here catches an accidental coefficient
    bump that would push a stress-replay's slip out of the [5, 20] pip
    spec for FX or push indices into FX-tier amplification.
    """
    fx_symbols = ("EURUSD", "GBPUSD", "USDJPY")
    index_symbols = ("GER40", "US100")
    for sym in fx_symbols:
        assert 10.0 <= CALIBRATIONS[sym].stress_multiplier <= 20.0, (
            f"{sym} stress_multiplier outside [10, 20]"
        )
    for sym in index_symbols:
        assert 3.0 <= CALIBRATIONS[sym].stress_multiplier <= 7.0, (
            f"{sym} stress_multiplier outside [3, 7]"
        )


def test_xauusd_base_pips_in_spec_band() -> None:
    """Gold base ~5-10 pips, much wider than FX. Spec lock."""
    assert 5.0 <= CALIBRATIONS["XAUUSD"].base_pips <= 10.0


def test_fx_majors_base_pips_in_spec_band() -> None:
    """FX majors base ~0.3 pips. Spec lock."""
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        assert 0.2 <= CALIBRATIONS[sym].base_pips <= 0.6, f"{sym} base_pips outside [0.2, 0.6]"


def test_limit_reject_in_valid_probability_range() -> None:
    """Every limit_reject_at_baseline is a valid probability ∈ [0, 1]."""
    for symbol, entry in CALIBRATIONS.items():
        assert 0.0 <= entry.limit_reject_at_baseline <= 1.0, (
            f"{symbol} limit_reject_at_baseline outside [0, 1]"
        )


# --------------------------------------------------------------------------- #
# Components accounting
# --------------------------------------------------------------------------- #
def test_components_sum_to_total_no_rng() -> None:
    """Components reconcile to slippage_pips for market orders (no-noise path).

    Invariant: ``slippage_pips == (base + vol_term + size_term + minute_term)
    * stress_factor + noise``. With ``rng=None``, noise is 0 and the
    aggregation is exact within float-arithmetic tolerance.
    """
    state = _eurusd_state(stress=True)  # exercise stress_factor != 1.0
    result = evaluate(state, _req())
    comp = result.components
    aggregated = (comp["base"] + comp["vol_term"] + comp["size_term"] + comp["minute_term"]) * comp[
        "stress_factor"
    ] + comp["noise"]
    assert result.slippage_pips == pytest.approx(aggregated)


def test_components_sum_to_total_with_rng() -> None:
    """Components reconcile to slippage_pips with noise active."""
    state = _eurusd_state()
    rng = np.random.default_rng(seed=123)
    result = evaluate(state, _req(), rng=rng)
    comp = result.components
    aggregated = (comp["base"] + comp["vol_term"] + comp["size_term"] + comp["minute_term"]) * comp[
        "stress_factor"
    ] + comp["noise"]
    assert result.slippage_pips == pytest.approx(aggregated)


def test_components_keys_present() -> None:
    """All six expected component keys are present in every result."""
    result = evaluate(_eurusd_state(), _req())
    expected_keys = {"base", "vol_term", "size_term", "minute_term", "stress_factor", "noise"}
    assert set(result.components.keys()) == expected_keys


def test_limit_order_slippage_zero_regardless_of_components() -> None:
    """For limit orders, ``slippage_pips`` is 0 even though components are populated.

    The components dict tells diagnostics "what would the slip have been
    if this were a market order" — useful for debugging — but the slip
    field is 0 by the order-type contract.
    """
    state = _eurusd_state(stress=True)  # would produce ~7+ pips if market
    result = evaluate(state, _req(order_type="limit"))
    assert result.slippage_pips == 0.0
    # But components reflect the hypothetical market slip:
    assert result.components["base"] > 0.0
    assert result.components["stress_factor"] > 1.0


# --------------------------------------------------------------------------- #
# Per-symbol coverage smoke
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", list(CALIBRATIONS.keys()))
def test_every_symbol_evaluates(symbol: str) -> None:
    """Smoke test: evaluate produces a sane result for every supported symbol."""
    state = MarketState(
        symbol=symbol,
        ts_utc=_MID_SESSION_TS,
        realized_vol_5m=0.10,
    )
    market = evaluate(state, _req(order_type="market"))
    limit = evaluate(state, _req(order_type="limit"))
    stop = evaluate(state, _req(order_type="stop"))
    assert market.slippage_pips >= 0.0
    assert limit.slippage_pips == 0.0
    assert limit.reject_probability > 0.0
    assert stop.slippage_pips > 0.0


# --------------------------------------------------------------------------- #
# Calibration override
# --------------------------------------------------------------------------- #
def test_calibration_override_used_in_lieu_of_registry() -> None:
    """An explicit ``calibration`` argument overrides the registry lookup."""
    override = SlippageCalibrationEntry(
        symbol="EURUSD",
        base_pips=10.0,
        vol_coef=0.0,
        size_coef=0.0,
        stress_multiplier=1.0,
        limit_reject_at_baseline=0.0,
        confidence="uncertain",
        snapshot_date=date(2026, 5, 12),
        snapshot_source="test-override",
    )
    state = _eurusd_state()
    result = evaluate(state, _req(), calibration=override)
    # base=10 pips, no vol/size/stress contribution -> slip ~= 10 pips
    # plus minute_term (0 at 10:00 UTC).
    assert result.slippage_pips == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Minute-of-day axis
# --------------------------------------------------------------------------- #
def test_ny_close_minute_adds_pips() -> None:
    """21:30 UTC (NY close window peak) → strictly more slip than 10:00 UTC."""
    quiet_state = _eurusd_state(ts=_MID_SESSION_TS)
    ny_close_ts = datetime(2024, 6, 12, 21, 30, tzinfo=UTC)
    ny_close_state = _eurusd_state(ts=ny_close_ts)
    assert (
        evaluate(ny_close_state, _req()).slippage_pips > evaluate(quiet_state, _req()).slippage_pips
    )


def test_minute_term_zero_outside_window() -> None:
    """``minute_term`` is exactly 0 outside the 21:00-22:00 UTC window."""
    for hour in (0, 5, 10, 15, 20, 22, 23):
        ts = datetime(2024, 6, 12, hour, 0, tzinfo=UTC)
        state = _eurusd_state(ts=ts)
        result = evaluate(state, _req())
        assert result.components["minute_term"] == 0.0, (
            f"minute_term should be 0 at hour {hour}, got {result.components['minute_term']}"
        )


def test_zero_size_lots_allowed() -> None:
    """size_lots=0 returns slip equal to baseline (size_term=0), no error.

    The fill engine can shorthand "no position" through the same code
    path. Slip is base + vol + minute (no size contribution).
    """
    state = _eurusd_state()
    result = evaluate(state, _req(size_lots=0.0))
    assert result.components["size_term"] == 0.0
    assert result.slippage_pips >= 0.0
