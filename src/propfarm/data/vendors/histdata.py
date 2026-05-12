"""HistData.com 1-minute OHLC cross-check downloader.

HistData publishes free 1-minute FX bars by month as ASCII ZIPs distributed via
a two-step POST form: a listing page that embeds a hidden token (``tk``) in a
form, and a download endpoint that returns the ZIP only when posted the
matching token. Inside the ZIP is a single semicolon-delimited CSV with rows
of the form::

    YYYYMMDD HHMMSS;open;high;low;close;volume

This module is a **cross-check** source for Phase-0 Day-5 reconciliation
against Dukascopy (the primary feed), NOT the primary historical pipeline. Use
``scripts/download_histdata.py`` for the bulk monthly pull (Task 3.3).

Timezone (single point of trust):
    HistData ASCII FX files are published in **EST (UTC-5, no DST)**.
    The parser converts to UTC by adding 5 hours. If HistData ever switches to
    EDT or to local exchange time, ``_EST_TO_UTC_OFFSET`` below is the only
    line that needs to change. The summer-month regression test in
    ``tests/data/test_histdata.py`` guards against accidental DST conversion.

Legal / etiquette note:
    HistData allows free download for **personal research only**. This module
    performs the same two-step POST form that the website's "Download" button
    performs — it is not a scraper of intermediate site content. Do NOT
    redistribute downloaded data, and do not invoke ``fetch_month`` in tight
    loops; for any bulk pull, prefer ``scripts/download_histdata.py`` which
    inserts polite sleeps between requests.

HTTP-client abstraction:
    The module accepts any object implementing the ``HttpClient`` Protocol
    (``get(url) -> bytes`` and ``post(url, data) -> bytes``). This keeps the
    unit tests offline (stubs in ``tests/data/test_histdata.py``) and lets the
    bulk script swap in a polite, throttled client without touching this
    file. The Protocol is defined locally; if Task 3.1's ``dukascopy`` module
    defines an identical Protocol, a later refactor (W1 reviewer or Phase-1
    refactor) can hoist a shared ``propfarm.data.http`` module — duplicating
    the Protocol for now is an explicit choice to avoid premature coupling
    across two parallel agents.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import urllib.parse
import urllib.request
import zipfile
from typing import TYPE_CHECKING, Protocol

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "HistDataFetchError",
    "HttpClient",
    "UrllibHttpClient",
    "fetch_month",
    "parse_csv_bytes",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# EST is fixed UTC-5 year-round (no DST). HistData publishes ASCII FX files in
# EST regardless of season. The summer regression test asserts this; do NOT
# replace with ``ZoneInfo("America/New_York")`` without first verifying with
# fresh HistData downloads that they have switched to DST-aware timestamps.
_EST_TO_UTC_OFFSET = dt.timedelta(hours=5)

# HistData endpoints. Kept as constants so a future change of host (or a mirror)
# only needs touching here.
_HISTDATA_BASE = "https://www.histdata.com"
_LISTING_URL_TEMPLATE = (
    f"{_HISTDATA_BASE}/download-free-forex-historical-data/?/ascii/"
    "1-minute-bar-quotes/{symbol}/{year}/{month}"
)
_DOWNLOAD_URL = f"{_HISTDATA_BASE}/get.php"

# Browser-like UA — HistData's CDN sometimes rejects bare Python UAs. This is
# the identity their own "Download" button uses (modulo browser version).
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex hunts the hidden ``tk`` input in the listing page's form. The page
# layout is stable enough for cross-check use; the unit tests exercise the
# happy path and a missing-token failure mode.
_TK_RE = re.compile(
    rb'<input[^>]*\bid\s*=\s*"tk"[^>]*\bvalue\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class HistDataFetchError(RuntimeError):
    """Raised when HistData fetch/parse fails in a way the caller should see.

    Wraps three distinct failure modes that all share a common recovery path
    (retry / fall back to Dukascopy):
      1. Listing page returned no scrapeable ``tk`` token.
      2. Download endpoint returned a body that is not a ZIP.
      3. CSV inside the ZIP was empty or had an unexpected delimiter.
    """


# --------------------------------------------------------------------------- #
# HTTP client Protocol (structural typing — implementations need not subclass)
# --------------------------------------------------------------------------- #
class HttpClient(Protocol):
    """Minimal HTTP surface this module needs.

    Implementations must support a GET-returning-bytes and a
    POST-form-returning-bytes. ``urllib`` and ``requests`` both fit trivially.
    The Protocol is intentionally tiny so a test stub can implement it in
    ~10 lines (see ``tests/data/test_histdata.py::StubHttpClient``).
    """

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> bytes:
        """GET ``url`` and return raw response bytes. Should raise on HTTP errors."""
        ...

    def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """POST ``data`` (form-urlencoded) to ``url`` and return raw response bytes."""
        ...


class UrllibHttpClient:
    """Default ``HttpClient`` implementation backed by ``urllib.request``.

    Stdlib-only, no external dependency added. The bulk downloader script
    (Task 3.3) can swap in a throttled, retry-aware client without touching
    this module.
    """

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> bytes:
        req = urllib.request.Request(url, headers=dict(headers or _DEFAULT_HEADERS), method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            body: bytes = resp.read()
            return body

    def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        body = urllib.parse.urlencode(dict(data)).encode("ascii")
        merged_headers = dict(headers or _DEFAULT_HEADERS)
        merged_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        # Referer is required by HistData's download endpoint (basic anti-hotlink).
        merged_headers.setdefault("Referer", _HISTDATA_BASE + "/")
        req = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            out: bytes = resp.read()
            return out


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def parse_csv_bytes(csv_bytes: bytes) -> pl.DataFrame:
    """Parse a HistData M1 CSV body into a tidy Polars DataFrame.

    Args:
        csv_bytes: raw CSV bytes (semicolon-delimited, no header).

    Returns:
        DataFrame with columns ``ts`` (UTC datetime, minute precision),
        ``open``, ``high``, ``low``, ``close``, ``volume`` (all ``Float64``).

    Raises:
        HistDataFetchError: if the body is empty, has the wrong delimiter,
        or fails to parse into the expected 6-column shape.
    """
    if not csv_bytes:
        raise HistDataFetchError("empty CSV body")

    # Guard against the easy delimiter mistake. HistData uses ';' not ','.
    # If the first line has no ';' but has ',', this is the wrong format.
    first_line = csv_bytes.split(b"\n", 1)[0]
    if b";" not in first_line:
        raise HistDataFetchError(
            "expected semicolon-delimited CSV (HistData M1 ASCII); "
            "got a body with no ';' in the first line. "
            "If the upstream format ever changes to comma-delimited, "
            "update parse_csv_bytes."
        )

    # Polars' read_csv with explicit dtypes + EST timestamp string parse.
    try:
        raw = pl.read_csv(
            io.BytesIO(csv_bytes),
            separator=";",
            has_header=False,
            new_columns=["dt_str", "open", "high", "low", "close", "volume"],
            schema_overrides={
                "dt_str": pl.String,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            },
        )
    except Exception as exc:  # polars wraps Rust errors in a base Exception
        raise HistDataFetchError(f"failed to parse HistData CSV: {exc}") from exc

    if raw.height == 0:
        raise HistDataFetchError("CSV parsed to zero rows")

    # ``YYYYMMDD HHMMSS`` -> naive datetime -> +5h -> UTC-aware.
    # We do the timezone shift in Python via offset arithmetic so the
    # no-DST invariant is encoded in code, not in a tz database lookup.
    df = raw.with_columns(
        pl.col("dt_str")
        .str.strptime(pl.Datetime("us"), format="%Y%m%d %H%M%S", strict=True)
        .dt.offset_by(f"{int(_EST_TO_UTC_OFFSET.total_seconds())}s")
        .dt.replace_time_zone("UTC")
        .alias("ts"),
    ).select(["ts", "open", "high", "low", "close", "volume"])

    return df


# --------------------------------------------------------------------------- #
# Two-step fetch flow
# --------------------------------------------------------------------------- #
def _extract_tk(html: bytes) -> str:
    """Pull the hidden ``tk`` token from a HistData listing page."""
    m = _TK_RE.search(html)
    if not m:
        raise HistDataFetchError(
            "could not locate hidden 'tk' token in HistData listing page; "
            "either the page format changed or the symbol/month does not exist."
        )
    return m.group(1).decode("ascii")


def _validate_args(symbol: str, year: int, month: int) -> tuple[str, int, int]:
    if not 1 <= month <= 12:
        raise ValueError(f"month must be in 1..12, got {month}")
    if not 2000 <= year <= 2100:
        # Soft sanity range; HistData history starts circa 2000.
        raise ValueError(f"year out of plausible range: {year}")
    if not symbol or not symbol.isalnum():
        raise ValueError(f"symbol must be alphanumeric, got {symbol!r}")
    return symbol.upper(), year, month


def fetch_month(
    symbol: str,
    year: int,
    month: int,
    *,
    http_client: HttpClient | None = None,
) -> pl.DataFrame:
    """Fetch one month of 1-minute OHLC bars for ``symbol`` from HistData.

    This is a cross-check fetch — small volume, one month at a time. For bulk
    historical pulls use ``scripts/download_histdata.py`` (Task 3.3).

    Args:
        symbol: FX or commodity ticker, e.g. ``"EURUSD"`` (case-insensitive).
        year: 4-digit year, e.g. ``2024``.
        month: 1..12.
        http_client: optional ``HttpClient`` for testing / throttled access.
            Defaults to a fresh ``UrllibHttpClient``.

    Returns:
        Polars DataFrame with the schema described in ``parse_csv_bytes``.

    Raises:
        HistDataFetchError: on any failure of the two-step fetch / unzip /
            parse pipeline.
        ValueError: on out-of-range arguments.
    """
    symbol_norm, year_n, month_n = _validate_args(symbol, year, month)
    client = http_client if http_client is not None else UrllibHttpClient()

    listing_url = _LISTING_URL_TEMPLATE.format(
        symbol=symbol_norm.lower(),
        year=year_n,
        month=month_n,
    )
    listing_html = client.get(listing_url)
    tk = _extract_tk(listing_html)

    # HistData's POST form fields. The ``date`` field is the YYYYMM the
    # download endpoint uses to look up the file matching ``tk``. The other
    # fields mirror the form inputs the real "Download" button sends.
    form = {
        "tk": tk,
        "date": f"{year_n}{month_n:02d}",
        "datemonth": f"{year_n}{month_n:02d}",
        "platform": "ASCII",
        "timeframe": "M1",
        "fxpair": symbol_norm,
    }
    zip_body = client.post(_DOWNLOAD_URL, form)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_body))
    except zipfile.BadZipFile as exc:
        raise HistDataFetchError(
            "HistData download endpoint did not return a valid ZIP "
            f"(first bytes: {zip_body[:32]!r})"
        ) from exc

    # The archive contains one CSV (and sometimes a small license/readme).
    csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not csv_names:
        raise HistDataFetchError(
            f"no .csv inside HistData ZIP for {symbol_norm} {year_n}-{month_n:02d}; "
            f"archive contents: {zf.namelist()!r}"
        )
    csv_bytes = zf.read(csv_names[0])
    return parse_csv_bytes(csv_bytes)
