"""Data-quality predicates: holiday calendar, DST boundaries, session hours (Task 5.1).

Three downstream consumers depend on this module getting the *right* answer:

1. **Gap report (Task 5.2)** — flags a missing-data interval only when
   :func:`expect_data` would have predicted data on that calendar day.
   Without this guard, Christmas and Boxing Day appear as 24-hour data gaps
   every year, drowning every legitimate vendor outage in noise.
2. **Vendor reconciliation (Task 5.3)** — tolerates the well-known 1-hour
   vendor disagreement around DST transitions, via :func:`is_dst_boundary`.
3. **Simulator + cost model (Tasks 6.x/7.x)** — uses :func:`is_market_open`
   to refuse to fill a synthetic order when the market is closed, which is
   how we keep the placebo gate honest.

Source choices and conventions
------------------------------
* **Holiday list (FX).** FX markets do not have a single authoritative
  exchange calendar — they are an OTC network of bank dealing desks across
  Sydney/Tokyo/London/New York. We start from ``pandas_market_calendars``'
  ``CME_Currency`` calendar (the closest exchange-traded analogue: CME FX
  futures), which contributes only **New Year's Day** and **Christmas Day**.
  We then overlay **Boxing Day (Dec 26)** because the London and Sydney
  centers — which between them cover ~50% of FX volume — observe it,
  rendering global liquidity effectively zero. This matches the FTMO /
  FundedNext "FX no-trade days" published holiday lists for 2023-2025.
  Good Friday is intentionally *not* added: London FX desks observe it but
  the US is open, so dukascopy ticks exist and a "no data expected" flag
  would be wrong.
* **DST window.** :func:`is_dst_boundary` returns True for any UTC timestamp
  within **±1 hour** of either the US (2nd Sun Mar / 1st Sun Nov, 02:00 ET)
  or the EU (last Sun Mar / last Sun Oct, 01:00 UTC) transition. The 1-hour
  half-width is deliberately equal to the size of the clock shift itself:
  any vendor whose ingestion pipeline is off by one DST step disagrees by
  at most 1 hour, so a 1-hour window catches every such case without
  silencing genuine outages further away.
* **FX session.** 24/5 with the weekly open at **Sunday 22:00 UTC** and
  the weekly close at **Friday 22:00 UTC**, matching the Sydney
  open / NY close convention used by every retail aggregator we have seen.
* **XAUUSD.** Same week shape as FX but reopens **Sunday 23:00 UTC** per
  LBMA's spot-gold convention — one hour later than FX. The Friday close
  is at 22:00 UTC, identical to FX.
* **GER40 (DAX).** Xetra extended hours **08:00-22:00 Frankfurt local**,
  Mon-Fri. UTC equivalent shifts with European DST: 07:00-21:00 UTC in
  winter (CET = UTC+1), 06:00-20:00 UTC in summer (CEST = UTC+2). The
  session is computed per-day via ``ZoneInfo("Europe/Berlin")`` rather
  than hardcoded to UTC, so backtests stay in-phase across the spring
  and autumn DST transitions. Closed on the FX holiday calendar too,
  since Dec 25 / Dec 26 / Jan 1 are also Xetra holidays.
* **US100 (NDX).** *Cash session only:* **09:30-16:00 New York local**,
  Mon-Fri. UTC equivalent shifts with US DST: 14:30-21:00 UTC in winter
  (EST = UTC-5), 13:30-20:00 UTC in summer (EDT = UTC-4). Computed
  per-day via ``ZoneInfo("America/New_York")`` for the same reason as
  GER40 — hardcoding UTC silently mis-times every backtest for the ~8
  months/year the US observes DST. Dukascopy quotes US100 in extended
  hours (pre-market + after-hours), but liquidity is thin, the cost
  model has not been calibrated outside the cash window, and the gap
  report would systematically over-flag if we widened the definition.

Constraints
-----------
* All ``datetime`` inputs **must be tz-aware UTC** — naive datetimes raise
  ``ValueError``. This prevents an entire class of accidental wall-clock
  comparisons in downstream code.
* Unknown symbols raise ``ValueError`` rather than silently returning
  ``False``: a typo should fail loudly, not pretend the market is shut.
* No broker, no MT5, no VPS strings appear in this module — quality is
  broker-agnostic.

Public API
----------
* :func:`expect_data`
* :func:`is_dst_boundary`
* :func:`is_market_open`
* :data:`SUPPORTED_SYMBOLS`
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Symbol registry
# --------------------------------------------------------------------------- #

#: Asset-class tag per symbol.  Used internally to dispatch session-hours
#: rules. Adding a symbol means: (a) add it here, (b) confirm the holiday
#: list applies (it does for everything Phase 0 trades), (c) ensure
#: :func:`_session_open` handles the asset class.
_ASSET_CLASS: Final[dict[str, str]] = {
    "EURUSD": "fx",
    "GBPUSD": "fx",
    "USDJPY": "fx",
    "XAUUSD": "xau",
    "GER40": "ger40",
    "US100": "us100",
}

#: Public tuple of supported symbols. Iteration order matches the dict above.
SUPPORTED_SYMBOLS: Final[tuple[str, ...]] = tuple(_ASSET_CLASS.keys())


# --------------------------------------------------------------------------- #
# Holiday calendar
# --------------------------------------------------------------------------- #
def _is_full_holiday(d: date) -> bool:
    """True if ``d`` is a global FX full-close (Jan 1, Dec 25, Dec 26).

    Rationale and source choice are documented in the module docstring.
    These three dates apply across every symbol we currently support.
    """
    return (d.month, d.day) in {(1, 1), (12, 25), (12, 26)}


def _is_weekend_day(d: date) -> bool:
    """True if ``d`` is a Saturday or Sunday (no FX trading either day).

    Note: the *Sunday evening* FX open (22:00 UTC) is a session-hours
    concern, not a calendar-day concern — :func:`is_market_open` handles
    that. :func:`expect_data` is per calendar day, where Sunday is still
    not a normal trading day.
    """
    return d.weekday() >= 5  # Mon=0 .. Sun=6


def _require_symbol(symbol: str) -> str:
    """Return the asset class for ``symbol``, raising on unknown symbols."""
    try:
        return _ASSET_CLASS[symbol]
    except KeyError as exc:
        raise ValueError(
            f"unknown symbol {symbol!r}; supported symbols: {SUPPORTED_SYMBOLS}"
        ) from exc


def _require_utc(ts: datetime, *, arg_name: str = "ts_utc") -> None:
    """Raise ``ValueError`` unless ``ts`` is tz-aware (no naive datetimes)."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def expect_data(symbol: str, d: date) -> bool:
    """Return True if we *expect* trading data on calendar date ``d`` for ``symbol``.

    Used by the gap report (Task 5.2): a missing-data interval on a date
    where ``expect_data is False`` is a calendar artefact, not a vendor
    outage, and should not be flagged.

    Parameters
    ----------
    symbol : str
        Must be a member of :data:`SUPPORTED_SYMBOLS`.
    d : datetime.date
        Calendar date to evaluate.

    Returns
    -------
    bool
        ``False`` if ``d`` is a weekend, ``False`` if ``d`` is a global FX
        full-close (Jan 1 / Dec 25 / Dec 26), ``True`` otherwise.

    Notes
    -----
    No Monday-substitution is applied for holidays that fall on a weekend.
    If Jan 1 lands on a Saturday, the calendar date is closed (so the
    weekend check already returns False), but the following Monday is
    treated as an ordinary trading day even though some venues (e.g. UK
    bank-holiday substitution) observe the holiday on Monday. This is
    deliberate: the data layer asks "should vendor ticks exist on this
    date?", not "is human trading allowed?". The latter is the rules
    layer's job (Task 11.1).

    Raises
    ------
    ValueError
        If ``symbol`` is not in :data:`SUPPORTED_SYMBOLS`.
    """
    _require_symbol(symbol)
    if _is_weekend_day(d):
        return False
    return not _is_full_holiday(d)


