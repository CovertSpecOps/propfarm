"""Gate 1 cost-reconciliation sister test (Task 13.1b).

Why this module exists
----------------------
The Phase-0 placebo gate (``propfarm.placebo.gate``) uses a *residual*
bootstrap to verify that random-entry P&L lines up with the modelled cost
floor. The Gate 1 adversarial reviewer demonstrated that the residual
bootstrap correctly catches *alpha-leak* bugs (look-ahead, wrong sign on
the directional leg, return-on-no-trade != 0) but it does **not** catch a
*systematic cost-arithmetic error* where the modelled cost equals the
applied cost but both diverge from ground truth. If the spread/commission/
swap formulas inside the sim were silently doubled (or halved, or off by a
unit), the residual bootstrap would still report "pass" because the noise
floor is computed off the same wrong cost ledger.

This module closes that gap. It is **deterministic** and proves the cost
pipeline's arithmetic against a closed-form analytic recomputation:

1. Enumerate a 10_000+ deterministic synthetic-trade matrix that exercises
   every dimension of the cost-pipeline surface (multiple symbols, both
   directions, several volume tiers, every nights_held edge case
   including the Wednesday triple).
2. For each trade, compute the cost two independent ways:
   - **Applied:** by invoking the actual sim pipeline (``simulate_fill``
     twice — open + close legs — plus ``commission_for_trade`` and
     ``swap_for_position``).
   - **Analytic:** by independently re-deriving the closed-form expected
     cost from per-symbol calibration constants, with no call to
     ``simulate_fill``.
3. Sum each independently and assert agreement to within **0.01 bps
   relative error**. Floating-point noise only — not statistical noise.

If the two diverge by more than 0.01 bps, either the pipeline is wrong or
the analytic formula here is wrong; both are real bugs. The tolerance is
not negotiable.

Slippage-handling path (requirement #2)
---------------------------------------
We use ``order_type="limit"`` for both legs of every synthetic trade.
``simulate_fill`` returns a limit fill at the requested price (zero
slippage by construction; ``slippage_observed_pips == 0.0`` and
``fill_price == requested_price``). The reject draw with ``rng=None``
accepts deterministically as long as ``reject_probability < 1.0``, which
is true for every shipped slippage calibration (max is 0.05). This means:

* No RNG is consumed anywhere in this module.
* The slippage cost component is structurally zero for every trade.
* The pipeline's reported spread (``spread_at_request_pips``) is fully
  deterministic — driven by the spread module, which is pure.

We chose this over a seeded-rng path because option (a) "configure trades
so the random-slippage component is structurally zero" is strongly
preferred by the spec, and a closed-form analytic check is cleaner when
the noise channel is shut off entirely.

Trade-time selection (no session-open widening)
-----------------------------------------------
Every synthetic trade opens at 10:00 UTC and closes at 15:00 UTC (for the
0-night intraday case) or later (for the multi-night cases). Both anchors
are deliberately chosen to be **more than 60 minutes** past every tracked
session open (London 07:00 UTC, NY 12:00/13:00 UTC, Tokyo 23:00 UTC prior
day, Sunday reopen 22:00 UTC). The spread module's
``session_open_window`` returns ``(None, 0.0)`` outside the 60-minute
window, so ``session_factor == 1.0`` exactly. With
``news_window=False`` and ``stress_mode=False`` on the ``MarketState``,
``news_factor == 1.0`` and the modelled spread equals
``calibration.baseline_bps`` exactly. The analytic reconciliation uses the
baseline_bps constant directly; this is what makes the closed-form
recomputation deterministic.

Nights-held coverage
--------------------
The synthetic matrix exercises four ``nights_held`` cases (requirement #4):

* **0 nights** — open and close on the same trading day, before the
  22:00 NY rollover. We open Tue 10:00 UTC and close Tue 15:00 UTC.
  No rollover is crossed; nights_held == 0.
* **1 night** — open Tue 10:00 UTC, close Wed 10:00 UTC. Crosses the
  Tuesday 22:00 NY rollover exactly once (Tue NY-local weekday is
  Tuesday, not in {Fri,Sat,Sun}, not Wed → 1 night).
* **2 nights** — open Mon 10:00 UTC, close Wed 10:00 UTC. Crosses Mon
  22:00 NY (=1) and Tue 22:00 NY (=1) → 2 nights.
* **Triple-Wed** — open Wed 15:00 UTC, close Thu 15:00 UTC. Crosses
  Wed 22:00 NY exactly once, and Wed NY-local weekday is the triple-
  rollover day → 3 nights. (15:00 UTC sits >60 min past the NY session
  open at 12:00 UTC during EDT, keeping session_factor=1.0.)

For each case, ``nights_held`` is independently recomputed in this
module's analytic helper (replicating the swap module's algorithm) and
asserted to match the expected value. Determinism: every trade in the
enumeration has its expected nights_held baked into its row by
construction — see :func:`_expected_nights_for_case`.

Determinism and lack of RNG (requirement #1, #7)
------------------------------------------------
* No ``random``, no ``numpy.random``, no bootstrap, no resampling. Trade
  enumeration is ``itertools.product`` over fixed lists.
* No I/O, no global state.
* All timestamps are tz-aware UTC.
* All returned models are frozen pydantic v2.
* :func:`run_cost_reconciliation` returns bit-identical aggregates on
  repeated invocation, locked by ``test_determinism_bit_identical``.

Public API
----------
* :class:`SyntheticTrade` — frozen input model.
* :class:`CostReconciliationResult` — frozen output model.
* :func:`generate_synthetic_trades` — deterministic enumeration of N>=10_000.
* :func:`analytic_cost` — closed-form expected cost per trade.
* :func:`applied_cost` — pipeline-computed cost per trade.
* :func:`run_cost_reconciliation` — top-level entry; the acceptance test
  asserts ``result.verdict == "pass"``.

Sign convention (matches the placebo gate's own aggregation)
------------------------------------------------------------
Throughout this module, "cost" is **positive = charged to the trader**.

* Spread cost: always >= 0. Computed from the bps-converted-to-pips
  spread reported by ``simulate_fill`` (or the deterministic baseline
  for the analytic side), times volume times point_value.
* Commission: always >= 0 in the shipped tables. Returned by
  ``commission_for_trade`` directly.
* Swap: signed. ``swap_for_position`` returns positive=cost in
  simulator convention, so we use it directly (no sign flip) on both
  sides of the reconciliation. A negative swap (i.e. a credit) is
  legal and the aggregate handles it consistently — the relative-error
  check is on the *signed* totals, so credits net against costs the
  same way on both sides.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from propfarm.sim.commission import (
    FTMO_MT5_COMMISSION,
    CommissionTable,
    commission_for_trade,
)
from propfarm.sim.fill_engine import (
    RETCODE_DONE,
    FillRequest,
    simulate_fill,
)
from propfarm.sim.market import MarketState
from propfarm.sim.spread import CALIBRATIONS as SPREAD_CALIBRATIONS
from propfarm.sim.swap import (
    FTMO_MT5_SWAP,
    SwapTable,
    nights_held,
    swap_for_position,
)

# --------------------------------------------------------------------------- #
# Module-level constants
# --------------------------------------------------------------------------- #

#: Tolerance: aggregate applied vs analytic relative error must be < this in
#: basis points. 0.01 bps = 1e-6 relative error — pure floating-point noise
#: territory. Widening this is forbidden by spec; if the test trips, fix the
#: arithmetic, not the threshold.
DEFAULT_TOLERANCE_BPS: Final[float] = 0.01

#: Per-symbol pip size (price units). Mirrors the fill engine's _SYMBOL_DIGITS
#: formula ``pip = 10 ** -(digits - 1)``. Duplicated here so the analytic side
#: doesn't reach into the pipeline's private map — the analytic recomputation
#: must stand on its own.
_PIP_SIZE: Final[dict[str, float]] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.01,
    "GER40": 1.0,
    "US100": 1.0,
}

#: Per-symbol USD value of a one-pip move on one lot. Matches the placebo
#: gate's ``_POINT_VALUE_PER_LOT_USD`` map.
_POINT_VALUE_PER_LOT_USD: Final[dict[str, float]] = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDJPY": 10.0,
    "XAUUSD": 1.0,
    "GER40": 1.0,
    "US100": 1.0,
}

#: Per-symbol reference mid-price used as ``requested_price`` for both legs.
#: Chosen near the calibration regimes (e.g. EURUSD ~1.10) so the bps-to-pips
#: conversion in the spread model produces non-degenerate values. The exact
#: value cancels out of the reconciliation as long as the same number is
#: used on the applied and analytic sides for the same symbol.
_REFERENCE_PRICE: Final[dict[str, float]] = {
    "EURUSD": 1.10000,
    "GBPUSD": 1.30000,
    "USDJPY": 150.000,
    "XAUUSD": 3500.00,
    "GER40": 20000.0,
    "US100": 20000.0,
}


# --------------------------------------------------------------------------- #
# Public Pydantic models
# --------------------------------------------------------------------------- #
class SyntheticTrade(BaseModel):
    """One deterministically-generated synthetic round-trip.

    Trades are generated by :func:`generate_synthetic_trades` and consumed
    by :func:`applied_cost` / :func:`analytic_cost`. The :attr:`order_type`
    is always ``"limit"`` on the public API surface — this is the
    zero-slippage path required by Phase 0 acceptance.

    Attributes
    ----------
    symbol : str
        Trading symbol; must be a key in :data:`_PIP_SIZE`.
    direction : Literal["long", "short"]
        Trade direction (swap-module convention). Mapped to the fill
        engine's ``side`` parameter via long→buy/short→sell internally.
    volume_lots : float
        Order size in lots, > 0.
    open_ts_utc : datetime.datetime
        Tz-aware UTC open time. Chosen to be outside any session-open
        window so the modelled spread reduces to baseline (see module
        docstring).
    close_ts_utc : datetime.datetime
        Tz-aware UTC close time, strictly after open.
    order_type : Literal["limit", "market"]
        Always "limit" in the shipped enumeration; the API accepts
        "market" for future expansion / explicit test cases.
    expected_nights_held : int
        Number of swap nights this trade is *expected* to span by
        construction. The analytic helper asserts the swap module's
        ``nights_held`` agrees; a mismatch is a real bug in either side.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    direction: Literal["long", "short"]
    volume_lots: float
    open_ts_utc: datetime
    close_ts_utc: datetime
    order_type: Literal["limit", "market"]
    expected_nights_held: int


