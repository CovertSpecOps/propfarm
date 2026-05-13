"""Slippage model parameterized by ``(vol, size, minute_of_day, order_type)`` (Task 7.1).

Why this module exists
----------------------
The fill engine (Task 7.2) needs an empirical model for the slippage between
the requested price and the actual fill at any ``(symbol, market_state,
request)`` triple. Slippage is **the** silent cost-leak source on a retail
prop-firm account: spreads are widely published, commissions are
contractual, swaps are nightly and tabulated — but slippage is the
broker-discretion residual that turns a +0.3 R backtest into a -0.1 R
live result. Mis-model it and the placebo gate either falsely raises
("strategy is losing money it shouldn't") or, worse, falsely passes ("our
random strategy is profitable" — which means alpha is leaking into the
fills somewhere).

The model is **order-type-dependent**:

* **Market orders** take the full modelled slippage. The size + vol + base
  + minute terms all contribute; ``reject_probability`` is **0** (a market
  order on a liquid retail symbol never rejects — it just fills worse).
* **Limit orders** take **zero slippage**. They either fill at the limit
  price or they do not fill at all. The model output exposes a separate
  ``reject_probability`` instead, which the fill engine consumes to decide
  whether the requested limit price was hit during the bar but skipped.
* **Stop orders** take the **full slippage at trigger** — once triggered
  they become market orders, so the same formula as market orders applies.
  ``reject_probability`` is **0** (a triggered stop fills, the only
  question is at what worse price).

Stress mode
-----------
The ``stress_mode`` flag on :class:`MarketState` amplifies slippage by the
calibrated ``stress_multiplier`` (typically 10-20x for FX, ~5x for indices).
This is consumed by the Task 10.2 stress-replay library, which loads
historical event windows (Lehman, SNB, GBP-flash, COVID, UK-gilts, SVB)
and runs the strategy through them with the multiplier active. Without
stress amplification, a strategy that holds a 1-lot EURUSD long through
the SNB unpegging would book a few-pip slip when in reality the fill was
~150 pips adverse; we would systematically *underestimate* tail loss.

Parameterization
----------------
The functional form is::

    raw = base_pips + vol_coef * realized_vol_5m + size_coef * log(size_lots + 1) + minute_term
    slippage = raw * stress_factor + noise

Where:

* ``base_pips`` is the symbol's baseline at typical volatility, 0.01 lot,
  mid-session (e.g. ~0.3 pips for FX majors, ~7 pips for XAUUSD).
* ``vol_coef`` translates **annualized return vol** into adverse pips.
  Annualized vol of 0.10 (10%) is "typical" for FX majors; vol=0.50 is
  an event-day regime that the coefficient scales linearly through.
* ``size_coef`` multiplies ``log(size_lots + 1)``. The ``log(.+1)`` shape
  is **sub-linear at retail size** (0.01 vs 0.1 lot differ by <0.1 pip)
  and **accelerates at institutional size** (1 lot adds ~0.35 pips on
  top of a 0.01-lot baseline). The log-curve flattens further beyond
  ~10 lots, which matches dealer-quote behavior where post-RFQ pricing
  takes over from streamed retail quotes.
* ``minute_term`` is a small per-minute adverse adjustment for the
  illiquid New-York-close hour (21:00-22:00 UTC). It is zero outside
  that window. This is the "minute_of_day" axis the reviewer requires.
* ``stress_factor`` is ``stress_multiplier`` when
  ``market_state.stress_mode`` is True, else 1.0. It additionally bumps
  slightly during news windows (``news_window=True`` doubles the slip)
  to honor the "news affects FX more" comment in the calibration spec.
* ``noise`` is **zero** when ``rng`` is None (the deterministic path).
  When a generator is provided, a small symmetric uniform noise of
  ±0.05 pips is added — this is the only source of non-determinism in
  the model and exists only so backtests that want to model micro-jitter
  on top of the deterministic floor can opt in.

Determinism
-----------
* :func:`evaluate` with ``rng=None`` is **byte-identical** across calls
  on the same ``(market_state, request, calibration)`` input — locked by
  ``test_determinism_no_rng``.
* :func:`evaluate` with a seeded ``rng`` is **byte-identical** across
  runs of that same seed and **distinguishable** across different seeds
  — locked by ``test_determinism_with_rng``. The noise draw consumes
  exactly one ``rng.uniform()`` call per :func:`evaluate` invocation.

Confidence flag
---------------
Every :class:`SlippageCalibrationEntry` carries a ``confidence`` field
matching the W3/W4 commission/swap pattern. **All shipped entries are
marked ``"uncertain"``** until live calibration data lands. The
Gate-2B fill-recording runbook (separate Wave 6b task) is what will
upgrade these to ``"high"`` after a sufficient sample of paired
requested-price / actual-fill observations has been collected from the
FTMO MT5 demo account.

Coordination with Task 6.1 (spread model)
-----------------------------------------
Both this module and ``propfarm.sim.spread`` need a ``MarketState`` model
that describes the prevailing market conditions at the time a fill is
being simulated. **First-writer-wins:** at the time slippage.py was first
written, ``propfarm.sim.spread`` did not yet exist, so :class:`MarketState`
lives here and Task 6.1 will import it from ``propfarm.sim.slippage``.
If a future refactor moves it to a shared module (e.g.
``propfarm.sim.market_state``), both consumers should be updated together
and this docstring updated to reflect the new home.

Public API
----------
* :class:`MarketState`
* :class:`SlippageRequest`
* :class:`SlippageCalibrationEntry`
* :class:`SlippageResult`
* :func:`evaluate`
* :data:`CALIBRATIONS`
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import SUPPORTED_SYMBOLS

# --------------------------------------------------------------------------- #
# Module-level constants
# --------------------------------------------------------------------------- #

#: Default realized-vol value used when ``MarketState.realized_vol_5m`` is
#: ``None``. Set to 10% annualized — the rough midpoint of EURUSD's realized
#: vol distribution over 2015-2025. Picking a fixed default rather than
#: failing keeps the model usable in the unit tests and early placebo runs
#: before the realized-vol feature pipeline is wired in.
_DEFAULT_REALIZED_VOL: Final[float] = 0.10

#: Half-width of the symmetric uniform noise (pips) added when an ``rng`` is
#: supplied. Chosen to be small enough that ``test_market_order_takes_slippage``
#: stays inside ``[0, 1]`` for the EURUSD baseline (~0.5 pips), and large
#: enough that two distinct seeds produce distinguishable outputs.
_NOISE_HALF_WIDTH_PIPS: Final[float] = 0.05

#: News-window multiplier. When ``MarketState.news_window`` is True and
#: ``stress_mode`` is False, slip is doubled. When ``stress_mode`` is True,
#: the news flag is ignored (the much larger ``stress_multiplier`` already
#: captures the regime). This keeps the model's behavior across the four
#: flag combinations monotonic in adversity.
_NEWS_FACTOR: Final[float] = 2.0

#: Wall-clock hour (UTC) at which the New-York-close illiquidity window
#: begins. The window runs from this hour to (but not including)
#: ``_NY_CLOSE_END_HOUR_UTC``. Slippage gets a per-minute additive bump
#: scaled by ``_NY_CLOSE_PEAK_PIPS`` peaking at the window's midpoint.
_NY_CLOSE_START_HOUR_UTC: Final[int] = 21
_NY_CLOSE_END_HOUR_UTC: Final[int] = 22

#: Peak additive pips at the midpoint of the NY-close window. Empirically
#: retail brokers widen by ~0.3-0.5 pips on FX majors and ~1-2 pips on
#: metals/indices during this hour; we encode a conservative 0.3 here.
_NY_CLOSE_PEAK_PIPS: Final[float] = 0.3

#: Snapshot date for the seeded calibration entries. Mirrors the W3/W4
#: convention so a future re-calibration moves the snapshot date in lockstep
#: across cost-model modules.
_SNAPSHOT_DATE: Final[date] = date(2026, 5, 12)
_SNAPSHOT_SOURCE: Final[str] = (
    "docs/runbooks/gate-2b-fill-recording.md (placeholder — calibration uncertain)"
)


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
# ``MarketState`` is re-exported from :mod:`propfarm.sim.market` — the
# canonical source for the shared market-context model. Wave 6b shipped a
# local copy here; W6b reviewer flagged the duplication as a HIGH-severity
# coupling problem for Wave 6c (the fill engine would otherwise accumulate
# adapter code between two nominally-distinct Pydantic types of the same
# name). Consolidated 2026-05-13.
from propfarm.sim.market import MarketState  # noqa: E402 — re-export


class SlippageRequest(BaseModel):
    """One slippage-evaluation request from the fill engine.

    Attributes
    ----------
    side : Literal["buy", "sell"]
        Trade direction. Slippage is always **adverse** to this direction
        — for a buy, slip pushes the fill price higher; for a sell, lower.
        The ``slippage_pips`` field on :class:`SlippageResult` is the
        magnitude only (always >= 0); the fill engine applies the sign
        by combining with this ``side``.
    order_type : Literal["market", "limit", "stop"]
        Order type. Drives the order-type-dependent branch in
        :func:`evaluate`. Pydantic raises ``ValidationError`` for unknown
        types — test ``test_unknown_order_type_raises`` locks this.
    size_lots : float
        Order size in lots. Must be non-negative; zero is allowed (and
        returns zero slippage) so the fill engine can shorthand "no
        position" through the same path.
    """

    model_config = ConfigDict(frozen=True)

    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop"]
    size_lots: float


class SlippageCalibrationEntry(BaseModel):
    """Per-symbol slippage-model calibration.

    The functional form is::

        raw = base_pips
              + vol_coef  * realized_vol
              + size_coef * log(size_lots + 1)
              + minute_term
        slippage = raw * stress_factor + noise

    where ``stress_factor`` is ``stress_multiplier`` when stress mode is
    on, the news-factor (2.0) when only ``news_window`` is True, and 1.0
    otherwise. See the module docstring for the full derivation.

    Attributes
    ----------
    symbol : str
        Trading symbol this entry applies to.
    base_pips : float
        Baseline slippage at typical vol, 0.01 lot, mid-session, no news,
        no stress. Must be non-negative.
    vol_coef : float
        Coefficient on ``realized_vol_5m`` (annualized). Must be non-negative.
    size_coef : float
        Coefficient on ``log(size_lots + 1)``. Must be non-negative.
    stress_multiplier : float
        Multiplicative amplification applied when
        ``MarketState.stress_mode`` is True. Must be >= 1.0 — stress can
        only make slippage worse, never better.
    limit_reject_at_baseline : float
        Probability ``[0, 1]`` that a limit order rejects (the bar's
        extreme never reached the limit price, or the broker did not
        honor the touch). The Task 7.2 fill engine consumes this as a
        Bernoulli draw against an rng.
    confidence : Literal["high", "uncertain"]
        Runtime marker for whether the calibration values came from a
        primary, verified source (``"high"``) or are placeholders
        awaiting calibration (``"uncertain"``). Every shipped entry is
        ``"uncertain"`` until Gate-2B live recording fills the gap.
    snapshot_date : datetime.date
        The date of the underlying calibration data — for placeholder
        entries, the date the placeholder was committed.
    snapshot_source : str
        Repo-relative path to the calibration evidence file (markdown
        or parquet). Asserted to exist by the calibration runbook tests.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_pips: float
    vol_coef: float
    size_coef: float
    stress_multiplier: float
    limit_reject_at_baseline: float
    confidence: Literal["high", "uncertain"]
    snapshot_date: date
    snapshot_source: str


