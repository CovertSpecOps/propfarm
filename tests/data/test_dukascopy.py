"""Offline-only unit tests for the Dukascopy historical tick downloader.

Every test in this module runs without network access. The single live
integration test is marked ``@pytest.mark.integration`` and is skipped by
default (`pyproject.toml` adds ``-m 'not integration'`` to ``pytest -q``).
"""

from __future__ import annotations

import lzma
import struct
from datetime import UTC, datetime

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from propfarm.data.vendors.dukascopy import (
    DukascopyParseError,
    HttpClient,
    _build_url,
    fetch_ticks,
    parse_bi5,
)

from ._synthetic import SyntheticTick, make_raw_records, make_synthetic_bi5

# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------


def test_url_for_known_hour() -> None:
    """Dukascopy URLs use 0-indexed months (Jan=00). 2024-01-02 10:00 UTC."""
    url = _build_url("EURUSD", datetime(2024, 1, 2, 10, tzinfo=UTC))
    assert url == ("https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/02/10h_ticks.bi5")


def test_url_month_zero_indexed_for_december() -> None:
    """December 2024 must serialize as MM=11, not MM=12."""
    url = _build_url("EURUSD", datetime(2024, 12, 31, 23, tzinfo=UTC))
    assert "/2024/11/31/23h_ticks.bi5" in url


def test_url_requires_utc() -> None:
    """Naive datetimes must be rejected; ts_hour MUST be tz-aware UTC."""
    with pytest.raises(ValueError, match="UTC"):
        _build_url("EURUSD", datetime(2024, 1, 2, 10))  # naive


# ---------------------------------------------------------------------------
# parse_bi5
# ---------------------------------------------------------------------------


def test_parse_bi5_decodes_known_record_count(
    four_eurusd_records: list[SyntheticTick], four_eurusd_bi5: bytes
) -> None:
    """Four-record blob round-trips to four monotonic rows with bid < ask."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    df = parse_bi5(four_eurusd_bi5, hour_ts=hour, symbol="EURUSD")
    assert df.height == len(four_eurusd_records)
    ts = df["ts"].to_list()
    assert ts == sorted(ts), "ts must be monotonic non-decreasing"
    assert (df["bid"] < df["ask"]).all()


def test_parse_bi5_applies_digit_scaling() -> None:
    """EURUSD digits=5: 109510 → 1.09510. XAUUSD digits=3: 2034250 → 2034.250."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)

    eur = [SyntheticTick(0, ask_int=109512, bid_int=109510, ask_vol=1.0, bid_vol=1.0)]
    df_eur = parse_bi5(make_synthetic_bi5(eur), hour_ts=hour, symbol="EURUSD")
    assert df_eur["ask"][0] == pytest.approx(1.09512, abs=1e-9)
    assert df_eur["bid"][0] == pytest.approx(1.09510, abs=1e-9)

    xau = [SyntheticTick(0, ask_int=2034260, bid_int=2034250, ask_vol=1.0, bid_vol=1.0)]
    df_xau = parse_bi5(make_synthetic_bi5(xau), hour_ts=hour, symbol="XAUUSD")
    assert df_xau["ask"][0] == pytest.approx(2034.260, abs=1e-9)
    assert df_xau["bid"][0] == pytest.approx(2034.250, abs=1e-9)


def test_parse_bi5_handles_empty_payload() -> None:
    """An empty (but valid LZMA-compressed) tick stream → empty DataFrame, right schema."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    df = parse_bi5(make_synthetic_bi5([]), hour_ts=hour, symbol="EURUSD")
    assert df.height == 0
    assert df.columns == ["ts", "bid", "ask", "bid_vol", "ask_vol"]


def test_parse_bi5_truncated_record_raises() -> None:
    """A payload whose length is not a multiple of 20 bytes is corrupt."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    # 19-byte raw payload, no compression: bypass via the raw-records path.
    bad = make_synthetic_bi5([])[:0] + struct.pack(">II", 0, 0)[:7]
    compressed_bad = lzma.compress(bad, format=lzma.FORMAT_ALONE)
    with pytest.raises(DukascopyParseError, match="multiple of 20"):
        parse_bi5(compressed_bad, hour_ts=hour, symbol="EURUSD")