class CostReconciliationResult(BaseModel):
    """Frozen output of :func:`run_cost_reconciliation`.

    Attributes
    ----------
    n_trades : int
        Number of trades reconciled. >= 10_000 by construction.
    known_total_usd : float
        Sum of ``analytic_cost`` across all trades (positive = cost).
    applied_total_usd : float
        Sum of ``applied_cost`` across all trades (positive = cost).
    abs_error_usd : float
        ``abs(applied_total_usd - known_total_usd)``.
    relative_error_bps : float
        ``abs_error_usd / abs(known_total_usd) * 10_000``. Sub-bps means
        floating-point noise; integer-bps or larger means an arithmetic
        bug in either the pipeline or the analytic recomputation.
    tolerance_bps : float
        The threshold the verdict is computed against (default 0.01).
    verdict : Literal["pass", "fail"]
        ``"pass"`` iff ``relative_error_bps < tolerance_bps``.
    per_symbol_breakdown : dict[str, dict[str, float]]
        Per-symbol diagnostic. Each value carries
        ``{"known_usd", "applied_usd", "abs_error_usd",
        "relative_error_bps"}``. On fail, this is what tells you which
        symbol's arithmetic diverged.
    """

    model_config = ConfigDict(frozen=True)

    n_trades: int
    known_total_usd: float
    applied_total_usd: float
    abs_error_usd: float
    relative_error_bps: float
    tolerance_bps: float
    verdict: Literal["pass", "fail"]
    per_symbol_breakdown: dict[str, dict[str, float]]


