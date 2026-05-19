"""Fill engine — the single chokepoint through which every Phase-1+ strategy
submits an order during backtest (Task 7.2, Wave 6c).

Why this module exists
----------------------
Every cost component the simulator models (spread / slippage / commission /
swap / latency / retcode noise) has to converge somewhere before a strategy
sees the fill price it booked. That somewhere is :func:`simulate_fill`. It
ingests one ``(MarketState, FillRequest)`` pair and emits a frozen
:class:`FillResult` carrying every field the live recorder
(``scripts/record_fills.py``) writes to parquet — same names, same types.

This schema lock matters because Gate 2B (sim-vs-live fill comparison) loads
the user's FTMO-demo capture parquet at ``data/fills_capture_001.parquet`` and
runs every recorded row back through :func:`simulate_fill` to compute a
per-row residual. If the schemas drift even by one field, the gate cannot
compare apples to apples; if the **types** drift (e.g. ``int`` vs ``float``,
naive vs tz-aware datetime), polars-side ingestion silently coerces and the
residuals lie. The single identity test
``test_fill_result_schema_matches_fill_record`` is what locks this contract.

Public API
----------
* :class:`FillRequest` — one order submission.
* :class:`FillResult` — schema-locked output (identical to
  ``scripts/record_fills.FillRecord``).
* :func:`simulate_fill` — the only public entry point.

Determinism contract
--------------------
:func:`simulate_fill` is deterministic given
``(request, market_state, execution_latency_ms, rng-with-same-seed)``. No
hidden RNG, no ``datetime.now()``, no environment lookups. The rng is the
**only** source of stochasticity, and it is consumed in a strictly-ordered
sequence (one ``uniform`` for slippage noise, then one ``random`` for the
limit-reject Bernoulli draw) so replays line up byte for byte.

Configurable execution latency
------------------------------
``execution_latency_ms`` (default 20ms, aligned with the 2026-05-18 Gate-2B
capture median ~19ms on FTMO MT5 demo; was 150ms matching the Run-2 spike
RTT pre-calibration) is added to ``request.request_time_utc`` to produce
``broker_fill_time_utc``.
Gate 2B can pass a per-symbol or per-run override after subtracting the
bridge's measured RTT from the recorded broker latency, so the sim and live
results are comparable on the same time-axis.

Order-type routing
------------------
* **market**: full spread + full slippage. Retcode 10009 (TRADE_RETCODE_DONE)
  unless the market is closed at ``request_time_utc`` for the symbol —
  then retcode 10018 (TRADE_RETCODE_MARKET_CLOSED), ``fill_price=0.0``, and
  ``slippage_observed_pips=NaN``.
* **limit**: zero slippage by construction (limit fills at the limit price
  or not at all). The slippage module exposes a calibrated
  ``reject_probability``; the engine draws a ``uniform`` against it via the
  supplied rng. On reject → retcode 10031 (TRADE_RETCODE_REJECT),
  ``fill_price=0.0``, ``slippage_observed_pips=NaN``. On accept →
  retcode 10009, ``fill_price == requested_price``.
* **stop**: the **caller** is responsible for deciding whether the stop has
  triggered — :class:`MarketState` carries no OHLC bar, so the engine cannot
  infer trigger from market data alone. Once the caller passes a stop
  request to :func:`simulate_fill` it is treated functionally as a market
  order at ``requested_price``: spread + full slippage, retcode 10009 (or
  10018 if the market is closed). This trigger-responsibility split is
  documented in the Phase-0 plan and explicitly tested in
  ``test_stop_order_treated_as_market``.

Per-request semantics (Wave 6c adversarial review)
--------------------------------------------------
This module is a **single-order, per-request model**: one :class:`FillRequest`
plus one :class:`MarketState` snapshot produces exactly one :class:`FillResult`.
The engine intentionally does NOT model:

* **Partial fills.** A 1-lot request books entirely at the spread/slippage
  evaluated at ``request_time_utc``; there is no split across a price move.
  The Wave 6c adversarial reviewer asked which side of a mid-request spread
  spike applies — the answer is "whichever side ``request_time_utc`` lands on".
* **SL/TP race resolution on the same bar.** If a strategy has both an SL
  and a TP that wick through inside one bar, the caller (strategy /
  backtester) decides which fires; the engine fills whichever it is told to.
* **Multi-order atomicity.** The caller serializes submissions. Two requests
  with the same ``request_time_utc`` are processed independently and do not
  share liquidity; Phase-1 strategies that need atomic multi-order semantics
  must implement them above this layer.
* **Whipsaw / phantom stop detection.** The engine cannot distinguish a
  real stop trigger from a single-tick spike that immediately reverses,
  because :class:`MarketState` carries no OHLC/tick stream. Whipsaw filtering
  is a Phase-1 strategy concern; the engine accepts whatever the caller
  asserts via the ``stop`` request.
* **Gap-fill price differentiation.** For a stop crossing a market-closed
  boundary (e.g. Friday 22:00 UTC → Sunday 22:00 UTC reopen), the caller
  must pass the post-reopen quote as ``requested_price`` to keep Gate 2B's
  residual-fill analysis accurate. The engine fills at ``requested_price +
  slippage``; it does not look up the gap-open from a bar stream.

This per-request framing is what makes the engine deterministic and what
makes the schema-locked :class:`FillResult` sufficient (no extra fields
needed for partial-fill ledgers, race-resolution metadata, or order-book
state).

Sign convention (mirrors ``scripts/record_fills.parse_fill_into_record``)
------------------------------------------------------------------------
``slippage_observed_pips`` is **adverse-positive**:

* Buy filled higher than requested → ``slippage = (fill - requested) / pip``,
  positive.
* Sell filled lower than requested → ``slippage = (requested - fill) / pip``,
  positive.

The recording script enforces the same convention and the schema-lock test
keeps the two definitions field-identical. If the recording convention ever
changes, both sides must move in lockstep — there is no per-side conversion
inside the comparison routine in Gate 2B.

Pip-size convention (mirrors ``scripts/record_fills.parse_fill_into_record``)
-----------------------------------------------------------------------------
The pip size is derived from the broker-reported quote digits using
``pip = 10 ** -(digits - 1)``:

* FX 5-digit (EURUSD, GBPUSD)   → digits=5 → pip = 0.0001.
* FX 3-digit JPY (USDJPY)       → digits=3 → pip = 0.01.
* XAUUSD (3 digits at $3500/oz) → digits=3 → pip = 0.01.
* GER40 / US100 index CFDs      → digits=1 → pip = 1.0.

This module bakes in the per-symbol digits via :data:`_SYMBOL_DIGITS`. The
identical formula lives in ``scripts/record_fills.parse_fill_into_record``
where ``symbol_digits`` is read off the live broker (``mt5.symbol_info``).
The fill engine uses the static map because the simulator never has a live
broker session attached — but the **formula** is the same, so the converted
pip values match the recording for any symbol the recorder also covers.

Constraints
-----------
* No network, no MT5 import, no broker host strings (W1 drift rule).
* All datetimes are tz-aware UTC; naive datetimes raise ``ValueError`` at
  the entry-point boundary.
* MT5 retcode constants are imported as plain ``int`` literals, not from
  the MetaTrader5 package (which has no macOS/Linux wheel and is not a
  dependency of this module).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import is_market_open
from propfarm.sim.market import MarketState
from propfarm.sim.slippage import SlippageRequest
from propfarm.sim.slippage import evaluate as evaluate_slippage
from propfarm.sim.spread import SpreadRequest
from propfarm.sim.spread import evaluate as evaluate_spread

# --------------------------------------------------------------------------- #
# MT5 retcode constants
# --------------------------------------------------------------------------- #
# Imported as integer literals (NOT from MetaTrader5) so this module remains
# importable on macOS/Linux for unit testing. Values cross-checked against the
# MQL5 documentation and ``scripts/record_fills.py`` (which is the canonical
# usage site on the live recording path).

#: ``TRADE_RETCODE_DONE`` — order successfully filled.
RETCODE_DONE: Final[int] = 10009

#: ``TRADE_RETCODE_MARKET_CLOSED`` — market closed at request time.
RETCODE_MARKET_CLOSED: Final[int] = 10018

#: ``TRADE_RETCODE_NO_MONEY`` — insufficient margin. Not produced by this
#: engine today (no margin model) but defined here so callers can reference
#: it without importing MetaTrader5.
RETCODE_NO_MONEY: Final[int] = 10019

#: ``TRADE_RETCODE_CLIENT_DISABLES_AT`` — autotrading disabled on the
#: client side. Not produced by this engine.
RETCODE_AUTOTRADING_DISABLED: Final[int] = 10027

#: ``TRADE_RETCODE_INVALID_FILL`` — broker rejected the filling mode.
#: Not produced by this engine today.
RETCODE_INVALID_FILL: Final[int] = 10030

#: ``TRADE_RETCODE_REJECT`` — rejected for any other reason. The fill
#: engine emits this for limit orders that lose the Bernoulli draw against
#: the calibrated ``reject_probability``.
RETCODE_REJECT: Final[int] = 10031


# --------------------------------------------------------------------------- #
# Default execution latency
# --------------------------------------------------------------------------- #
#: Default round-trip execution latency, ms.
#:
#: Gate-2B round 1 (2026-05-18) lowered this from **150.0** to **20.0** to
#: align with the live FTMO MT5 demo broker latency observed on the
#: 2026-05-18 capture (median 19 ms over 199 retcode-matched rows). The
#: old value tracked the bridge's round-trip-time spike but treated that
#: spike as the steady-state default, which produced a ~-46 ms residual
#: against live (sim too slow). 20 ms is the round-number conservative
#: choice: slightly above the live median to keep the sim a worst-case
#: estimator without overshooting into the bridge-spike regime.
#:
#: Gate 2B's harness still derives an empirical override from the median
#: of the captured ``broker_latency_ms`` column (see ``run_gate_2b``);
#: this constant is the fallback used by every non-harness caller and by
#: the harness when no successful rows are present.
DEFAULT_EXECUTION_LATENCY_MS: Final[float] = 20.0


# --------------------------------------------------------------------------- #
# Per-symbol quote-digit map (mirrors the broker-reported ``symbol_info.digits``)
# --------------------------------------------------------------------------- #
# Same convention as ``scripts/record_fills.parse_fill_into_record`` which
# reads ``digits`` directly off the MT5 broker. The pip-size formula is the
# same: ``pip = 10 ** -(digits - 1)``.
#
# FX 5-digit (EURUSD/GBPUSD)        -> digits=5 -> pip=0.0001
# FX 3-digit JPY (USDJPY)           -> digits=3 -> pip=0.01
# XAU 3-digit ($3500/oz, 1 cent)    -> digits=3 -> pip=0.01
# Index CFDs (GER40/US100), 1 dp    -> digits=1 -> pip=1.0
_SYMBOL_DIGITS: Final[dict[str, int]] = {
    "EURUSD": 5,
    "GBPUSD": 5,
    "USDJPY": 3,
    "XAUUSD": 3,
    "GER40": 1,
    "US100": 1,
}


def _pip_size(symbol: str) -> float:
    """Return the pip size for ``symbol`` using the recording-script formula.

    Formula: ``pip = 10 ** -(digits - 1)`` where ``digits`` is the broker's
    quote precision. Identical to ``scripts/record_fills.parse_fill_into_record``.

    Raises
    ------
    ValueError
        If ``symbol`` is not in :data:`_SYMBOL_DIGITS`.
    """
    try:
        digits = _SYMBOL_DIGITS[symbol]
    except KeyError as exc:
        raise ValueError(
            f"unknown symbol {symbol!r}; no pip-size mapping. "
            f"Known symbols: {sorted(_SYMBOL_DIGITS.keys())}"
        ) from exc
    return 10.0 ** -(digits - 1)


def _require_utc(ts: datetime, *, arg_name: str) -> None:
    """Reject naive datetimes."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


