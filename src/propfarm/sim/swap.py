"""Swap / overnight financing model with the triple-Wednesday rule (Task 6.3).

Why this module exists
----------------------
A position held across the **22:00 New York-time rollover** accrues a swap
(overnight financing) charge or credit, computed from the interest-rate
differential between the two currencies (FX) or the broker's posted
financing rate (metals, indices). On the simulator side, getting this
wrong silently mis-prices any strategy that holds positions overnight:

* a swing strategy that backtests as a winner can bleed cash in
  production because we forgot the swap leg;
* a "carry" strategy designed to *earn* swap will be flagged as a loser
  if we put the sign upside-down;
* the **triple-Wednesday rule** — three nights' swap charged at the
  Wednesday 22:00 ET rollover, covering the Sat+Sun+Mon T+2 settlement
  gap — is responsible for ~30% of weekly swap cost on FX majors. Miss
  it and any P&L attribution over the weekend is wrong.

Sign convention (CRITICAL)
--------------------------
The MT5 broker-server fields ``swap_long`` and ``swap_short`` follow the
**broker convention**: positive = trader is **credited**, negative =
trader is **charged**. This module stores those raw broker values in
:class:`SwapTable` so that the snapshot docs round-trip 1:1 against the
broker-published numbers.

:func:`swap_for_position` **inverts the sign on output** so its return
follows the **simulator's PnL convention** used by the rest of
``propfarm.sim`` (and the rules layer):

* ``return > 0`` — swap is a **cost** charged to the trader.
* ``return < 0`` — swap is a **credit** earned by the trader.
* ``return == 0`` — no rollover crossings inside the position window.

Time-zone & DST
---------------
The rollover instant is **22:00 America/New_York**, which translates to
**02:00 UTC** during EDT (mid-March to early November) and **03:00 UTC**
during EST (rest of the year). We compute the per-day rollover instant
via ``zoneinfo.ZoneInfo("America/New_York")`` rather than hardcoding a
UTC offset, so the simulator stays in-phase across DST transitions
without manual tweaks.

A "swap night" — the unit returned by :func:`nights_held` — is one
crossing of that 22:00 ET instant where the FX market is open on the
relevant trade-date. Weekend crossings (Fri 22:00, Sat 22:00, Sun 22:00
in NY-local time) are **not** counted: those settlement nights are
already pre-paid by the Wednesday triple. The Wednesday rollover
(NY-local Wed 22:00) counts as **3 nights**.

If you need rollover behavior for a non-FX symbol that does not follow
the T+2 weekend convention, pass ``triple_rollover_weekday=None`` to
:func:`nights_held`.

Why not :func:`propfarm.data.quality.is_market_open`?
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
``is_market_open`` answers "is this symbol tradable at this UTC instant"
— a per-symbol session-hours question. The rollover is a wall-clock
instant defined globally (22:00 NY), independent of symbol. The two
checks overlap (weekend FX is closed and weekend rollovers are not
charged) but they answer different questions, and conflating them
would silently mis-count any cross-asset position where a symbol's
session hours differ from the FX rollover schedule (e.g. GER40 closes
at 21:00 UTC summer, an hour before the FX rollover).

Known gap (documented, not yet fixed): the current implementation does
**not** suppress the rollover charge on full-market holidays (Jan 1,
Dec 25, Dec 26) — see ``_is_full_holiday`` in
:mod:`propfarm.data.quality`. In practice these dates almost never
appear on weekday rollovers within a backtest window — Christmas-week
positions are typically closed before the holiday — but a Phase 1
follow-up should add a holiday-skip pass to ``nights_held`` once
calibration against live broker statements clarifies the per-firm
convention (some firms charge holiday rollovers; some don't).

Module-level firm tables
------------------------
:data:`FTMO_MT5_SWAP`, :data:`FUNDEDNEXT_MT5_SWAP`, :data:`FUNDINGPIPS_MT5_SWAP`
are frozen :class:`SwapTable` instances seeded from the three firm
snapshots under ``docs/firm-tos-snapshots/{firm}-swap-2026-05-12.md``.
Their numeric values are flagged UNCERTAIN at snapshot time because the
canonical source (the MT5 terminal's Symbol Specification dialog) is not
reachable from the implementation host. They must be refreshed against
live broker statements before any Phase 1 live work.

Public API
----------
* :class:`SwapTable`
* :func:`nights_held`
* :func:`swap_for_position`
* :data:`FTMO_MT5_SWAP`
* :data:`FUNDEDNEXT_MT5_SWAP`
* :data:`FUNDINGPIPS_MT5_SWAP`
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from propfarm.data.quality import SUPPORTED_SYMBOLS

# --------------------------------------------------------------------------- #
# Time-zone constants
# --------------------------------------------------------------------------- #

#: The rollover wall-clock is anchored to New York time (22:00 NY). The
#: corresponding UTC offset shifts between -05:00 (EST) and -04:00 (EDT)
#: across the year; we never hardcode either offset and instead resolve
#: each rollover instant via ``ZoneInfo`` to stay DST-aware.
_NY: Final[ZoneInfo] = ZoneInfo("America/New_York")

#: Wall-clock hour (in New York time) at which FX rollover occurs.
_ROLLOVER_HOUR_NY: Final[int] = 22

#: NY-local weekdays on which the 22:00 NY rollover is **not** charged.
#: * Friday (4): coincident with weekly close; no swap booked.
#: * Saturday (5): market closed throughout the day.
#: * Sunday (6): the 22:00 NY instant is the weekly reopen, not a rollover.
#:
#: Weekend swap is pre-paid via the Wednesday triple rollover, so excluding
#: these three avoids double-counting.
_NON_CHARGED_NY_WEEKDAYS: Final[frozenset[int]] = frozenset({4, 5, 6})

#: Industry default: charge 3x the daily swap at the **Wednesday** rollover.
_DEFAULT_TRIPLE_WEEKDAY: Final[int] = 2


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
class SwapTable(BaseModel):
    """Frozen swap-rate lookup for one (firm, account_type) pair.

    Fields use the **broker MT5 server convention**: a positive value in
    ``swap_long_points`` / ``swap_short_points`` means the trader is
    **credited** at the rollover. The :func:`swap_for_position` helper
    inverts the sign on output to match the simulator's "positive = cost"
    PnL convention.

    Parameters
    ----------
    firm : str
        Public firm name (e.g. ``"FTMO"``).
    account_type : str
        Account-type label (e.g. ``"MT5"``, ``"MT5-swap-free"``).
    snapshot_date : datetime.date
        The retrieval date of the underlying ToS snapshot.
    snapshot_source : str
        Markdown filename (or URL) under ``docs/firm-tos-snapshots/``.
    triple_rollover_weekday : int
        Python ``weekday()`` (0=Mon..6=Sun) on which the 3x swap is
        charged. Industry default is Wednesday (2).
    swap_long_points : dict[str, float]
        Per-symbol swap charged for **long** positions, in
        **points per lot per night**, broker sign convention.
    swap_short_points : dict[str, float]
        Per-symbol swap charged for **short** positions.
    point_value_usd : dict[str, float]
        USD value of 1 point on 1 lot, used to convert the points-per-lot
        swap into a USD cost. For the FTMO/FundedNext/FundingPips MT5
        feeds at the snapshot date this is approximately 1.00 across the
        six supported symbols; documented per-firm in the snapshot files.

    Invariants
    ----------
    * The three dict fields share the **same key set**. Mismatched keys
      raise ``ValueError`` at construction time (caught by the validator).
    """

    model_config = ConfigDict(frozen=True)

    firm: str
    account_type: str
    snapshot_date: date
    snapshot_source: str
    triple_rollover_weekday: int | None
    swap_long_points: dict[str, float]
    swap_short_points: dict[str, float]
    point_value_usd: dict[str, float]

    @model_validator(mode="after")
    def _check_symbol_keys_match(self) -> SwapTable:
        long_keys = set(self.swap_long_points)
        short_keys = set(self.swap_short_points)
        pv_keys = set(self.point_value_usd)
        if not (long_keys == short_keys == pv_keys):
            raise ValueError(
                "SwapTable symbol-key mismatch: "
                f"swap_long_points={sorted(long_keys)}, "
                f"swap_short_points={sorted(short_keys)}, "
                f"point_value_usd={sorted(pv_keys)} "
                "— all three dicts must cover the same symbols."
            )
        return self


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str) -> None:
    """Reject naive datetimes — every caller must pass tz-aware UTC."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def _rollover_instant_utc(ny_date: date) -> datetime:
    """Return the UTC instant of the 22:00 NY rollover on the given NY-local date.

    Uses ``ZoneInfo("America/New_York")`` for the local→UTC conversion, so
    the result follows DST: 02:00 UTC next-day during EDT, 03:00 UTC
    next-day during EST. The DST transition itself never happens at
    22:00 NY (transitions are at 02:00 local), so the local time is
    always unambiguous on the given date.
    """
    local = datetime.combine(ny_date, time(_ROLLOVER_HOUR_NY, 0), tzinfo=_NY)
    return local.astimezone(UTC)