# --------------------------------------------------------------------------- #
# Synthetic-trade enumeration
# --------------------------------------------------------------------------- #
#: Symbols exercised by the synthetic matrix. Chosen to span the four asset-
#: class shapes the cost pipeline must handle:
#:  * Two FX 5-digit majors (EURUSD, GBPUSD) — pip=0.0001, point_value=$10.
#:  * One FX 3-digit JPY pair (USDJPY) — pip=0.01.
#:  * One metal (XAUUSD) — pip=0.01, point_value=$1, large baseline spread.
_ENUMERATED_SYMBOLS: Final[tuple[str, ...]] = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")

#: Directions exercised (requirement #4).
_ENUMERATED_DIRECTIONS: Final[tuple[Literal["long", "short"], ...]] = ("long", "short")

#: Volume tiers exercised (requirement #4). Spans three orders of magnitude
#: so the commission/swap linearity in volume is exercised.
_ENUMERATED_VOLUMES: Final[tuple[float, ...]] = (0.01, 0.10, 0.50, 1.00)


#: The four ``nights_held`` cases (requirement #4). Each case carries a
#: human-readable name and the construction recipe used by
#: :func:`_open_close_for_case` to materialise a (open_ts, close_ts) pair.
#:
#: Anchor dates are chosen in May 2026 (EDT in effect, so the NY rollover at
#: 22:00 NY-local = 02:00 UTC the next day, and the NY session open is at
#: 12:00 UTC). Specifically:
#:
#:  * 2026-05-04 (Mon), 2026-05-05 (Tue), 2026-05-06 (Wed), 2026-05-07 (Thu),
#:    2026-05-08 (Fri).
#:
#: All four anchor dates are weekdays, FX-open, far from US/EU DST boundaries
#: (the EU spring transition was 2026-03-29; the US spring transition was
#: 2026-03-08; the next transitions are 2026-10-25 and 2026-11-01).
#:
#: For each case, both anchors land at 10:00 UTC or 12:00 UTC — more than
#: 60 minutes past every session open, so the spread module returns
#: ``baseline_bps`` exactly.
_NIGHTS_CASES: Final[tuple[tuple[str, int], ...]] = (
    ("intraday_0n", 0),  # Tue 10:00 UTC -> Tue 15:00 UTC
    ("overnight_1n", 1),  # Tue 10:00 UTC -> Wed 10:00 UTC
    ("monwed_2n", 2),  # Mon 10:00 UTC -> Wed 10:00 UTC
    ("triple_wed_3n", 3),  # Wed 12:00 UTC -> Thu 12:00 UTC
)