class SlippageResult(BaseModel):
    """Output of :func:`evaluate`.

    Attributes
    ----------
    symbol : str
        Echoes the requested symbol.
    ts_utc : datetime.datetime
        Echoes ``market_state.ts_utc``.
    order_type : Literal["market", "limit", "stop"]
        Echoes the requested order type.
    slippage_pips : float
        Slippage magnitude in pips (always >= 0). Always 0.0 for limit
        orders. The fill engine applies the sign by combining with
        ``request.side``.
    reject_probability : float
        Probability ``[0, 1]`` that the order rejects. Always 0.0 for
        market and stop orders. For limit orders, equals the calibrated
        ``limit_reject_at_baseline`` (could be extended later to depend
        on vol or distance-to-mid; documented as a TODO in the module).
    components : dict[str, float]
        Component breakdown::

            {
                "base":          base_pips,
                "vol_term":      vol_coef  * effective_vol,
                "size_term":     size_coef * log(size_lots + 1),
                "minute_term":   per-minute NY-close adjustment,
                "stress_factor": multiplicative regime amplifier,
                "noise":         additive uniform noise (0 if rng=None),
            }

        Invariant (locked by ``test_components_sum_to_total`` for the
        no-noise path): ``slippage_pips == (base + vol_term + size_term +
        minute_term) * stress_factor + noise`` for market and stop orders;
        ``slippage_pips == 0`` for limit orders regardless of components.
    calibration_confidence : Literal["high", "uncertain"]
        Echoes ``calibration.confidence`` so consumers can gate downstream
        decisions on it without re-fetching the calibration entry.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts_utc: datetime
    order_type: Literal["market", "limit", "stop"]
    slippage_pips: float
    reject_probability: float
    components: dict[str, float]
    calibration_confidence: Literal["high", "uncertain"]


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str) -> None:
    """Reject naive datetimes — every caller must pass tz-aware UTC."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def _minute_term_pips(ts_utc: datetime) -> float:
    """Compute the per-minute NY-close adverse adjustment in pips.

    The window runs from 21:00 UTC (inclusive) to 22:00 UTC (exclusive)
    and follows a symmetric triangular shape that peaks at
    21:30 UTC at :data:`_NY_CLOSE_PEAK_PIPS` and tapers to zero at
    both edges. The triangular shape (rather than a flat or rectangular
    bump) makes the slippage curve a continuous function of
    ``minute_of_day`` — a requirement of the reviewer's "minute_of_day"
    parameterization axis.

    Outside the window, the function returns 0.0 exactly.
    """
    hour = ts_utc.hour
    if hour != _NY_CLOSE_START_HOUR_UTC:
        # The window is exactly one hour wide and starts at hour 21 UTC.
        # Anything else is mid-session for the purposes of this term.
        return 0.0
    minute = ts_utc.minute
    # Triangular kernel: peak at minute 30, zero at minute 0 and minute 60.
    distance_from_peak = abs(minute - 30)
    # 30 minutes from peak -> 0 pips; 0 minutes from peak -> peak pips.
    fraction = max(0.0, 1.0 - distance_from_peak / 30.0)
    return _NY_CLOSE_PEAK_PIPS * fraction


