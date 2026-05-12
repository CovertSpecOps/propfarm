"""Offline unit tests for the HistData 1m cross-check downloader.

These tests exercise the parser and the two-step POST-form fetch flow via a
stubbed ``HttpClient`` — no network is touched. The single live smoke test is
gated on ``@pytest.mark.integration`` and skipped by the default pytest
addopts (see pyproject.toml).

Run all offline tests::

    pytest tests/data/test_histdata.py -v

Run the integration smoke (manual, requires network)::

    pytest tests/data/test_histdata.py -m integration -v
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl
import pytest  # type: ignore[import-not-found]

from propfarm.data.vendors import histdata
from propfarm.data.vendors.histdata import HistDataFetchError, fetch_month, parse_csv_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping


# --------------------------------------------------------------------------- #
# Helpers — synthetic CSV + ZIP builders. No fixtures on disk.
# --------------------------------------------------------------------------- #
SAMPLE_CSV = (
    b"20240115 093000;1.09010;1.09020;1.09005;1.09015;0\n"
    b"20240115 093100;1.09015;1.09030;1.09010;1.09025;0\n"
    b"20240115 093200;1.09025;1.09035;1.09020;1.09030;0\n"
    b"20240115 093300;1.09030;1.09040;1.09025;1.09035;0\n"
    b"20240115 093400;1.09035;1.09045;1.09030;1.09040;0\n"
)


def _build_zip(csv_bytes: bytes, csv_name: str = "DAT_ASCII_EURUSD_M1_202401.csv") -> bytes:
    """Produce an in-memory ZIP containing a single CSV file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(csv_name, csv_bytes)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Stub HttpClient — satisfies the Protocol in propfarm.data.vendors.histdata.
# Records calls so tests can assert the two-step flow happens correctly.
# --------------------------------------------------------------------------- #
@dataclass
class StubHttpClient:
    """Test double for ``histdata.HttpClient``.

    Returns ``listing_html`` for GETs and ``zip_bytes`` for POSTs. Records
    every call to allow assertions on URL / method / form data.
    """

    listing_html: bytes = b""
    zip_bytes: bytes = b""
    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, Mapping[str, str]]] = field(default_factory=list)

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> bytes:
        self.gets.append(url)
        return self.listing_html

    def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        self.posts.append((url, dict(data)))
        return self.zip_bytes


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_parse_csv_decodes_known_rows() -> None:
    """Five-row CSV parses to a DataFrame with monotonic ts and OHLC consistency."""
    df = parse_csv_bytes(SAMPLE_CSV)

    assert df.height == 5
    assert df.columns == ["ts", "open", "high", "low", "close", "volume"]

    # ts is a UTC-aware Datetime column.
    ts_dtype = df.schema["ts"]
    assert isinstance(ts_dtype, pl.Datetime)
    assert ts_dtype.time_zone == "UTC"

    # Numeric columns are float64.
    for col in ("open", "high", "low", "close", "volume"):
        assert df.schema[col] == pl.Float64, (col, df.schema[col])

    # Monotonic timestamps.
    ts_list = df.get_column("ts").to_list()
    assert all(ts_list[i] < ts_list[i + 1] for i in range(len(ts_list) - 1))

    # OHLC consistency: low <= open,close <= high; low <= high.
    rows = df.to_dicts()
    for r in rows:
        assert r["low"] <= r["open"] <= r["high"], r
        assert r["low"] <= r["close"] <= r["high"], r
        assert r["low"] <= r["high"], r


def test_est_to_utc_conversion() -> None:
    """``20240115 093000`` EST (UTC-5, no DST) -> ``2024-01-15T14:30:00Z``."""
    import datetime as dt

    df = parse_csv_bytes(SAMPLE_CSV)
    first_ts = df.get_column("ts").to_list()[0]
    expected = dt.datetime(2024, 1, 15, 14, 30, 0, tzinfo=dt.UTC)
    assert first_ts == expected, f"got {first_ts!r}, expected {expected!r}"


def test_est_to_utc_no_dst_in_summer() -> None:
    """HistData FX files are fixed UTC-5 (no DST). A July row at 09:30 must
    still parse as 14:30 UTC — NOT 13:30 (which would imply EDT=UTC-4)."""
    import datetime as dt

    summer_csv = b"20240715 093000;1.09010;1.09020;1.09005;1.09015;0\n"
    df = parse_csv_bytes(summer_csv)
    ts = df.get_column("ts").to_list()[0]
    # If DST were applied, this would be 13:30:00; we assert 14:30:00.
    assert ts == dt.datetime(2024, 7, 15, 14, 30, 0, tzinfo=dt.UTC)