def _ny_date_range(start_utc: datetime, end_utc: datetime) -> list[date]:
    """Enumerate NY-local calendar dates from start_utc to end_utc inclusive.

    We pad by ±1 day so that the caller can intersect with the
    [open, close] half-open window via direct comparison of rollover
    UTC instants; off-by-one errors at the day boundary are caught by
    that intersection step, not here.
    """
    start_ny_date = start_utc.astimezone(_NY).date()
    end_ny_date = end_utc.astimezone(_NY).date()
    span = (end_ny_date - start_ny_date).days
    return [start_ny_date + timedelta(days=i) for i in range(-1, span + 2)]


# --------------------------------------------------------------------------- #
# Public functions
# --------------------------------------------------------------------------- #
def nights_held(
    *,
    open_ts_utc: datetime,
    close_ts_utc: datetime,
    triple_rollover_weekday: int | None = _DEFAULT_TRIPLE_WEEKDAY,
) -> int:
    """Count "swap nights" between ``open_ts_utc`` and ``close_ts_utc``.

    A "swap night" is a crossing of the 22:00 New York-time rollover where:

    1. The rollover instant ``T_roll`` satisfies
       ``open_ts_utc < T_roll <= close_ts_utc`` (the position is held
       *through* the instant), and
    2. The NY-local weekday at ``T_roll`` is **not** one of the
       non-charged weekend weekdays (Fri/Sat/Sun) — weekend swap is
       pre-paid via the Wednesday triple.

    On the ``triple_rollover_weekday`` (NY-local), the crossing counts as
    **3 nights**. On all other charged weekdays it counts as 1.

    Parameters
    ----------
    open_ts_utc : datetime.datetime
        Position open time, **tz-aware UTC**. Naive datetimes raise.
    close_ts_utc : datetime.datetime
        Position close time, tz-aware UTC.
    triple_rollover_weekday : int or None, default 2 (Wednesday)
        Python ``weekday()`` (0=Mon..6=Sun) of the NY-local day on which
        the 3x triple swap is charged. Pass ``None`` to disable the
        triple-multiplier (useful for asset classes whose financing
        does not follow FX T+2 settlement, e.g. some index CFDs).

    Returns
    -------
    int
        Number of "swap nights" (each unit is one daily-swap charge);
        the Wednesday rollover contributes 3.

    Raises
    ------
    ValueError
        If either datetime is naive (no ``tzinfo``).

    Notes
    -----
    * If ``close_ts_utc <= open_ts_utc``, the function returns ``0``
      without raising. This makes the function robust against
      simulator code that closes a position at the same bar it opens
      (a zero-hold scratch).
    * The implementation enumerates every NY-local date between open
      and close and converts each NY-local 22:00 to UTC via
      ``zoneinfo``. This is O(span_days) — fine for backtest positions
      (typical hold ≤ 30 days) and not a hot-path concern.
    """
    _require_utc(open_ts_utc, arg_name="open_ts_utc")
    _require_utc(close_ts_utc, arg_name="close_ts_utc")
    if close_ts_utc <= open_ts_utc:
        return 0

    total = 0
    for ny_date in _ny_date_range(open_ts_utc, close_ts_utc):
        rollover_utc = _rollover_instant_utc(ny_date)
        if not (open_ts_utc < rollover_utc <= close_ts_utc):
            continue
        if ny_date.weekday() in _NON_CHARGED_NY_WEEKDAYS:
            # Friday, Saturday, or Sunday in NY-local time — weekend swap
            # is pre-paid via the Wednesday triple; do not double-charge.
            continue
        if triple_rollover_weekday is not None and ny_date.weekday() == triple_rollover_weekday:
            total += 3
        else:
            total += 1
    return total


