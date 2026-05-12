"""Tests for ``propfarm.data.quality`` (Task 5.1) — holiday calendar, DST, sessions.

The quality module answers three downstream questions:

1. ``expect_data(symbol, date)`` — would we *expect* tick data on that calendar
   date for that symbol? Used by the gap report (Task 5.2) to avoid flagging
   genuine market closures as data gaps.
2. ``is_dst_boundary(ts_utc)`` — does this UTC timestamp fall within a 1-hour
   window of a US or EU DST transition? Used by vendor reconciliation (5.3)
   and gap reports to tolerate the well-known 1-hour vendor disagreement
   around clock changes.
3. ``is_market_open(symbol, ts_utc)`` — combines holiday + session-hours
   semantics per asset class (FX 24/5, XAUUSD with LBMA-style Sunday-23:00
   open, GER40 cash, US100 cash). Unknown symbols raise ``ValueError`` so a
   typo never silently returns ``False``.

Every test is offline and date-anchored to specific 2024 calendar dates whose
DST/holiday status is independent of any vendor calendar cache.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from propfarm.data.quality import (
    expect_data,
    is_dst_boundary,
    is_market_open,
)


# --------------------------------------------------------------------------- #
# expect_data — calendar-day holiday closures for FX
# --------------------------------------------------------------------------- #
def test_christmas_fx_closed() -> None:
    """Dec 25 is a full FX-market close, regardless of weekday."""
    assert expect_data("EURUSD", date(2024, 12, 25)) is False


def test_boxing_day_fx_closed() -> None:
    """Dec 26 is a full FX close across most non-US centers (UK, EU, AU, HK)."""
    assert expect_data("EURUSD", date(2024, 12, 26)) is False


def test_new_years_day_fx_closed() -> None:
    """Jan 1 is a full FX close."""
    assert expect_data("EURUSD", date(2024, 1, 1)) is False


def test_normal_weekday_fx_open() -> None:
    """A bog-standard Thursday in March should expect data."""
    assert expect_data("EURUSD", date(2024, 3, 14)) is True


def test_saturday_fx_closed_via_expect_data() -> None:
    """Saturdays are not trading days even though they aren't 'holidays'."""
    # 2024-03-16 is a Saturday.
    assert expect_data("EURUSD", date(2024, 3, 16)) is False


def test_xauusd_christmas_closed() -> None:
    """Gold respects the same major-holiday calendar as FX."""
    assert expect_data("XAUUSD", date(2024, 12, 25)) is False


def test_unknown_symbol_raises_expect_data() -> None:
    with pytest.raises(ValueError, match="unknown symbol"):
        expect_data("ZZZ", date(2024, 3, 14))


# --------------------------------------------------------------------------- #
# is_dst_boundary — ±1h window around US + EU clock changes
# --------------------------------------------------------------------------- #
def test_us_dst_spring_forward_2024() -> None:
    """US spring-forward 2024-03-10: 02:00 EST -> 03:00 EDT == 07:00 UTC."""
    transition = datetime(2024, 3, 10, 7, 0, tzinfo=UTC)
    assert is_dst_boundary(transition) is True
    # 2 hours before — outside the 1-hour window.
    assert is_dst_boundary(transition - timedelta(hours=2)) is False
    # 2 hours after — outside the 1-hour window.
    assert is_dst_boundary(transition + timedelta(hours=2)) is False


def test_eu_dst_spring_forward_2024() -> None:
    """EU spring-forward 2024-03-31: 01:00 UTC."""
    transition = datetime(2024, 3, 31, 1, 0, tzinfo=UTC)
    assert is_dst_boundary(transition) is True
    assert is_dst_boundary(transition - timedelta(hours=2)) is False
    assert is_dst_boundary(transition + timedelta(hours=2)) is False


def test_us_dst_fall_back_2024() -> None:
    """US fall-back 2024-11-03: 02:00 EDT -> 01:00 EST == 06:00 UTC."""
    transition = datetime(2024, 11, 3, 6, 0, tzinfo=UTC)
    assert is_dst_boundary(transition) is True
    assert is_dst_boundary(transition - timedelta(hours=2)) is False
    assert is_dst_boundary(transition + timedelta(hours=2)) is False


def test_eu_dst_fall_back_2024() -> None:
    """EU fall-back 2024-10-27: 01:00 UTC."""
    transition = datetime(2024, 10, 27, 1, 0, tzinfo=UTC)
    assert is_dst_boundary(transition) is True
    assert is_dst_boundary(transition - timedelta(hours=2)) is False
    assert is_dst_boundary(transition + timedelta(hours=2)) is False


def test_dst_boundary_requires_tz_aware() -> None:
    """Naive datetimes must raise so callers cannot accidentally compare wall clocks."""
    with pytest.raises(ValueError, match="tz-aware"):
        is_dst_boundary(datetime(2024, 3, 10, 7, 0))