def test_parse_bi5_rejects_unknown_symbol() -> None:
    """Symbol not in the digits map should raise so we never silently mis-scale."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    with pytest.raises(DukascopyParseError, match="unknown symbol"):
        parse_bi5(make_synthetic_bi5([]), hour_ts=hour, symbol="DOGEUSD")


def test_parse_bi5_rejects_inverted_bid_ask() -> None:
    """If wire data has bid_int >= ask_int the parser must raise."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    bad = [SyntheticTick(0, ask_int=109500, bid_int=109510, ask_vol=1.0, bid_vol=1.0)]
    with pytest.raises(DukascopyParseError, match="bid"):
        parse_bi5(make_synthetic_bi5(bad), hour_ts=hour, symbol="EURUSD")


# ---------------------------------------------------------------------------
# Hypothesis property test: parser must raise on any inverted-spread payload
# ---------------------------------------------------------------------------


@given(
    bid=st.integers(min_value=1_000, max_value=10_000_000),
    deficit=st.integers(min_value=0, max_value=1_000),
)
def test_bid_lt_ask_invariant(bid: int, deficit: int) -> None:
    """Property: bid_int >= ask_int in *any* record → DukascopyParseError.

    ``deficit`` >= 0 forces ``ask <= bid`` (inverted spread) while keeping
    ``ask`` non-negative so ``struct.pack('>I', ask)`` always succeeds.
    """
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    ask = bid - deficit  # ask <= bid; ask is still a valid uint32.
    payload = [
        SyntheticTick(0, ask_int=bid + 1, bid_int=bid, ask_vol=1.0, bid_vol=1.0),  # ok
        SyntheticTick(100, ask_int=ask, bid_int=bid, ask_vol=1.0, bid_vol=1.0),  # bad
    ]
    with pytest.raises(DukascopyParseError):
        parse_bi5(make_synthetic_bi5(payload), hour_ts=hour, symbol="EURUSD")


# ---------------------------------------------------------------------------
# fetch_ticks (uses stub HttpClient — no network)
# ---------------------------------------------------------------------------