def swap_for_position(
    *,
    table: SwapTable,
    symbol: str,
    direction: str,
    volume_lots: float,
    open_ts_utc: datetime,
    close_ts_utc: datetime,
) -> float:
    """Compute the signed USD swap cost for one position.

    Parameters
    ----------
    table : SwapTable
        The firm-specific swap-rate table to consult.
    symbol : str
        Trading symbol; must appear in both ``table.swap_long_points``
        and ``table.swap_short_points`` (validated at table construction).
    direction : str
        Either ``"long"`` or ``"short"`` (case-insensitive). Anything
        else raises ``ValueError``.
    volume_lots : float
        Position size in lots. ``0`` returns ``0.0`` exactly.
    open_ts_utc, close_ts_utc : datetime.datetime
        Position open / close times, tz-aware UTC.

    Returns
    -------
    float
        Signed USD swap cost, **simulator convention** (positive = cost,
        negative = credit, zero = no rollover crossings).

        The arithmetic is::

            usd = -1.0
                  * broker_points_per_night
                  * point_value_usd[symbol]
                  * volume_lots
                  * nights_held(open, close, triple=table.triple_rollover_weekday)

        The leading ``-1.0`` is the sign inversion from broker convention
        (positive = credit) to simulator convention (positive = cost).

    Raises
    ------
    ValueError
        * If ``symbol`` is not in the table.
        * If ``direction`` is not ``"long"`` or ``"short"``.
        * If either timestamp is naive.
    """
    direction_norm = direction.lower()
    if direction_norm == "long":
        rate_map = table.swap_long_points
    elif direction_norm == "short":
        rate_map = table.swap_short_points
    else:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    if symbol not in rate_map:
        raise ValueError(
            f"unknown symbol {symbol!r} for SwapTable(firm={table.firm!r}); "
            f"supported: {sorted(rate_map)}"
        )
    if symbol not in table.point_value_usd:  # pragma: no cover — guarded by model_validator
        raise ValueError(
            f"unknown symbol {symbol!r} in point_value_usd for SwapTable(firm={table.firm!r})"
        )

    if volume_lots == 0.0:
        return 0.0

    nights = nights_held(
        open_ts_utc=open_ts_utc,
        close_ts_utc=close_ts_utc,
        triple_rollover_weekday=table.triple_rollover_weekday,
    )
    if nights == 0:
        return 0.0

    broker_points = rate_map[symbol]
    point_value = table.point_value_usd[symbol]
    # Sign inversion: broker convention "+ = credit" → simulator "+ = cost".
    return -1.0 * broker_points * point_value * volume_lots * nights