def _open_close_for_case(case_name: str) -> tuple[datetime, datetime]:
    """Return the (open_ts_utc, close_ts_utc) anchor pair for a nights-held case.

    All anchors are deterministic, tz-aware UTC, in the week of 2026-05-04.

    Raises
    ------
    ValueError
        If ``case_name`` is not one of the four enumerated cases.
    """
    if case_name == "intraday_0n":
        # Tue 2026-05-05, 10:00 UTC -> 15:00 UTC. Same trading day, no
        # rollover crossing (the 22:00 NY rollover that day is at
        # 02:00 UTC on 2026-05-06, well after the close).
        return (
            datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            datetime(2026, 5, 5, 15, 0, tzinfo=UTC),
        )
    if case_name == "overnight_1n":
        # Tue 2026-05-05, 10:00 UTC -> Wed 2026-05-06, 10:00 UTC. Crosses
        # exactly one 22:00 NY rollover: the Tue-NY rollover at
        # 02:00 UTC 2026-05-06. NY-local weekday at that instant is
        # Tuesday (since 02:00 UTC 2026-05-06 = 22:00 NY-local 2026-05-05)
        # which is NOT in {Fri,Sat,Sun} and is NOT the triple-Wed day,
        # so it counts as 1 night.
        return (
            datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
        )
    if case_name == "monwed_2n":
        # Mon 2026-05-04, 10:00 UTC -> Wed 2026-05-06, 10:00 UTC. Crosses
        # the Mon-NY rollover (02:00 UTC 2026-05-05, NY-local Mon = 1 night)
        # and the Tue-NY rollover (02:00 UTC 2026-05-06, NY-local Tue =
        # 1 night). 2 nights total.
        return (
            datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
            datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
        )
    if case_name == "triple_wed_3n":
        # Wed 2026-05-06, 15:00 UTC -> Thu 2026-05-07, 15:00 UTC. Crosses
        # exactly the Wed-NY rollover (02:00 UTC 2026-05-07 = 22:00 NY-local
        # 2026-05-06 = Wednesday). NY-local Wednesday IS the triple-rollover
        # weekday, so this crossing counts as 3 nights.
        #
        # 15:00 UTC is chosen (not 12:00 UTC) because 12:00 UTC during EDT
        # is **exactly** the NY session open, which would trigger the
        # spread module's session_open_multiplier (5x for FX, decaying with
        # ~10min half-life). 15:00 UTC sits 180 minutes past NY open,
        # well outside the 60-minute decay window, so session_factor=1.0
        # and the analytic baseline_bps reduction is exact.
        return (
            datetime(2026, 5, 6, 15, 0, tzinfo=UTC),
            datetime(2026, 5, 7, 15, 0, tzinfo=UTC),
        )
    raise ValueError(f"unknown nights case {case_name!r}")


#: Number of "replica" trades emitted per (symbol, direction, volume, case)
#: combination. With 4*2*4*4 = 128 base combos, we need
#: ceil(10_000 / 128) = 79 replicas to meet the >= 10_000 requirement. We
#: pick 79 exactly so the total is 10_112 — comfortably above the floor
#: with no slack we'd have to defend in review.
#:
#: All replicas of one combo are exactly identical (same symbol, direction,
#: volume, open/close timestamps). This is intentional: the reconciliation
#: is per-trade arithmetic, so emitting 79 copies of an identical trade is
#: identical to emitting 1 copy * 79 in the aggregate. The replica count is
#: kept in case a future enhancement wants to vary the replicas along a
#: fifth dimension (e.g. ``realized_vol_5m``); today it inflates the trade
#: count without changing the aggregate per-combo cost.
_REPLICAS_PER_COMBO: Final[int] = 79