def _resolve_calibration(
    symbol: str, calibration: SlippageCalibrationEntry | None
) -> SlippageCalibrationEntry:
    """Look up the calibration entry for ``symbol`` or use the passed override.

    If ``calibration`` is provided, it is used verbatim (the symbol field
    is not cross-checked — the caller is responsible for not passing a
    GBPUSD calibration with a EURUSD market state, and the fill engine
    will never do that).

    If ``calibration`` is None and ``symbol`` is in :data:`CALIBRATIONS`,
    the registered entry is returned. Otherwise ``ValueError`` is raised
    — silently filling with a default would let an upstream typo
    propagate into the placebo gate as "we filled at zero slip" which is
    exactly the failure mode this module exists to prevent.
    """
    if calibration is not None:
        return calibration
    try:
        return CALIBRATIONS[symbol]
    except KeyError as exc:
        raise ValueError(
            f"unknown symbol {symbol!r} for slippage calibration; "
            f"available symbols: {sorted(CALIBRATIONS.keys())}"
        ) from exc


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def evaluate(
    market_state: MarketState,
    request: SlippageRequest,
    *,
    calibration: SlippageCalibrationEntry | None = None,
    rng: np.random.Generator | None = None,
) -> SlippageResult:
    """Compute slippage for one ``(market_state, request)`` pair.

    Order-type-specific behavior
    ----------------------------
    * **market**: slippage = ``(base + vol_term + size_term + minute_term)
      * stress_factor + noise``; reject_probability = 0.0.
    * **limit**:  slippage = 0.0; reject_probability =
      ``limit_reject_at_baseline``. Limit orders fill at the limit price
      or not at all — they take no slippage by construction. The reject
      probability is what the fill engine consumes as a Bernoulli to
      decide whether the limit was honored.
    * **stop**:   slippage = same formula as market; reject_probability =
      0.0. A triggered stop is functionally a market order — the only
      question is at what worse price.

    Stress and news regimes
    -----------------------
    * If ``market_state.stress_mode`` is True, the ``stress_factor`` in
      ``components`` equals ``calibration.stress_multiplier``.
    * Else if ``market_state.news_window`` is True, the ``stress_factor``
      equals :data:`_NEWS_FACTOR` (2.0).
    * Otherwise, ``stress_factor`` equals 1.0.

    Determinism
    -----------
    * ``rng=None`` (default): fully deterministic. The ``noise`` component
      is exactly 0.0. Two calls with the same inputs return byte-identical
      :class:`SlippageResult` instances (modulo dict identity).
    * ``rng`` provided: a single ``rng.uniform(-h, h)`` draw is added as
      additive noise, where ``h = _NOISE_HALF_WIDTH_PIPS = 0.05``. Two
      calls with the same seed produce identical results; two different
      seeds produce statistically distinguishable results.

    Parameters
    ----------
    market_state : MarketState
        Market regime snapshot.
    request : SlippageRequest
        The order to evaluate slippage for.
    calibration : SlippageCalibrationEntry, optional
        Override the default lookup. If None (default), the entry is
        fetched from :data:`CALIBRATIONS` by ``market_state.symbol``.
    rng : numpy.random.Generator, optional
        Source of the noise draw. If None (default), the result is fully
        deterministic.

    Returns
    -------
    SlippageResult
        The computed slip + reject probability + component breakdown.

    Raises
    ------
    ValueError
        * If ``market_state.ts_utc`` is naive.
        * If ``request.size_lots`` is negative.
        * If ``market_state.realized_vol_5m`` is provided and is negative.
        * If ``market_state.symbol`` is not in :data:`CALIBRATIONS` and
          no explicit ``calibration`` is provided.
    """
    _require_utc(market_state.ts_utc, arg_name="market_state.ts_utc")
    if request.size_lots < 0.0:
        raise ValueError(
            f"size_lots must be non-negative, got {request.size_lots!r} "
            f"for symbol {market_state.symbol!r}"
        )
    if market_state.realized_vol_5m is not None and market_state.realized_vol_5m < 0.0:
        raise ValueError(
            f"realized_vol_5m must be non-negative, got "
            f"{market_state.realized_vol_5m!r} for symbol {market_state.symbol!r}"
        )

    cal = _resolve_calibration(market_state.symbol, calibration)

    # Effective vol: fall back to the module default if the caller did not
    # supply one. The default is a typical-regime placeholder so the model
    # remains usable before the realized-vol pipeline is wired in.
    effective_vol = (
        market_state.realized_vol_5m
        if market_state.realized_vol_5m is not None
        else _DEFAULT_REALIZED_VOL
    )

    # Component breakdown — computed for *every* order type, even limit
    # orders, so the components dict tells the consumer "what would the
    # slip have been if this were a market order" for diagnostics.
    base = cal.base_pips
    vol_term = cal.vol_coef * effective_vol
    size_term = cal.size_coef * math.log(request.size_lots + 1.0)
    minute_term = _minute_term_pips(market_state.ts_utc)

    # Stress factor: stress_mode dominates news_window (the two flags are
    # not summed — stress mode by construction subsumes the news regime).
    if market_state.stress_mode:
        stress_factor = cal.stress_multiplier
    elif market_state.news_window:
        stress_factor = _NEWS_FACTOR
    else:
        stress_factor = 1.0

    # Noise: only drawn if a generator was provided. Single uniform draw
    # per call so the rng consumption is predictable across replays.
    noise = (
        float(rng.uniform(-_NOISE_HALF_WIDTH_PIPS, _NOISE_HALF_WIDTH_PIPS))
        if rng is not None
        else 0.0
    )

    raw_pre_stress = base + vol_term + size_term + minute_term
    raw = raw_pre_stress * stress_factor + noise
    # Clip at zero: slip can never be negative (no positive-slip free
    # lunch). The plan's spec test "Slippage adverse to direction in
    # >=95% of cases" enforces this directly via the absence of a
    # negative branch in the noise draw (noise alone cannot push a
    # non-degenerate raw below zero).
    slippage_pips_market = max(0.0, raw)

    if request.order_type == "limit":
        slippage_pips = 0.0
        reject_probability = cal.limit_reject_at_baseline
    elif request.order_type in ("market", "stop"):
        slippage_pips = slippage_pips_market
        reject_probability = 0.0
    else:  # pragma: no cover — pydantic ValidationError fires first
        raise ValueError(
            f"unknown order_type {request.order_type!r}; expected one of: market, limit, stop"
        )

    components = {
        "base": base,
        "vol_term": vol_term,
        "size_term": size_term,
        "minute_term": minute_term,
        "stress_factor": stress_factor,
        "noise": noise,
    }

    return SlippageResult(
        symbol=market_state.symbol,
        ts_utc=market_state.ts_utc,
        order_type=request.order_type,
        slippage_pips=slippage_pips,
        reject_probability=reject_probability,
        components=components,
        calibration_confidence=cal.confidence,
    )