def test_dst_boundary_window_edge_inclusive_at_one_hour() -> None:
    """The window is closed at ±1 hour: exactly 1h offset is still a boundary."""
    transition = datetime(2024, 3, 10, 7, 0, tzinfo=UTC)
    assert is_dst_boundary(transition - timedelta(hours=1)) is True
    assert is_dst_boundary(transition + timedelta(hours=1)) is True


# --------------------------------------------------------------------------- #
# is_market_open — session hours by asset class
# --------------------------------------------------------------------------- #
def test_fx_sunday_open() -> None:
    """FX week opens Sunday 22:00 UTC."""
    # 2024-03-17 is a Sunday.
    assert is_market_open("EURUSD", datetime(2024, 3, 17, 22, 0, tzinfo=UTC)) is True
    # Saturday: closed.
    assert is_market_open("EURUSD", datetime(2024, 3, 16, 12, 0, tzinfo=UTC)) is False


def test_fx_friday_close() -> None:
    """FX week closes Friday 22:00 UTC."""
    # 2024-03-15 is a Friday.
    assert is_market_open("EURUSD", datetime(2024, 3, 15, 21, 59, tzinfo=UTC)) is True
    assert is_market_open("EURUSD", datetime(2024, 3, 15, 22, 0, tzinfo=UTC)) is False


def test_fx_christmas_closed_intraday() -> None:
    """Even at an otherwise-tradeable hour, Christmas is closed."""
    assert is_market_open("EURUSD", datetime(2024, 12, 25, 12, 0, tzinfo=UTC)) is False


def test_xauusd_weekend_close() -> None:
    """Gold closes Fri 22:00 UTC and reopens Sunday 23:00 UTC (LBMA-style)."""
    # Friday 2024-03-15 23:00 UTC: closed.
    assert is_market_open("XAUUSD", datetime(2024, 3, 15, 23, 0, tzinfo=UTC)) is False
    # Sunday 2024-03-17 22:30 UTC: still closed (FX would be open here).
    assert is_market_open("XAUUSD", datetime(2024, 3, 17, 22, 30, tzinfo=UTC)) is False
    # Sunday 2024-03-17 23:30 UTC: open.
    assert is_market_open("XAUUSD", datetime(2024, 3, 17, 23, 30, tzinfo=UTC)) is True


def test_ger40_outside_hours() -> None:
    """GER40 cash session is 07:00-21:00 UTC Mon-Fri (Xetra plus extended)."""
    # Monday 2024-03-11.
    assert is_market_open("GER40", datetime(2024, 3, 11, 5, 0, tzinfo=UTC)) is False
    assert is_market_open("GER40", datetime(2024, 3, 11, 10, 0, tzinfo=UTC)) is True
    assert is_market_open("GER40", datetime(2024, 3, 11, 21, 0, tzinfo=UTC)) is False


def test_ger40_weekend_closed() -> None:
    assert is_market_open("GER40", datetime(2024, 3, 16, 10, 0, tzinfo=UTC)) is False


def test_us100_cash_session() -> None:
    """US100: per Dukascopy convention we accept cash session 14:30-21:00 UTC Mon-Fri.

    Decision documented in ``quality.py``: we use the cash session only.
    Extended-hours quotes exist on Dukascopy but liquidity is thin and the
    cost model has not been calibrated outside the cash window, so the gap
    report would over-flag if we widened the definition here.
    """
    # Monday 2024-03-11.
    assert is_market_open("US100", datetime(2024, 3, 11, 12, 0, tzinfo=UTC)) is False
    assert is_market_open("US100", datetime(2024, 3, 11, 14, 30, tzinfo=UTC)) is True
    assert is_market_open("US100", datetime(2024, 3, 11, 20, 59, tzinfo=UTC)) is True
    assert is_market_open("US100", datetime(2024, 3, 11, 21, 0, tzinfo=UTC)) is False


def test_index_christmas_closed() -> None:
    """Indices respect the holiday calendar too."""
    assert is_market_open("GER40", datetime(2024, 12, 25, 10, 0, tzinfo=UTC)) is False
    assert is_market_open("US100", datetime(2024, 12, 25, 15, 0, tzinfo=UTC)) is False


def test_unknown_symbol_raises() -> None:
    """A typo must raise loudly, not silently return False."""
    with pytest.raises(ValueError, match="unknown symbol"):
        is_market_open("ZZZ", datetime(2024, 3, 11, 10, 0, tzinfo=UTC))


def test_is_market_open_requires_tz_aware() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        is_market_open("EURUSD", datetime(2024, 3, 11, 10, 0))


def test_all_supported_symbols_have_session_rules() -> None:
    """Every symbol in SUPPORTED_SYMBOLS must accept a query at noon UTC Monday."""
    from propfarm.data.quality import SUPPORTED_SYMBOLS

    ts = datetime(2024, 3, 11, 12, 0, tzinfo=UTC)
    for sym in SUPPORTED_SYMBOLS:
        # We don't assert open/closed — only that the call does not raise.
        is_market_open(sym, ts)