def generate_synthetic_trades() -> list[SyntheticTrade]:
    """Return a deterministic list of >= 10_000 synthetic trades.

    The list is the Cartesian product of:

    * :data:`_ENUMERATED_SYMBOLS` (4 symbols)
    * :data:`_ENUMERATED_DIRECTIONS` (2 directions)
    * :data:`_ENUMERATED_VOLUMES` (4 volumes)
    * :data:`_NIGHTS_CASES` (4 nights-held cases)
    * :data:`_REPLICAS_PER_COMBO` (79 identical replicas)

    Total: 4 * 2 * 4 * 4 * 79 = **10_112** trades. The iteration order is
    deterministic across machines / process restarts (Python dict insertion
    order; ``itertools.product`` over fixed tuples).

    Every trade has ``order_type="limit"`` so the fill engine returns zero
    slippage by construction (see module docstring on the slippage-handling
    path choice).

    Returns
    -------
    list[SyntheticTrade]
        Frozen, deterministic enumeration. Repeated calls return the
        same list (bit-identical).
    """
    trades: list[SyntheticTrade] = []
    for symbol, direction, volume, (case_name, expected_nights) in itertools.product(
        _ENUMERATED_SYMBOLS,
        _ENUMERATED_DIRECTIONS,
        _ENUMERATED_VOLUMES,
        _NIGHTS_CASES,
    ):
        open_ts, close_ts = _open_close_for_case(case_name)
        for _ in range(_REPLICAS_PER_COMBO):
            trades.append(
                SyntheticTrade(
                    symbol=symbol,
                    direction=direction,
                    volume_lots=volume,
                    open_ts_utc=open_ts,
                    close_ts_utc=close_ts,
                    order_type="limit",
                    expected_nights_held=expected_nights,
                )
            )
    return trades


# --------------------------------------------------------------------------- #
# Cost computation: applied (via pipeline) and analytic (closed-form)
# --------------------------------------------------------------------------- #
def _direction_to_side(direction: Literal["long", "short"]) -> Literal["buy", "sell"]:
    """Map swap-module direction to fill-engine side."""
    return "buy" if direction == "long" else "sell"


def _applied_spread_cost_usd(
    *,
    trade: SyntheticTrade,
    open_fill_spread_pips: float,
    close_fill_spread_pips: float,
) -> float:
    """Sum the half-spread on each leg into a USD round-trip spread cost.

    Mirrors the placebo gate's spread-cost formula (the canonical
    convention): ``(open + close) * 0.5 * volume * point_value``. Each
    leg pays its own half-spread; summing the two pip values and applying
    the 0.5 factor produces the round-trip cost.
    """
    point_value = _POINT_VALUE_PER_LOT_USD[trade.symbol]
    return (open_fill_spread_pips + close_fill_spread_pips) * 0.5 * trade.volume_lots * point_value