# --------------------------------------------------------------------------- #
# Calibration registry — Phase 0 seed values
# --------------------------------------------------------------------------- #
#
# Magnitudes (all uncertain until Gate-2B live recording fills the gap):
#
# +---------+-----------+----------+-----------+---------------+----------+
# | symbol  | base_pips | vol_coef | size_coef | stress_mult.  | reject   |
# +=========+===========+==========+===========+===============+==========+
# | EURUSD  | 0.3       | 2.0      | 0.5       | 15.0          | 0.02     |
# | GBPUSD  | 0.4       | 2.5      | 0.6       | 15.0          | 0.02     |
# | USDJPY  | 0.3       | 2.0      | 0.5       | 15.0          | 0.02     |
# | XAUUSD  | 7.0       | 30.0     | 4.0       | 12.0          | 0.05     |
# | GER40   | 0.8       | 4.0      | 1.0       | 5.0           | 0.03     |
# | US100   | 0.6       | 3.0      | 0.8       | 5.0           | 0.03     |
# +---------+-----------+----------+-----------+---------------+----------+
#
# FX-major rationale:
#   - base_pips ~0.3-0.4: matches retail-broker observed slip on quiet-hour
#     0.01-lot market orders (median ~0.2-0.5 pips per Dukascopy 2023-2024
#     event-day percentile work pre-staged for Phase 1).
#   - vol_coef 2.0-2.5: vol=0.10 (typical) -> 0.20-0.25 pips contribution;
#     vol=0.50 (event) -> 1.0-1.25 pips contribution. Adds together with
#     stress_multiplier (15x) and news (2x) when both apply.
#   - size_coef 0.5-0.6: log(1.01) ~ 0.01 -> ~0.005 pips at 0.01 lot;
#     log(2) ~ 0.69 -> ~0.35 pips at 1 lot. Sub-linear at retail.
#   - stress_multiplier 15: SNB unpegging-style amplification puts a
#     typical-regime ~0.5 pip slip at ~7.5 pips, inside the [5, 20]
#     spec band for stress mode.
#   - limit_reject_at_baseline 0.02: 2% baseline rejection on tight limits,
#     consistent with retail-broker fill-quality reports.
#
# XAUUSD rationale:
#   - base_pips 7.0: gold spreads at $3500/oz are 50-100x wider than FX
#     measured in pips; this midpoint sits inside the [5, 10] spec band.
#   - vol_coef 30.0: gold realized vol is structurally higher; 0.10 vol
#     adds ~3 pips, 0.30 vol adds ~9 pips.
#   - size_coef 4.0: gold liquidity drops off much faster with size;
#     1 lot adds ~2.8 pips on top of 0.01-lot baseline.
#   - stress_multiplier 12: news affects gold less than FX (no central
#     bank shock can re-peg a metal), so the multiplier sits below
#     the FX 15x but still above the index 5x.
#
# Index (GER40 / US100) rationale:
#   - base_pips 0.6-0.8 index points: matches CFD-broker observed slip on
#     0.01-lot index orders during cash session.
#   - stress_multiplier 5: indices have fewer overnight-gap-style events
#     than FX (the SNB unpegging is FX-specific) so the multiplier is
#     lower; aligns with the "~5 for indices" spec line.
#
# Every entry is confidence="uncertain". The Gate-2B fill-recording
# runbook is what upgrades these to "high" after enough paired
# requested-price / actual-fill observations have been collected from
# the FTMO MT5 demo account.