# --------------------------------------------------------------------------- #
# DST boundaries (US + EU)
# --------------------------------------------------------------------------- #
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the ``n``-th occurrence of ``weekday`` in ``year``/``month``.

    ``weekday`` follows Python convention (Mon=0 .. Sun=6). ``n`` is 1-based.
    """
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the date of the *last* ``weekday`` in ``year``/``month``."""
    # Walk back from the last day of the month (always lands within the month).
    last_day = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _us_dst_transitions_utc(year: int) -> tuple[datetime, datetime]:
    """Return ``(spring_forward_utc, fall_back_utc)`` for US DST in ``year``.

    Both anchors are the *UTC instant* at which the wall clock shifts.

    * Spring forward: 2nd Sunday of March, 02:00 EST -> 03:00 EDT. The
      02:00 EST instant equals 07:00 UTC.
    * Fall back: 1st Sunday of November, 02:00 EDT -> 01:00 EST. The
      02:00 EDT instant equals 06:00 UTC.
    """
    spring = _nth_weekday(year, 3, weekday=6, n=2)  # Sunday=6
    fall = _nth_weekday(year, 11, weekday=6, n=1)
    spring_utc = datetime(spring.year, spring.month, spring.day, 7, 0, tzinfo=UTC)
    fall_utc = datetime(fall.year, fall.month, fall.day, 6, 0, tzinfo=UTC)
    return spring_utc, fall_utc