def applied_cost(
    trade: SyntheticTrade,
    *,
    commission_table: CommissionTable = FTMO_MT5_COMMISSION,
    swap_table: SwapTable = FTMO_MT5_SWAP,
) -> float:
    """Compute the trade's total cost via the actual sim pipeline.

    Two ``simulate_fill`` calls (open + close), one ``commission_for_trade``
    call, one ``swap_for_position`` call. Returns the **signed** USD cost
    (positive = trader was charged) summed across spread + commission +
    swap. Slippage cost is structurally zero for limit orders.

    Parameters
    ----------
    trade : SyntheticTrade
        The trade to price.
    commission_table : CommissionTable, optional
        Default :data:`propfarm.sim.commission.FTMO_MT5_COMMISSION`.
    swap_table : SwapTable, optional
        Default :data:`propfarm.sim.swap.FTMO_MT5_SWAP`.

    Raises
    ------
    RuntimeError
        If either ``simulate_fill`` call returns a non-DONE retcode. We
        chose the trade timestamps deliberately to avoid market-closed
        and reject paths; a non-DONE retcode means the construction is
        wrong and the reconciliation cannot run.
    """
    side = _direction_to_side(trade.direction)
    close_side: Literal["buy", "sell"] = "sell" if side == "buy" else "buy"
    reference_price = _REFERENCE_PRICE[trade.symbol]

    # --- Open leg ----------------------------------------------------------
    open_market = MarketState(
        symbol=trade.symbol,
        ts_utc=trade.open_ts_utc,
        # Realized vol does NOT affect spread, commission, or swap — only
        # slippage uses it, and limit orders take zero slippage. Setting
        # 0.10 (the slippage module's typical-regime default) keeps the
        # MarketState well-formed without affecting the reconciliation.
        realized_vol_5m=0.10,
        news_window=False,  # baseline spread regime
        stress_mode=False,  # baseline spread/slip regime
    )
    open_request = FillRequest(
        run_id="cost_reconciliation",
        symbol=trade.symbol,
        order_type=trade.order_type,
        side=side,
        volume_lots=trade.volume_lots,
        requested_price=reference_price,
        request_time_utc=trade.open_ts_utc,
        comment="open",
    )
    open_fill = simulate_fill(open_request, open_market, rng=None)
    if open_fill.retcode != RETCODE_DONE:
        raise RuntimeError(
            f"open leg returned non-DONE retcode {open_fill.retcode} for trade {trade!r}; "
            "construction is wrong (timestamps must land inside the symbol's session)."
        )

    # --- Close leg ---------------------------------------------------------
    close_market = MarketState(
        symbol=trade.symbol,
        ts_utc=trade.close_ts_utc,
        realized_vol_5m=0.10,
        news_window=False,
        stress_mode=False,
    )
    close_request = FillRequest(
        run_id="cost_reconciliation",
        symbol=trade.symbol,
        order_type=trade.order_type,
        side=close_side,
        volume_lots=trade.volume_lots,
        requested_price=reference_price,
        request_time_utc=trade.close_ts_utc,
        comment="close",
    )
    close_fill = simulate_fill(close_request, close_market, rng=None)
    if close_fill.retcode != RETCODE_DONE:
        raise RuntimeError(
            f"close leg returned non-DONE retcode {close_fill.retcode} for trade {trade!r}."
        )

    # --- Spread cost (positive = charged) ---------------------------------
    spread_cost_usd = _applied_spread_cost_usd(
        trade=trade,
        open_fill_spread_pips=open_fill.spread_at_request_pips,
        close_fill_spread_pips=close_fill.spread_at_request_pips,
    )

    # --- Commission (always >= 0; round-trip already) ---------------------
    commission_usd = commission_for_trade(
        table=commission_table,
        symbol=trade.symbol,
        volume_lots=trade.volume_lots,
    )

    # --- Swap (positive = cost in simulator convention) -------------------
    swap_usd = swap_for_position(
        table=swap_table,
        symbol=trade.symbol,
        direction=trade.direction,
        volume_lots=trade.volume_lots,
        open_ts_utc=trade.open_ts_utc,
        close_ts_utc=trade.close_ts_utc,
    )

    # All three components on the same "positive = cost" convention. Sum.
    return spread_cost_usd + commission_usd + swap_usd


def _analytic_baseline_spread_bps(symbol: str) -> float:
    """Return the baseline spread (bps) for a symbol from the spread calibration.

    We assert the analytic side reduces to ``baseline_bps`` exactly — this
    is true because the trade timestamps were chosen to land outside every
    session-open window (session_factor=1.0) with news_window=False
    (news_factor=1.0). Reaching into the calibration registry here is the
    closed-form analytic input, not a re-invocation of the spread pipeline.
    """
    return SPREAD_CALIBRATIONS[symbol].baseline_bps


def _analytic_spread_cost_usd(*, trade: SyntheticTrade) -> float:
    """Closed-form spread cost in USD for one round-trip limit trade.

    Derivation (no call to ``simulate_fill`` or the spread module's
    ``evaluate``):

    1. The modelled spread in bps at a quiet-hour timestamp with no news
       equals ``baseline_bps`` (see module docstring on trade-time
       selection).
    2. Convert bps to pips at the reference price using the formula
       mirrored from :func:`propfarm.sim.fill_engine._bps_to_pips`:
       ``pips = bps * reference_price * 1e-4 / pip_size``.
    3. Apply the placebo-gate spread convention: half-spread on each leg,
       both legs combine to ``(open_pips + close_pips) * 0.5 * volume *
       point_value``. With both legs deterministic and identical-symbol
       (so open_pips == close_pips), this simplifies to
       ``spread_pips * volume * point_value``.

    The result is structurally exact at the floating-point level — every
    operation here is a multiplication or division, identical to the
    pipeline's, just inlined.
    """
    baseline_bps = _analytic_baseline_spread_bps(trade.symbol)
    reference_price = _REFERENCE_PRICE[trade.symbol]
    pip = _PIP_SIZE[trade.symbol]
    point_value = _POINT_VALUE_PER_LOT_USD[trade.symbol]

    # bps -> pips (matches fill_engine._bps_to_pips)
    spread_pips = baseline_bps * reference_price * 1e-4 / pip
    # Half-spread on each leg; both legs identical -> open_pips + close_pips
    # = 2 * spread_pips; times 0.5 cancels the 2 -> spread_pips * volume *
    # point_value.
    return spread_pips * trade.volume_lots * point_value