# --------------------------------------------------------------------------- #
# Firm-specific snapshot tables
# --------------------------------------------------------------------------- #
# Each table mirrors a markdown snapshot under docs/firm-tos-snapshots/.
# Values are flagged UNCERTAIN in those snapshots because the canonical
# source (the MT5 terminal's Symbol Specification dialog) is not reachable
# from this host. They are seeded from publicly-archived community
# references so the tests exercise non-trivial signed rates; they MUST be
# refreshed against a live broker session before any Phase 1 live trading.

#: FTMO MT5 (non-swap-free) swap-rate snapshot.
#: Source: docs/firm-tos-snapshots/ftmo-swap-2026-05-12.md (UNCERTAIN).
FTMO_MT5_SWAP: Final[SwapTable] = SwapTable(
    firm="FTMO",
    account_type="MT5",
    snapshot_date=date(2026, 5, 12),
    snapshot_source="docs/firm-tos-snapshots/ftmo-swap-2026-05-12.md",
    triple_rollover_weekday=_DEFAULT_TRIPLE_WEEKDAY,
    swap_long_points={
        "EURUSD": -7.20,
        "GBPUSD": -3.40,
        "USDJPY": +8.10,
        "XAUUSD": -22.50,
        "GER40": -1.10,
        "US100": -2.50,
    },
    swap_short_points={
        "EURUSD": +2.30,
        "GBPUSD": -0.80,
        "USDJPY": -14.20,
        "XAUUSD": +9.80,
        "GER40": -0.40,
        "US100": -0.90,
    },
    point_value_usd={s: 1.0 for s in SUPPORTED_SYMBOLS},
)