# --------------------------------------------------------------------------- #
# Public Pydantic models
# --------------------------------------------------------------------------- #
class FillRequest(BaseModel):
    """One order submission, ready for :func:`simulate_fill`.

    Attributes
    ----------
    run_id : str
        Echoed verbatim into :class:`FillResult.run_id` for run-level
        attribution. Live recordings use a UUID hex; backtests typically
        use the strategy + iteration identifier.
    symbol : str
        Trading symbol. Must be a key in :data:`_SYMBOL_DIGITS`.
    order_type : Literal["market", "limit", "stop"]
        See module docstring for the per-type behavior. Stop orders rely on
        the caller having decided the trigger fired — :class:`MarketState`
        does not carry an OHLC bar, so the engine cannot infer trigger.
    side : Literal["buy", "sell"]
        Trade direction. Drives the sign of ``slippage_observed_pips`` and
        the slippage-vs-spread adverse direction.
    volume_lots : float
        Order size in lots. Must be non-negative.
    requested_price : float
        Reference price for the order. Convention by order type:

        * ``market``: 0.0 means "use the prevailing fill price as the
          requested price for slippage reporting" (matching the recording
          script's row when MT5 returns its own filled price as the
          baseline). Non-zero is also accepted — the engine treats it as
          the trader's intent.
        * ``limit``: the limit price the order rests at.
        * ``stop``: the trigger price; once the caller decides the trigger
          fired, this is the reference for slippage.
    request_time_utc : datetime
        Tz-aware UTC instant the order was submitted. ``broker_fill_time_utc``
        on the result equals this plus ``execution_latency_ms``.
    comment : str
        Free-text annotation. Echoed verbatim. Defaults to empty string.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    symbol: str
    order_type: Literal["market", "limit", "stop"]
    side: Literal["buy", "sell"]
    volume_lots: float
    requested_price: float
    request_time_utc: datetime
    comment: str = ""


class FillResult(BaseModel):
    """Schema-locked output of :func:`simulate_fill`.

    Field-set, field-names, and field-types are **identical** to
    ``scripts.record_fills.FillRecord`` — see
    ``test_fill_result_schema_matches_fill_record``. Any drift breaks Gate 2B.

    Attributes
    ----------
    run_id : str
        Echoes :class:`FillRequest.run_id`.
    request_time_utc : datetime
        Echoes :class:`FillRequest.request_time_utc` (tz-aware UTC).
    broker_fill_time_utc : datetime
        ``request_time_utc + timedelta(ms=execution_latency_ms)``. Always
        tz-aware UTC.
    symbol : str
        Echoes :class:`FillRequest.symbol`.
    order_type : Literal["market", "limit", "stop"]
        Echoes :class:`FillRequest.order_type`.
    side : Literal["buy", "sell"]
        Echoes :class:`FillRequest.side`.
    volume_lots : float
        Echoes :class:`FillRequest.volume_lots`.
    requested_price : float
        Echoes :class:`FillRequest.requested_price`.
    fill_price : float
        The simulated fill price. ``0.0`` on rejection or market-closed.
        For limit orders that fill, equals ``requested_price`` exactly
        (zero slippage by construction).
    spread_at_request_pips : float
        Modelled spread at ``request_time_utc`` converted from bps to pips
        using the symbol's pip size and the prevailing requested price.
        ``NaN`` on market-closed.
    slippage_observed_pips : float
        Adverse-positive slippage in pips. Zero for limit orders that fill;
        ``NaN`` on rejection or market-closed.
    broker_latency_ms : float
        ``execution_latency_ms``, echoed for downstream attribution.
    retcode : int
        MT5-compatible retcode. 10009 (DONE), 10018 (MARKET_CLOSED), or
        10031 (REJECT). Future versions may emit 10019/10027/10030 — the
        constants are defined in this module for forward compatibility.
    comment : str
        Echoes :class:`FillRequest.comment`.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    request_time_utc: datetime
    broker_fill_time_utc: datetime
    symbol: str
    order_type: Literal["market", "limit", "stop"]
    side: Literal["buy", "sell"]
    volume_lots: float
    requested_price: float
    fill_price: float
    spread_at_request_pips: float
    slippage_observed_pips: float
    broker_latency_ms: float
    retcode: int
    comment: str


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _bps_to_pips(spread_bps: float, reference_price: float, pip: float) -> float:
    """Convert a spread expressed in basis points (bps) to pips.

    A bps is 0.01% of the reference price; one pip in price-units is ``pip``.
    Therefore one bps in pips = ``reference_price * 1e-4 / pip``.

    Parameters
    ----------
    spread_bps : float
        Spread in basis points. May be NaN — propagated.
    reference_price : float
        Reference price the bps applies to (typically the mid or the
        requested price). Must be positive when ``spread_bps`` is finite.
    pip : float
        Pip size in price-units (e.g. 0.0001 for FX 5-digit).
    """
    if math.isnan(spread_bps):
        return math.nan
    return spread_bps * reference_price * 1e-4 / pip