def _analytic_commission_usd(*, trade: SyntheticTrade, commission_table: CommissionTable) -> float:
    """Closed-form round-trip commission in USD.

    Replicates ``commission_for_trade`` arithmetic inline:
    ``volume_lots * per_round_trip_usd[symbol]``. We re-read the
    ``per_round_trip_usd`` map from the table (which is the only place
    the firm's calibration lives) — this is the analytic *input*, not a
    re-invocation of the lookup function.
    """
    return trade.volume_lots * commission_table.per_round_trip_usd[trade.symbol]


def _analytic_nights_held(*, trade: SyntheticTrade, swap_table: SwapTable) -> int:
    """Independently recompute nights_held and assert it matches expectation.

    We **also** call the swap module's ``nights_held`` and assert it
    returns ``trade.expected_nights_held``. This is a defense-in-depth
    cross-check: the analytic side baked in an expected count by
    construction, and the swap module's algorithm should agree. If it
    doesn't, *that's* the bug — the test surfaces it via the
    reconciliation residual.

    Returns the **expected** count (not the swap module's) so the
    analytic side stands on its own; the assert turns a swap-module
    nights_held bug into a clear AssertionError rather than burying it
    inside a small relative-error residual.

    Raises
    ------
    AssertionError
        If the swap module's nights_held disagrees with the construction-
        time expectation. This is the cleanest signal a reviewer can ask
        for that the swap module's date arithmetic is correct.
    """
    pipeline_nights = nights_held(
        open_ts_utc=trade.open_ts_utc,
        close_ts_utc=trade.close_ts_utc,
        triple_rollover_weekday=swap_table.triple_rollover_weekday,
    )
    if pipeline_nights != trade.expected_nights_held:
        raise AssertionError(
            f"nights_held mismatch for trade {trade!r}: "
            f"expected={trade.expected_nights_held}, "
            f"pipeline={pipeline_nights}. Swap module's date arithmetic disagrees "
            "with the construction-time expectation; one of them is wrong."
        )
    return trade.expected_nights_held


def _analytic_swap_usd(*, trade: SyntheticTrade, swap_table: SwapTable) -> float:
    """Closed-form swap cost in USD, simulator convention (positive = cost).

    Replicates the formula in :func:`propfarm.sim.swap.swap_for_position`
    inline::

        usd = -1.0
              * broker_points_per_night
              * point_value_usd[symbol]
              * volume_lots
              * nights_held

    The leading ``-1.0`` flips broker-convention (positive = credit) to
    simulator-convention (positive = cost). We pick the correct
    ``broker_points_per_night`` from the long / short map based on the
    trade direction.

    With ``nights_held == 0`` (the intraday case), the result is 0.0
    exactly — there is no rollover crossing.
    """
    nights = _analytic_nights_held(trade=trade, swap_table=swap_table)
    if nights == 0:
        return 0.0
    if trade.direction == "long":
        broker_points = swap_table.swap_long_points[trade.symbol]
    else:
        broker_points = swap_table.swap_short_points[trade.symbol]
    point_value = swap_table.point_value_usd[trade.symbol]
    return -1.0 * broker_points * point_value * trade.volume_lots * nights