class _StubHttpClient:
    """Returns pre-baked bytes per URL; raises if asked for an unexpected URL."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def fetch_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self._mapping:
            raise AssertionError(f"unexpected URL fetched in unit test: {url}")
        return self._mapping[url]


def test_http_client_protocol_matches_stub() -> None:
    """The stub satisfies the HttpClient Protocol (structural typing check)."""
    stub: HttpClient = _StubHttpClient({})  # type-check at runtime via assignment
    assert hasattr(stub, "fetch_bytes")


def test_fetch_ticks_assembles_hours_in_order() -> None:
    """Two adjacent hours → concatenated DataFrame, monotonic ts across the seam."""
    hour_a = datetime(2024, 1, 2, 10, tzinfo=UTC)
    hour_b = datetime(2024, 1, 2, 11, tzinfo=UTC)
    recs_a = [
        SyntheticTick(0, ask_int=109512, bid_int=109510, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(1_000, ask_int=109514, bid_int=109511, ask_vol=1.0, bid_vol=1.0),
    ]
    recs_b = [
        SyntheticTick(0, ask_int=109520, bid_int=109518, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(2_000, ask_int=109522, bid_int=109519, ask_vol=1.0, bid_vol=1.0),
    ]
    stub = _StubHttpClient(
        {
            _build_url("EURUSD", hour_a): make_synthetic_bi5(recs_a),
            _build_url("EURUSD", hour_b): make_synthetic_bi5(recs_b),
        }
    )
    df = fetch_ticks(
        "EURUSD",
        start_utc=hour_a,
        end_utc=datetime(2024, 1, 2, 12, tzinfo=UTC),
        http_client=stub,
    )
    assert df.height == 4
    ts = df["ts"].to_list()
    assert ts == sorted(ts)
    # Two distinct hours were fetched, both URLs visited exactly once.
    assert len(stub.calls) == 2
    assert set(stub.calls) == {
        _build_url("EURUSD", hour_a),
        _build_url("EURUSD", hour_b),
    }


def test_fetch_ticks_filters_to_window() -> None:
    """Ticks outside [start, end) must be dropped even if the hour file overlaps.

    The hour file contains four ticks at +0ms, +1000ms, +2000ms, +3000ms from
    10:00:00. With start=10:00:00.500 and end=10:00:02.500, the half-open
    window [start, end) admits only the ticks at +1000ms and +2000ms.
    """
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    recs = [
        SyntheticTick(0, ask_int=109512, bid_int=109510, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(1_000, ask_int=109513, bid_int=109511, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(2_000, ask_int=109514, bid_int=109512, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(3_000, ask_int=109515, bid_int=109513, ask_vol=1.0, bid_vol=1.0),
    ]
    stub = _StubHttpClient({_build_url("EURUSD", hour): make_synthetic_bi5(recs)})
    df = fetch_ticks(
        "EURUSD",
        start_utc=datetime(2024, 1, 2, 10, 0, 0, 500_000, tzinfo=UTC),
        end_utc=datetime(2024, 1, 2, 10, 0, 2, 500_000, tzinfo=UTC),
        http_client=stub,
    )
    assert df.height == 2


def test_fetch_ticks_requires_tz_aware_inputs() -> None:
    """fetch_ticks must reject naive datetimes."""
    with pytest.raises(ValueError, match="UTC"):
        fetch_ticks(
            "EURUSD",
            start_utc=datetime(2024, 1, 2, 10),  # naive
            end_utc=datetime(2024, 1, 2, 11, tzinfo=UTC),
            http_client=_StubHttpClient({}),
        )


def test_fetch_ticks_returns_expected_schema() -> None:
    """Schema contract: ts (Datetime, UTC, us), bid/ask float64, vols float64."""
    hour = datetime(2024, 1, 2, 10, tzinfo=UTC)
    stub = _StubHttpClient({_build_url("EURUSD", hour): make_synthetic_bi5([])})
    df = fetch_ticks(
        "EURUSD",
        start_utc=hour,
        end_utc=datetime(2024, 1, 2, 11, tzinfo=UTC),
        http_client=stub,
    )
    assert df.columns == ["ts", "bid", "ask", "bid_vol", "ask_vol"]
    assert df["ts"].dtype == pl.Datetime(time_unit="us", time_zone="UTC")
    assert df["bid"].dtype == pl.Float64
    assert df["ask"].dtype == pl.Float64
    assert df["bid_vol"].dtype == pl.Float64
    assert df["ask_vol"].dtype == pl.Float64


def test_make_synthetic_bi5_round_trips_via_raw_path() -> None:
    """Sanity check on the fixture helper itself."""
    rec = SyntheticTick(0, ask_int=1, bid_int=0, ask_vol=0.0, bid_vol=0.0)
    raw = make_raw_records([rec])
    assert len(raw) == 20  # one record = 20 bytes


# ---------------------------------------------------------------------------
# Integration: live smoke test (skipped by default; opt-in with `-m integration`)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_fetch_smoke() -> None:
    """Fetch one hour of EURUSD ticks on a known-liquid weekday.

    Acceptance: > 50 ticks, monotonic ts, bid < ask everywhere.

    Tuesday 2024-01-02 10:00-11:00 UTC is a London/NY-overlap-adjacent hour
    on the first regular trading day of 2024 - guaranteed liquid for EURUSD.
    """
    df = fetch_ticks(
        "EURUSD",
        start_utc=datetime(2024, 1, 2, 10, tzinfo=UTC),
        end_utc=datetime(2024, 1, 2, 11, tzinfo=UTC),
    )
    assert df.height > 50, f"only {df.height} ticks fetched"
    ts = df["ts"].to_list()
    assert ts == sorted(ts)
    assert (df["bid"] < df["ask"]).all()