#: FundedNext MT5 (non-swap-free) swap-rate snapshot.
#: Source: docs/firm-tos-snapshots/fundednext-swap-2026-05-12.md (UNCERTAIN).
FUNDEDNEXT_MT5_SWAP: Final[SwapTable] = SwapTable(
    firm="FundedNext",
    account_type="MT5",
    snapshot_date=date(2026, 5, 12),
    snapshot_source="docs/firm-tos-snapshots/fundednext-swap-2026-05-12.md",
    triple_rollover_weekday=_DEFAULT_TRIPLE_WEEKDAY,
    swap_long_points={
        "EURUSD": -6.80,
        "GBPUSD": -3.10,
        "USDJPY": +7.50,
        "XAUUSD": -21.00,
        "GER40": -0.95,
        "US100": -2.30,
    },
    swap_short_points={
        "EURUSD": +1.90,
        "GBPUSD": -0.60,
        "USDJPY": -13.40,
        "XAUUSD": +9.20,
        "GER40": -0.35,
        "US100": -0.80,
    },
    point_value_usd={s: 1.0 for s in SUPPORTED_SYMBOLS},
)

#: FundingPips MT5 (non-swap-free) swap-rate snapshot.
#: Source: docs/firm-tos-snapshots/fundingpips-swap-2026-05-12.md (UNCERTAIN).
FUNDINGPIPS_MT5_SWAP: Final[SwapTable] = SwapTable(
    firm="FundingPips",
    account_type="MT5",
    snapshot_date=date(2026, 5, 12),
    snapshot_source="docs/firm-tos-snapshots/fundingpips-swap-2026-05-12.md",
    triple_rollover_weekday=_DEFAULT_TRIPLE_WEEKDAY,
    swap_long_points={
        "EURUSD": -7.50,
        "GBPUSD": -3.60,
        "USDJPY": +8.40,
        "XAUUSD": -23.00,
        "GER40": -1.20,
        "US100": -2.70,
    },
    swap_short_points={
        "EURUSD": +2.10,
        "GBPUSD": -0.90,
        "USDJPY": -14.80,
        "XAUUSD": +10.00,
        "GER40": -0.45,
        "US100": -1.00,
    },
    point_value_usd={s: 1.0 for s in SUPPORTED_SYMBOLS},
)


__all__ = [
    "FTMO_MT5_SWAP",
    "FUNDEDNEXT_MT5_SWAP",
    "FUNDINGPIPS_MT5_SWAP",
    "SwapTable",
    "nights_held",
    "swap_for_position",
]