def test_volume_zero_tolerated() -> None:
    """Rows with volume=0 (HistData's typical FX volume) must parse cleanly."""
    df = parse_csv_bytes(SAMPLE_CSV)
    vols = df.get_column("volume").to_list()
    assert vols == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_parse_csv_semicolon_delimiter_not_comma() -> None:
    """Regression guard: a comma-delimited input must NOT parse as if it were
    HistData (which uses semicolons). HistData's CSV is semicolon-delimited
    despite the ``.csv`` extension."""
    comma_csv = b"20240115 093000,1.09010,1.09020,1.09005,1.09015,0\n"
    with pytest.raises(HistDataFetchError):
        parse_csv_bytes(comma_csv)


def test_parse_empty_csv_raises() -> None:
    with pytest.raises(HistDataFetchError):
        parse_csv_bytes(b"")


# --------------------------------------------------------------------------- #
# Fetch flow tests
# --------------------------------------------------------------------------- #
def test_fetch_month_assembles_zip() -> None:
    """``fetch_month`` performs the GET-then-POST dance, unzips, and parses."""
    listing_html = (
        b'<html><body><form id="file_down">'
        b'<input type="hidden" id="tk" name="tk" value="abc123xyz">'
        b'<input type="hidden" id="date" name="date" value="202401">'
        b"</form></body></html>"
    )
    stub = StubHttpClient(
        listing_html=listing_html,
        zip_bytes=_build_zip(SAMPLE_CSV),
    )

    df = fetch_month("EURUSD", 2024, 1, http_client=stub)

    assert df.height == 5
    assert df.columns == ["ts", "open", "high", "low", "close", "volume"]
    # GET-then-POST happened.
    assert len(stub.gets) == 1
    assert len(stub.posts) == 1
    # The token scraped from the listing was forwarded in the POST body.
    _post_url, post_body = stub.posts[0]
    assert post_body.get("tk") == "abc123xyz"


def test_fetch_month_missing_token_raises() -> None:
    """If the listing HTML has no scrapeable ``tk`` token, fetch_month raises."""
    stub = StubHttpClient(
        listing_html=b"<html><body>no token here</body></html>",
        zip_bytes=b"",
    )
    with pytest.raises(HistDataFetchError):
        fetch_month("EURUSD", 2024, 1, http_client=stub)


def test_fetch_month_rejects_invalid_zip() -> None:
    """A non-ZIP body from the download endpoint must raise, not silently parse."""
    listing_html = (
        b'<html><form id="file_down">'
        b'<input type="hidden" id="tk" name="tk" value="t">'
        b"</form></html>"
    )
    stub = StubHttpClient(listing_html=listing_html, zip_bytes=b"NOT A ZIP")
    with pytest.raises(HistDataFetchError):
        fetch_month("EURUSD", 2024, 1, http_client=stub)


def test_fetch_month_normalizes_symbol_case() -> None:
    """Symbol comparison should be case-insensitive — ``eurusd`` works too."""
    listing_html = (
        b'<html><form id="file_down">'
        b'<input type="hidden" id="tk" name="tk" value="t">'
        b"</form></html>"
    )
    stub = StubHttpClient(
        listing_html=listing_html,
        zip_bytes=_build_zip(SAMPLE_CSV),
    )
    df = fetch_month("eurusd", 2024, 1, http_client=stub)
    assert df.height == 5


def test_fetch_month_rejects_bad_month() -> None:
    """Month must be in 1..12."""
    stub = StubHttpClient()
    with pytest.raises(ValueError):
        fetch_month("EURUSD", 2024, 0, http_client=stub)
    with pytest.raises(ValueError):
        fetch_month("EURUSD", 2024, 13, http_client=stub)


# --------------------------------------------------------------------------- #
# HttpClient Protocol — duck-typing smoke. Confirms the Protocol accepts a
# minimal implementation without inheriting from it (structural typing).
# --------------------------------------------------------------------------- #
def test_http_client_protocol_is_structural() -> None:
    """``StubHttpClient`` does not inherit from ``HttpClient`` but must satisfy it."""
    stub: histdata.HttpClient = StubHttpClient()  # type-check only
    assert stub is not None


# --------------------------------------------------------------------------- #
# Live smoke — skipped by default. Run manually with `pytest -m integration`.
# --------------------------------------------------------------------------- #
@pytest.mark.integration  # type: ignore[untyped-decorator]
def test_live_fetch_smoke() -> None:
    """Fetch EURUSD Jan 2024 from HistData. Asserts > 20000 rows.

    Run only when you actually want to hit the live service:
        pytest tests/data/test_histdata.py -m integration -v
    """
    df = fetch_month("EURUSD", 2024, 1)
    assert df.height > 20000, f"got only {df.height} rows; expected > 20000 for Jan 2024"
    # Schema spot-check.
    assert df.columns == ["ts", "open", "high", "low", "close", "volume"]