def _eu_dst_transitions_utc(year: int) -> tuple[datetime, datetime]:
    """Return ``(spring_forward_utc, fall_back_utc)`` for EU DST in ``year``.

    EU DST transitions happen at **01:00 UTC** on the last Sunday of March
    (spring forward) and last Sunday of October (fall back), per the EU
    directive 2000/84/EC.
    """
    spring = _last_weekday(year, 3, weekday=6)
    fall = _last_weekday(year, 10, weekday=6)
    spring_utc = datetime(spring.year, spring.month, spring.day, 1, 0, tzinfo=UTC)
    fall_utc = datetime(fall.year, fall.month, fall.day, 1, 0, tzinfo=UTC)
    return spring_utc, fall_utc


#: Half-width of the DST tolerance window. A vendor whose pipeline drifts
#: by one DST step disagrees by at most 1 hour, so a 1-hour radius catches
#: every such case without silencing outages further away.
_DST_WINDOW: Final[timedelta] = timedelta(hours=1)


def is_dst_boundary(ts_utc: datetime) -> bool:
    """Return True if ``ts_utc`` is within ±1 hour of a US or EU DST transition.

    Parameters
    ----------
    ts_utc : datetime.datetime
        Must be tz-aware. The comparison is performed in UTC.

    Returns
    -------
    bool
        ``True`` iff ``|ts_utc - transition| <= 1 hour`` for any of the four
        DST transitions (US spring/fall, EU spring/fall) in the same or
        adjacent year. Checking adjacent years is defensive: an EU
        transition very late in December could in principle fall within
        a window straddling a year boundary, though in practice the EU
        October transition is the latest of the four.

    Raises
    ------
    ValueError
        If ``ts_utc`` is naive (no tzinfo).
    """
    _require_utc(ts_utc, arg_name="ts_utc")
    # Normalize to UTC in case the caller passed a non-UTC tz-aware datetime.
    ts = ts_utc.astimezone(UTC)
    year = ts.year
    candidates: list[datetime] = []
    for y in (year - 1, year, year + 1):
        candidates.extend(_us_dst_transitions_utc(y))
        candidates.extend(_eu_dst_transitions_utc(y))
    return any(abs(ts - t) <= _DST_WINDOW for t in candidates)


# --------------------------------------------------------------------------- #
# Session hours by asset class
# --------------------------------------------------------------------------- #
# Weekday constants for readability.
_MON: Final[int] = 0
_FRI: Final[int] = 4
_SAT: Final[int] = 5
_SUN: Final[int] = 6