CALIBRATIONS: Final[dict[str, SlippageCalibrationEntry]] = {
    "EURUSD": SlippageCalibrationEntry(
        symbol="EURUSD",
        base_pips=0.3,
        vol_coef=2.0,
        size_coef=0.5,
        stress_multiplier=15.0,
        limit_reject_at_baseline=0.02,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
    "GBPUSD": SlippageCalibrationEntry(
        symbol="GBPUSD",
        base_pips=0.4,
        vol_coef=2.5,
        size_coef=0.6,
        stress_multiplier=15.0,
        limit_reject_at_baseline=0.02,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
    "USDJPY": SlippageCalibrationEntry(
        symbol="USDJPY",
        base_pips=0.3,
        vol_coef=2.0,
        size_coef=0.5,
        stress_multiplier=15.0,
        limit_reject_at_baseline=0.02,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
    "XAUUSD": SlippageCalibrationEntry(
        symbol="XAUUSD",
        base_pips=7.0,
        vol_coef=30.0,
        size_coef=4.0,
        stress_multiplier=12.0,
        limit_reject_at_baseline=0.05,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
    "GER40": SlippageCalibrationEntry(
        symbol="GER40",
        base_pips=0.8,
        vol_coef=4.0,
        size_coef=1.0,
        stress_multiplier=5.0,
        limit_reject_at_baseline=0.03,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
    "US100": SlippageCalibrationEntry(
        symbol="US100",
        base_pips=0.6,
        vol_coef=3.0,
        size_coef=0.8,
        stress_multiplier=5.0,
        limit_reject_at_baseline=0.03,
        confidence="uncertain",
        snapshot_date=_SNAPSHOT_DATE,
        snapshot_source=_SNAPSHOT_SOURCE,
    ),
}


# Cross-check: every symbol declared in the data layer has a calibration.
# A symbol added to SUPPORTED_SYMBOLS without a calibration entry here
# would otherwise surface only at runtime as a ValueError from
# `_resolve_calibration`; surfacing it at import time keeps the failure
# loud and local to this module.
_MISSING_CALIBRATIONS = set(SUPPORTED_SYMBOLS) - set(CALIBRATIONS.keys())
if _MISSING_CALIBRATIONS:  # pragma: no cover — import-time invariant
    raise RuntimeError(
        f"slippage CALIBRATIONS is missing entries for: "
        f"{sorted(_MISSING_CALIBRATIONS)}; either add them above or "
        f"remove the symbols from SUPPORTED_SYMBOLS."
    )


__all__ = [
    "CALIBRATIONS",
    "MarketState",
    "SlippageCalibrationEntry",
    "SlippageRequest",
    "SlippageResult",
    "evaluate",
]