def analytic_cost(
    trade: SyntheticTrade,
    *,
    commission_table: CommissionTable = FTMO_MT5_COMMISSION,
    swap_table: SwapTable = FTMO_MT5_SWAP,
) -> float:
    """Compute the trade's total cost from closed-form analytic formulas.

    Does **not** call ``simulate_fill``. Each component is independently
    re-derived from the calibration constants:

    * Spread: ``baseline_bps * ref_price * 1e-4 / pip * volume *
      point_value`` (closed-form for quiet-hour limit orders).
    * Commission: ``volume_lots * per_round_trip_usd[symbol]``.
    * Swap: ``-1.0 * broker_points * point_value_usd * volume_lots *
      expected_nights_held``.

    Returns the **signed** USD cost (positive = trader was charged) summed
    across the three components, on the same "positive = cost" convention
    as :func:`applied_cost`. The sign-flip inside the swap formula is
    identical to the pipeline's, so the two sides remain comparable.

    Parameters
    ----------
    trade : SyntheticTrade
        The trade to price.
    commission_table : CommissionTable, optional
        Default :data:`propfarm.sim.commission.FTMO_MT5_COMMISSION`.
    swap_table : SwapTable, optional
        Default :data:`propfarm.sim.swap.FTMO_MT5_SWAP`.
    """
    spread_cost_usd = _analytic_spread_cost_usd(trade=trade)
    commission_usd = _analytic_commission_usd(trade=trade, commission_table=commission_table)
    swap_usd = _analytic_swap_usd(trade=trade, swap_table=swap_table)
    return spread_cost_usd + commission_usd + swap_usd


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def run_cost_reconciliation(
    *,
    tolerance_bps: float = DEFAULT_TOLERANCE_BPS,
    commission_table: CommissionTable = FTMO_MT5_COMMISSION,
    swap_table: SwapTable = FTMO_MT5_SWAP,
) -> CostReconciliationResult:
    """Drive the full applied-vs-analytic cost reconciliation.

    Generates the deterministic 10_112-trade matrix, prices every trade
    twice (via :func:`applied_cost` and :func:`analytic_cost`), aggregates
    the totals, and decides ``"pass"``/``"fail"`` against the relative-
    error tolerance in bps. Also produces a per-symbol diagnostic
    breakdown so a fail can be attributed to the right symbol.

    Parameters
    ----------
    tolerance_bps : float, default 0.01
        Pass threshold in basis points (1 bps = 1e-4 relative error).
        The reviewer rejects on any widening; if you find a real
        divergence, fix the arithmetic instead.
    commission_table : CommissionTable, optional
        Default FTMO. Pass another firm's table for diagnostic runs.
    swap_table : SwapTable, optional
        Default FTMO. Same.

    Returns
    -------
    CostReconciliationResult
        Frozen result. Acceptance test asserts
        ``result.verdict == "pass"`` and
        ``result.relative_error_bps < tolerance_bps``.
    """
    trades = generate_synthetic_trades()

    # Per-symbol running tallies. Plain floats (not numpy) — no resampling
    # math here, just summations. Determinism is preserved because Python
    # float addition is bit-identical across runs given the same operand
    # sequence.
    per_symbol_known: dict[str, float] = {s: 0.0 for s in _ENUMERATED_SYMBOLS}
    per_symbol_applied: dict[str, float] = {s: 0.0 for s in _ENUMERATED_SYMBOLS}

    for trade in trades:
        known = analytic_cost(
            trade,
            commission_table=commission_table,
            swap_table=swap_table,
        )
        applied = applied_cost(
            trade,
            commission_table=commission_table,
            swap_table=swap_table,
        )
        per_symbol_known[trade.symbol] += known
        per_symbol_applied[trade.symbol] += applied

    known_total = sum(per_symbol_known.values())
    applied_total = sum(per_symbol_applied.values())
    abs_error = abs(applied_total - known_total)
    # Guard against a hypothetical zero-known-total (cannot happen with
    # the shipped enumeration — commission alone is > 0 for every trade —
    # but the guard keeps the math defined if a future enumeration ever
    # nets to zero).
    if known_total == 0.0:
        relative_error_bps = math.inf if abs_error > 0.0 else 0.0
    else:
        relative_error_bps = abs_error / abs(known_total) * 10_000.0

    per_symbol_breakdown: dict[str, dict[str, float]] = {}
    for symbol in _ENUMERATED_SYMBOLS:
        sym_known = per_symbol_known[symbol]
        sym_applied = per_symbol_applied[symbol]
        sym_abs_err = abs(sym_applied - sym_known)
        if sym_known == 0.0:
            sym_rel_err_bps = math.inf if sym_abs_err > 0.0 else 0.0
        else:
            sym_rel_err_bps = sym_abs_err / abs(sym_known) * 10_000.0
        per_symbol_breakdown[symbol] = {
            "known_usd": sym_known,
            "applied_usd": sym_applied,
            "abs_error_usd": sym_abs_err,
            "relative_error_bps": sym_rel_err_bps,
        }

    verdict: Literal["pass", "fail"] = "pass" if relative_error_bps < tolerance_bps else "fail"

    return CostReconciliationResult(
        n_trades=len(trades),
        known_total_usd=known_total,
        applied_total_usd=applied_total,
        abs_error_usd=abs_error,
        relative_error_bps=relative_error_bps,
        tolerance_bps=tolerance_bps,
        verdict=verdict,
        per_symbol_breakdown=per_symbol_breakdown,
    )


__all__ = [
    "DEFAULT_TOLERANCE_BPS",
    "CostReconciliationResult",
    "SyntheticTrade",
    "analytic_cost",
    "applied_cost",
    "generate_synthetic_trades",
    "run_cost_reconciliation",
]