def _fx_session_open(ts: datetime) -> bool:
    """FX 24/5: Sunday 22:00 UTC inclusive -> Friday 22:00 UTC exclusive.

    Weekend gap: Friday 22:00 UTC onwards through Sunday 21:59 UTC is closed.
    """
    wd = ts.weekday()
    if _MON <= wd <= 3:  # Mon-Thu: fully open.
        return True
    if wd == _FRI:
        return ts.hour < 22
    if wd == _SAT:
        return False
    # Sunday: open from 22:00 UTC onwards.
    return ts.hour >= 22


def _xau_session_open(ts: datetime) -> bool:
    """Gold: same Fri 22:00 UTC close as FX, but Sunday reopen at 23:00 UTC.

    The 1-hour-later open mirrors the LBMA spot-gold convention; FX desks
    that quote XAU often align to it.
    """
    wd = ts.weekday()
    if _MON <= wd <= 3:
        return True
    if wd == _FRI:
        return ts.hour < 22
    if wd == _SAT:
        return False
    # Sunday: open from 23:00 UTC onwards (one hour after FX).
    return ts.hour >= 23


def _ger40_session_open(ts: datetime) -> bool:
    """GER40: Xetra extended hours 08:00-22:00 Frankfurt local, Mon-Fri.

    UTC equivalent shifts with European DST: 07:00-21:00 UTC in winter
    (CET = UTC+1), 06:00-20:00 UTC in summer (CEST = UTC+2). We compute
    the session window per-day via ``ZoneInfo("Europe/Berlin")`` so the
    gap report does not silently flag the first or last hour of summer
    sessions as phantom outages.
    """
    if ts.weekday() > _FRI:
        return False
    local = ts.astimezone(ZoneInfo("Europe/Berlin"))
    minute_of_day = local.hour * 60 + local.minute
    return 8 * 60 <= minute_of_day < 22 * 60


def _us100_session_open(ts: datetime) -> bool:
    """US100 cash: Mon-Fri, 09:30-16:00 New York local. Extended hours excluded.

    UTC equivalent shifts with US DST: 14:30-21:00 UTC in winter (EST),
    13:30-20:00 UTC in summer (EDT). We compute the session per-day via
    ``ZoneInfo("America/New_York")`` so backtests are not silently off by
    one hour for the ~8 months/year the US observes DST.

    Extended-hours quotes (pre-market and after-hours) are intentionally
    excluded: the cost model is calibrated only for the cash session, so
    the gap report should not expect data outside it.
    """
    if ts.weekday() > _FRI:
        return False
    local = ts.astimezone(ZoneInfo("America/New_York"))
    minute_of_day = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minute_of_day < 16 * 60


_SESSION_FN: Final[dict[str, Callable[[datetime], bool]]] = {
    "fx": _fx_session_open,
    "xau": _xau_session_open,
    "ger40": _ger40_session_open,
    "us100": _us100_session_open,
}


def is_market_open(symbol: str, ts_utc: datetime) -> bool:
    """Return True if ``symbol`` is in its trading session at ``ts_utc``.

    Combines two checks:

    1. The calendar date is not a full-close holiday (Jan 1 / Dec 25 / Dec 26).
    2. The intraday session window for the symbol's asset class includes
       ``ts_utc`` (see module docstring for per-class windows).

    Parameters
    ----------
    symbol : str
        Must be a member of :data:`SUPPORTED_SYMBOLS`.
    ts_utc : datetime.datetime
        Tz-aware UTC timestamp.

    Returns
    -------
    bool
        ``True`` iff the market is open. ``False`` on holidays, weekends
        (outside the FX/XAU Sunday-evening window), and outside the
        symbol's intraday hours.

    Raises
    ------
    ValueError
        If ``symbol`` is not supported, or if ``ts_utc`` is naive.
    """
    asset_class = _require_symbol(symbol)
    _require_utc(ts_utc, arg_name="ts_utc")
    ts = ts_utc.astimezone(UTC)
    if _is_full_holiday(ts.date()):
        return False
    return _SESSION_FN[asset_class](ts)


__all__ = [
    "SUPPORTED_SYMBOLS",
    "expect_data",
    "is_dst_boundary",
    "is_market_open",
]