def _adverse_fill_price(
    requested: float, side: Literal["buy", "sell"], slip_price_units: float
) -> float:
    """Return the adverse fill price.

    Buy → fill = requested + slip (adverse upward).
    Sell → fill = requested - slip (adverse downward).
    ``slip_price_units`` is always non-negative (the slippage model never
    emits negative slip).
    """
    if side == "buy":
        return requested + slip_price_units
    return requested - slip_price_units


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def simulate_fill(
    request: FillRequest,
    market_state: MarketState,
    *,
    execution_latency_ms: float = DEFAULT_EXECUTION_LATENCY_MS,
    rng: np.random.Generator | None = None,
) -> FillResult:
    """Simulate one fill. The single public entry point of the fill engine.

    Determinism
    -----------
    Same ``(request, market_state, execution_latency_ms, rng-state)`` →
    bit-identical :class:`FillResult`. With ``rng=None`` the slippage noise
    and the limit-reject draw are both fixed (zero noise and the calibrated
    probability is treated as deterministic — the limit fills iff the
    probability is strictly less than 1.0; see "Limit reject without rng"
    below).

    Limit reject without rng
    ------------------------
    A limit order with ``rng=None`` always **fills** if
    ``reject_probability < 1.0``. This is the deterministic interpretation:
    "use the most-likely outcome". Callers wanting stochastic reject must
    pass an ``np.random.Generator``. The choice is consistent with the
    slippage module's ``rng=None`` deterministic-zero noise convention.

    Parameters
    ----------
    request : FillRequest
        The order submission.
    market_state : MarketState
        Market context at ``request.request_time_utc``. ``market_state.ts_utc``
        and ``market_state.symbol`` should match ``request.request_time_utc``
        and ``request.symbol``; the engine does not enforce equality (the
        caller may legitimately probe what-if scenarios) but downstream
        comparison logic in Gate 2B compares the recorded request time
        against the simulated request time, so consumers in normal use
        keep them in sync.
    execution_latency_ms : float
        Wall-clock latency, in ms, added to ``request_time_utc`` to produce
        ``broker_fill_time_utc``. Default 20ms (2026-05-18 Gate-2B capture
        median ~19ms; was 150ms matching the Run-2 spike RTT before
        round-1 calibration).
    rng : numpy.random.Generator, optional
        Source of randomness for slippage noise and the limit-reject draw.
        If ``None`` (default), the result is fully deterministic.

    Returns
    -------
    FillResult
        Frozen, schema-locked to ``scripts.record_fills.FillRecord``.

    Raises
    ------
    ValueError
        * ``request.request_time_utc`` is naive.
        * ``market_state.ts_utc`` is naive.
        * ``request.symbol`` is unknown (no pip-size mapping).
        * ``request.volume_lots`` is negative.
        * ``execution_latency_ms`` is negative.
    """
    _require_utc(request.request_time_utc, arg_name="request.request_time_utc")
    _require_utc(market_state.ts_utc, arg_name="market_state.ts_utc")
    if request.volume_lots < 0.0:
        raise ValueError(f"volume_lots must be non-negative, got {request.volume_lots!r}")
    if execution_latency_ms < 0.0:
        raise ValueError(f"execution_latency_ms must be non-negative, got {execution_latency_ms!r}")

    pip = _pip_size(request.symbol)
    broker_fill_time_utc = request.request_time_utc + timedelta(milliseconds=execution_latency_ms)

    # ---------------------------------------------------------------------- #
    # Closed-market short-circuit. Spread / slippage are not consulted; we
    # emit MARKET_CLOSED with NaN fields exactly as the recording script
    # does for retcodes != success. This applies to every order type:
    # if the market is shut, no order of any type can fill.
    # ---------------------------------------------------------------------- #
    if not is_market_open(request.symbol, request.request_time_utc):
        return FillResult(
            run_id=request.run_id,
            request_time_utc=request.request_time_utc,
            broker_fill_time_utc=broker_fill_time_utc,
            symbol=request.symbol,
            order_type=request.order_type,
            side=request.side,
            volume_lots=request.volume_lots,
            requested_price=request.requested_price,
            fill_price=0.0,
            spread_at_request_pips=math.nan,
            slippage_observed_pips=math.nan,
            broker_latency_ms=execution_latency_ms,
            retcode=RETCODE_MARKET_CLOSED,
            comment=request.comment,
        )

    # ---------------------------------------------------------------------- #
    # Spread: bps -> pips conversion uses the requested price as the
    # reference (matches the recording script's "spread at request time"
    # framing). For a market order with requested_price=0.0 we still need
    # a positive reference — fall back to 1.0 which produces a numerically
    # tiny pip-converted spread, but the spread_bps itself is what Gate 2B
    # compares against; the pip conversion is informational on the row.
    # ---------------------------------------------------------------------- #
    spread_result = evaluate_spread(market_state, SpreadRequest())
    reference_price = request.requested_price if request.requested_price > 0.0 else 1.0
    spread_at_request_pips = _bps_to_pips(spread_result.spread_bps, reference_price, pip)

    # ---------------------------------------------------------------------- #
    # Order-type dispatch.
    # ---------------------------------------------------------------------- #
    if request.order_type == "limit":
        # Slippage call still happens — for diagnostic continuity and so the
        # rng consumption pattern (1 uniform per evaluate call) is identical
        # across order types. The slippage module returns
        # ``slippage_pips=0.0`` for limit by construction.
        slippage_result = evaluate_slippage(
            market_state,
            SlippageRequest(side=request.side, order_type="limit", size_lots=request.volume_lots),
            rng=rng,
        )
        # Reject draw: 1 rng.random() draw if rng provided; deterministic
        # accept otherwise. Order of rng consumption: slippage noise first,
        # then this reject draw.
        if rng is None:
            rejected = slippage_result.reject_probability >= 1.0
        else:
            rejected = bool(rng.random() < slippage_result.reject_probability)

        if rejected:
            return FillResult(
                run_id=request.run_id,
                request_time_utc=request.request_time_utc,
                broker_fill_time_utc=broker_fill_time_utc,
                symbol=request.symbol,
                order_type=request.order_type,
                side=request.side,
                volume_lots=request.volume_lots,
                requested_price=request.requested_price,
                fill_price=0.0,
                spread_at_request_pips=spread_at_request_pips,
                slippage_observed_pips=math.nan,
                broker_latency_ms=execution_latency_ms,
                retcode=RETCODE_REJECT,
                comment=request.comment,
            )
        # Accepted limit: fills at the requested price, zero slippage.
        return FillResult(
            run_id=request.run_id,
            request_time_utc=request.request_time_utc,
            broker_fill_time_utc=broker_fill_time_utc,
            symbol=request.symbol,
            order_type=request.order_type,
            side=request.side,
            volume_lots=request.volume_lots,
            requested_price=request.requested_price,
            fill_price=request.requested_price,
            spread_at_request_pips=spread_at_request_pips,
            slippage_observed_pips=0.0,
            broker_latency_ms=execution_latency_ms,
            retcode=RETCODE_DONE,
            comment=request.comment,
        )

    # market or stop: same slip path. (Stop trigger is the caller's
    # responsibility — see module docstring.)
    slippage_result = evaluate_slippage(
        market_state,
        SlippageRequest(
            side=request.side,
            order_type=request.order_type,
            size_lots=request.volume_lots,
        ),
        rng=rng,
    )
    slip_pips = slippage_result.slippage_pips
    slip_price_units = slip_pips * pip
    fill_price = _adverse_fill_price(request.requested_price, request.side, slip_price_units)

    return FillResult(
        run_id=request.run_id,
        request_time_utc=request.request_time_utc,
        broker_fill_time_utc=broker_fill_time_utc,
        symbol=request.symbol,
        order_type=request.order_type,
        side=request.side,
        volume_lots=request.volume_lots,
        requested_price=request.requested_price,
        fill_price=fill_price,
        spread_at_request_pips=spread_at_request_pips,
        slippage_observed_pips=slip_pips,
        broker_latency_ms=execution_latency_ms,
        retcode=RETCODE_DONE,
        comment=request.comment,
    )


__all__ = [
    "DEFAULT_EXECUTION_LATENCY_MS",
    "RETCODE_AUTOTRADING_DISABLED",
    "RETCODE_DONE",
    "RETCODE_INVALID_FILL",
    "RETCODE_MARKET_CLOSED",
    "RETCODE_NO_MONEY",
    "RETCODE_REJECT",
    "FillRequest",
    "FillResult",
    "simulate_fill",
]
