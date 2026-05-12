"""Per-firm commission tables (Task 6.2).

The execution simulator subtracts commission on every fill. Commission is the
**easiest** cost component to get right — it is a deterministic per-firm,
per-symbol, per-round-trip number — and it is the component the Day-13
placebo gate most directly tests: random entries lose **exactly** the sum of
the costs we model, and the largest, most-predictable cost is commission.

If our commission numbers are wrong, the placebo gate either raises a false
alarm (numbers too high → "the strategy is losing money it shouldn't") or
worse, suppresses a real alarm (numbers too low → an actual alpha leak hides
inside the noise of a wrong commission). So we keep the numbers in a single
frozen pydantic model per (firm, account-type) pair, each one citing its own
on-disk markdown snapshot of the firm's published ToS.

The three firms we ship with are FTMO, FundedNext, and FundingPips, each
on the **MT5 commission account** variant (raw spread + per-lot commission),
not the swap-free / standard variants which have $0 commission and wider
spreads. The strategy team's funded-capital path lives on the commission
account because the planned scalping / intraday-FX strategies are
spread-sensitive, not commission-sensitive.

Source of truth
---------------
The canonical numbers live in ``docs/firm-tos-snapshots/*-commission-*.md``,
one file per firm, each one recording:

* The URL fetched (or the HTTP status code if unreachable).
* The retrieval date.
* The verbatim commission text from the firm's own pages, plus secondary
  corroboration where the primary URL is paywalled or 404'd.
* Any "as of" effective date the firm publishes.

This module mirrors those numbers into frozen ``CommissionTable`` instances.
The test suite cross-checks that the on-disk snapshot files still exist at
the paths the tables reference — drift between the snapshot files and the
literal table values trips a test on the next run.

Public API
----------
* :class:`CommissionTable` — frozen pydantic model for one (firm, account_type) pair.
* :func:`commission_for_trade` — USD cost for one round trip at ``volume_lots`` lots.
* :data:`FTMO_MT5_COMMISSION`, :data:`FUNDEDNEXT_MT5_COMMISSION`,
  :data:`FUNDINGPIPS_MT5_COMMISSION` — the three shipped tables.
* :data:`ALL_TABLES` — ``{firm_name: CommissionTable}`` lookup.

Design notes
------------
* The ``per_round_trip_usd`` mapping carries **round-trip** USD per lot, not
  per-side. Round-trip is the natural unit for backtests, which book the
  full cost on close. Per-side conversion is mechanical (÷ 2) and lives in
  the snapshot files where the firm's own per-side / per-side-formula
  numbers are recorded for auditability.
* For firms that charge a **percentage of notional** on metals (FTMO,
  FundedNext), we bake in a fixed-USD-per-lot figure using a documented
  gold-price peg ($3500/oz spot). This is correct to within ~10% across
  the snapshot's expected lifetime; the snapshot file explicitly calls
  out that re-pegging is required if gold moves >10% from the peg, and
  the cost-model engine (Task 7.2) is the right place to swap in a
  live-price-aware computation if that lands.
* No time-of-day commission tiers are modelled here. None of the three
  firms publishes peak-hour surcharges or off-hours discounts as of
  2026-05-12. If a firm later does, the right primitive is to import
  ``from propfarm.data.quality import is_market_open`` rather than
  reinventing session boundaries — but that hook is not needed today.

Constraints
-----------
* No MT5 host, no broker host, no VPS IP. Commission is broker-agnostic at
  the data-shape level — the firm name is metadata, not a runtime broker
  handle. This module imports nothing from ``propfarm.bridge``.
* All ``CommissionTable`` instances are frozen
  (``model_config = ConfigDict(frozen=True)``); the simulator cannot
  accidentally mutate a fee in flight.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class CommissionTable(BaseModel):
    """Frozen commission lookup for one ``(firm, account_type)`` pair.

    Attributes
    ----------
    firm : str
        Firm slug. One of ``"ftmo"``, ``"fundednext"``, ``"fundingpips"``.
    account_type : str
        Account-type slug (e.g. ``"mt5_commission"``). Distinguishes a firm's
        raw-spread + commission account from its standard / swap-free variant.
    snapshot_date : datetime.date
        The date the ToS snapshot file was retrieved.
    snapshot_source : str
        Repo-relative path to the markdown snapshot, e.g.
        ``"docs/firm-tos-snapshots/ftmo-commission-2026-05-12.md"``.
        The test suite asserts this file exists.
    per_round_trip_usd : dict[str, float]
        Mapping ``symbol -> USD commission for one round trip of 1 lot``.
        Keys must cover every symbol the system trades for that firm.

    Frozen-ness
    -----------
    Pydantic v2 ``ConfigDict(frozen=True)`` makes field-set raise after
    construction. The ``per_round_trip_usd`` dict itself is not deep-frozen
    (pydantic does not freeze nested mutables), but the typical mistake we
    are guarding against is ``table.firm = ...`` not ``table.per_round_trip_usd
    ["EURUSD"] = ...``; the latter would be caught by code review.
    """

    model_config = ConfigDict(frozen=True)

    firm: str
    account_type: str
    snapshot_date: date
    snapshot_source: str
    per_round_trip_usd: dict[str, float]


# --------------------------------------------------------------------------- #
# Lookup function
# --------------------------------------------------------------------------- #
def commission_for_trade(
    *,
    table: CommissionTable,
    symbol: str,
    volume_lots: float,
) -> float:
    """Return the USD commission for one round trip of ``volume_lots`` lots of ``symbol``.

    Commission is linear in volume: ``volume_lots * per_round_trip_usd[symbol]``.

    Parameters
    ----------
    table : CommissionTable
        The firm's table. Must contain ``symbol`` as a key in
        ``per_round_trip_usd`` or this function raises.
    symbol : str
        Instrument symbol (e.g. ``"EURUSD"``). Must be a key in ``table``.
    volume_lots : float
        Volume in lots. Must be non-negative; zero returns zero.

    Returns
    -------
    float
        Round-trip USD commission. Zero for zero volume.

    Raises
    ------
    ValueError
        If ``symbol`` is not a key in ``table.per_round_trip_usd``, or if
        ``volume_lots`` is negative. Negative volume is a programmer error
        (the simulator represents direction by side flag, not by negative
        volume); returning 0 or abs-value would let an upstream bug
        propagate silently into the placebo gate.
    """
    if volume_lots < 0.0:
        raise ValueError(
            f"volume_lots must be non-negative, got {volume_lots!r} for symbol {symbol!r}"
        )
    try:
        per_lot = table.per_round_trip_usd[symbol]
    except KeyError as exc:
        raise ValueError(
            f"unknown symbol {symbol!r} for firm {table.firm!r}; "
            f"available symbols: {sorted(table.per_round_trip_usd.keys())}"
        ) from exc
    return per_lot * volume_lots


# --------------------------------------------------------------------------- #
# Snapshot dates and source paths
# --------------------------------------------------------------------------- #
#: All three firms' commission snapshots were retrieved together on this date.
#: A re-fetch must update each firm's ``snapshot_date`` independently — they
#: do not have to move in lockstep, but they did the first time.
_SNAPSHOT_DATE: Final[date] = date(2026, 5, 12)

_FTMO_SNAPSHOT_SOURCE: Final[str] = "docs/firm-tos-snapshots/ftmo-commission-2026-05-12.md"
_FUNDEDNEXT_SNAPSHOT_SOURCE: Final[str] = (
    "docs/firm-tos-snapshots/fundednext-commission-2026-05-12.md"
)
_FUNDINGPIPS_SNAPSHOT_SOURCE: Final[str] = (
    "docs/firm-tos-snapshots/fundingpips-commission-2026-05-12.md"
)


# --------------------------------------------------------------------------- #
# FTMO MT5 commission account — verbatim from the snapshot file
# --------------------------------------------------------------------------- #
# Effective 2025-09-29 per FTMO's own trading-update blog post:
#   Forex & Exotics: $2.50/lot/side -> $5.00/lot/round-trip.
#   Metals: 0.0007%/side * notional; at $3500/oz * 100 oz/lot * 2 sides -> $4.90.
#   Indices: zero commission (per the 2025-08-01 "Zero Commissions on Indices"
#   announcement).
FTMO_MT5_COMMISSION: Final[CommissionTable] = CommissionTable(
    firm="ftmo",
    account_type="mt5_commission",
    snapshot_date=_SNAPSHOT_DATE,
    snapshot_source=_FTMO_SNAPSHOT_SOURCE,
    per_round_trip_usd={
        "EURUSD": 5.00,
        "GBPUSD": 5.00,
        "USDJPY": 5.00,
        "XAUUSD": 4.90,
        "GER40": 0.00,
        "US100": 0.00,
    },
)


# --------------------------------------------------------------------------- #
# FundedNext Stellar 2-Step MT5 commission account — verbatim from the snapshot
# --------------------------------------------------------------------------- #
# From the FundedNext help-center article and corroborating secondary sources:
#   Stellar 2-Step forex + commodities: $7.00/lot/round-trip.
#   Metals formula: 0.0016%/side * notional; at $3500/oz * 100 oz/lot * 2
#   sides -> $11.20/round-trip.
#   Indices and oil: $0.
# The Stellar 2-Step forex rate is the row most-likely-uncertain in the
# snapshot file (the help-center table is image-rendered, not text-extractable)
# and is flagged for live-account calibration in Phase 1 Day 2.
FUNDEDNEXT_MT5_COMMISSION: Final[CommissionTable] = CommissionTable(
    firm="fundednext",
    account_type="mt5_commission",
    snapshot_date=_SNAPSHOT_DATE,
    snapshot_source=_FUNDEDNEXT_SNAPSHOT_SOURCE,
    per_round_trip_usd={
        "EURUSD": 7.00,
        "GBPUSD": 7.00,
        "USDJPY": 7.00,
        "XAUUSD": 11.20,
        "GER40": 0.00,
        "US100": 0.00,
    },
)


# --------------------------------------------------------------------------- #
# FundingPips Raw Assessment MT5 — verbatim from the snapshot
# --------------------------------------------------------------------------- #
# FundingPips publishes a flat per-lot commission for forex AND metals, unlike
# FTMO and FundedNext which use percentage-of-notional formulas for metals.
# Raw Assessment 1-Step / 2-Step / 2-Step Pro: $5.00/lot/RT on forex and metals.
# Indices and oil: $0.
FUNDINGPIPS_MT5_COMMISSION: Final[CommissionTable] = CommissionTable(
    firm="fundingpips",
    account_type="mt5_commission",
    snapshot_date=_SNAPSHOT_DATE,
    snapshot_source=_FUNDINGPIPS_SNAPSHOT_SOURCE,
    per_round_trip_usd={
        "EURUSD": 5.00,
        "GBPUSD": 5.00,
        "USDJPY": 5.00,
        "XAUUSD": 5.00,
        "GER40": 0.00,
        "US100": 0.00,
    },
)


# --------------------------------------------------------------------------- #
# Public registry
# --------------------------------------------------------------------------- #
#: ``firm_name -> table`` lookup. Used by the rules engine and the placebo gate
#: to pick the right cost table for whichever firm an evaluation is targeting.
ALL_TABLES: Final[dict[str, CommissionTable]] = {
    "ftmo": FTMO_MT5_COMMISSION,
    "fundednext": FUNDEDNEXT_MT5_COMMISSION,
    "fundingpips": FUNDINGPIPS_MT5_COMMISSION,
}


__all__ = [
    "ALL_TABLES",
    "FTMO_MT5_COMMISSION",
    "FUNDEDNEXT_MT5_COMMISSION",
    "FUNDINGPIPS_MT5_COMMISSION",
    "CommissionTable",
    "commission_for_trade",
]
