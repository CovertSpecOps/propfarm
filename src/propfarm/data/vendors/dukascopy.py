"""Dukascopy historical tick downloader.

Dukascopy publishes free historical tick data via a public HTTP datafeed.
Each hour of each instrument is a separate LZMA-compressed file:

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5

with two important quirks:

1. **Months are 0-indexed in the URL.** January = ``00``, December = ``11``.
2. The body is **LZMA-compressed with the legacy "alone" (LZMA1) framing**,
   not xz. Decompress with ``lzma.decompress(data, format=lzma.FORMAT_ALONE)``.
   The decompressed payload is a concatenation of fixed-size 20-byte records
   packed big-endian as ``>IIIff``:

   ====  ===========  =================================================
   off   field        meaning
   ====  ===========  =================================================
   0     ms_from_hour milliseconds since the *start* of the hour (uint32)
   4     ask_int      ask price as an integer (uint32); scale by 10**digits
   8     bid_int      bid price as an integer (uint32); scale by 10**digits
   12    ask_vol      ask volume in millions of units (float32)
   16    bid_vol      bid volume in millions of units (float32)
   ====  ===========  =================================================

Price scaling depends on the instrument. The map below covers the six symbols
Phase-0 targets. Values are confirmed against Dukascopy's instrument table
(point-size column on https://www.dukascopy.com — FX 5dp majors, USDJPY 3dp,
XAUUSD 3dp, equity-index CFDs 2dp). If a future symbol needs different
scaling, add it explicitly to ``_DIGITS`` — unknown symbols raise.

Public API:

* :func:`fetch_ticks` — fetch a [start, end) UTC range for one symbol and
  return a polars DataFrame with monotonic ``ts`` (microsecond precision,
  ``time_zone="UTC"``), ``bid``, ``ask``, ``bid_vol``, ``ask_vol``.
* :func:`parse_bi5` — decode a single hour's raw ``.bi5`` bytes.
* :func:`fetch_hour_bi5` — fetch one hour's raw bytes for a symbol.
* :class:`HttpClient` — Protocol for the byte-fetching dependency. Tests
  inject a stub; the default :class:`UrllibHttpClient` uses ``urllib.request``.

Unit tests in :mod:`tests.data.test_dukascopy` are 100% offline (synthetic
``.bi5`` payloads generated in-process). A single ``@pytest.mark.integration``
test fetches a real hour and is skipped by default.
"""

from __future__ import annotations

import lzma
import struct
import urllib.request
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

import polars as pl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATAFEED_URL_TEMPLATE: Final[str] = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/{year:04d}/{month:02d}/"
    "{day:02d}/{hour:02d}h_ticks.bi5"
)

#: Decimal digits used to scale each symbol's integer-encoded price.
#: FX majors: 5 (1 point = 0.00001). USDJPY: 3 (1 point = 0.001).
#: XAUUSD: 3 (1 point = 0.001 USD). Equity-index CFDs (GER40, US100): 2.
#: Source: Dukascopy instrument specifications (point-size column).
_DIGITS: Final[dict[str, int]] = {
    "EURUSD": 5,
    "GBPUSD": 5,
    "USDJPY": 3,
    "XAUUSD": 3,
    "GER40": 2,
    "US100": 2,
}

_RECORD_STRUCT: Final[struct.Struct] = struct.Struct(">IIIff")
_RECORD_SIZE: Final[int] = _RECORD_STRUCT.size  # == 20
assert _RECORD_SIZE == 20, "Dukascopy tick records are always 20 bytes."

_SCHEMA: Final[list[str]] = ["ts", "bid", "ask", "bid_vol", "ask_vol"]

# A timeout on the default HTTP client — bridge tests get no chance to hang CI.
_DEFAULT_HTTP_TIMEOUT_S: Final[float] = 30.0

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DukascopyError(Exception):
    """Base error for all Dukascopy module faults."""


class DukascopyParseError(DukascopyError):
    """Raised when a ``.bi5`` payload cannot be decoded into valid ticks."""


# ---------------------------------------------------------------------------
# HTTP client abstraction (Protocol so tests can inject a stub)
# ---------------------------------------------------------------------------


@runtime_checkable
class HttpClient(Protocol):
    """Anything that can fetch raw bytes from a URL.

    Production uses :class:`UrllibHttpClient`; tests pass a stub returning
    in-memory fixture bytes. Keeping this surface tiny (one method) makes
    swapping in ``httpx``/``requests``/an async client later trivial.
    """

    def fetch_bytes(self, url: str) -> bytes: ...  # pragma: no cover - protocol


class UrllibHttpClient:
    """Default :class:`HttpClient` backed by the stdlib ``urllib.request``.

    Uses a fixed timeout (``_DEFAULT_HTTP_TIMEOUT_S``) so a misbehaving
    upstream cannot wedge a long-running download script.
    """

    def __init__(self, timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    def fetch_bytes(self, url: str) -> bytes:
        # urllib.request opens a network connection; this is the *only* code
        # path in this module that does I/O. Unit tests must never reach here.
        with urllib.request.urlopen(url, timeout=self._timeout_s) as resp:
            data: bytes = resp.read()
        return data


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------


def _require_utc(ts: datetime, *, name: str) -> None:
    """Validate that ``ts`` is timezone-aware UTC."""
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime; got tzinfo={ts.tzinfo!r}")


def _build_url(symbol: str, ts_hour: datetime) -> str:
    """Return the Dukascopy datafeed URL for ``symbol`` at the given UTC hour.

    Note the **0-indexed month** quirk: Jan→00, Feb→01, …, Dec→11.
    """
    _require_utc(ts_hour, name="ts_hour")
    return DATAFEED_URL_TEMPLATE.format(
        symbol=symbol,
        year=ts_hour.year,
        month=ts_hour.month - 1,  # Dukascopy is 0-indexed (Jan = 00).
        day=ts_hour.day,
        hour=ts_hour.hour,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decompress_bi5(raw: bytes) -> bytes:
    """LZMA-decompress a raw ``.bi5`` blob using the legacy ALONE format."""
    try:
        return lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as exc:
        raise DukascopyParseError(f"failed to LZMA-decompress .bi5 payload: {exc}") from exc


def parse_bi5(raw: bytes, hour_ts: datetime, symbol: str) -> pl.DataFrame:
    """Decode one hour's compressed Dukascopy tick file.

    Parameters
    ----------
    raw:
        Bytes as returned by :func:`fetch_hour_bi5` (LZMA-ALONE compressed).
    hour_ts:
        The UTC datetime corresponding to the start of this hour. Must be
        timezone-aware UTC.
    symbol:
        The Dukascopy instrument code (e.g. ``"EURUSD"``). Must appear in
        :data:`_DIGITS`; unknown symbols raise :class:`DukascopyParseError`.

    Returns
    -------
    polars.DataFrame
        Columns ``ts`` (Datetime[us, UTC]), ``bid``, ``ask``, ``bid_vol``,
        ``ask_vol`` (all Float64). Empty payloads return an empty DataFrame
        with the correct schema.

    Raises
    ------
    DukascopyParseError
        If LZMA decompression fails, the payload is not a multiple of 20
        bytes, the symbol is not in the digits map, or any tick has
        ``bid_int >= ask_int`` (inverted spread → upstream corruption).
    """
    _require_utc(hour_ts, name="hour_ts")
    if symbol not in _DIGITS:
        raise DukascopyParseError(
            f"unknown symbol {symbol!r}; add it to dukascopy._DIGITS with its point-size"
        )

    payload = _decompress_bi5(raw)
    if len(payload) % _RECORD_SIZE != 0:
        raise DukascopyParseError(
            f"decompressed payload length {len(payload)} is not a multiple of 20 bytes"
        )

    digits = _DIGITS[symbol]
    scale = float(10**digits)

    if not payload:
        return _empty_frame()

    n_records = len(payload) // _RECORD_SIZE
    ms_arr: list[int] = [0] * n_records
    bid_arr: list[float] = [0.0] * n_records
    ask_arr: list[float] = [0.0] * n_records
    bid_vol_arr: list[float] = [0.0] * n_records
    ask_vol_arr: list[float] = [0.0] * n_records

    for i, (ms, ask_int, bid_int, ask_vol, bid_vol) in enumerate(
        _RECORD_STRUCT.iter_unpack(payload)
    ):
        if bid_int >= ask_int:
            raise DukascopyParseError(
                f"record {i}: bid_int={bid_int} >= ask_int={ask_int}; "
                "inverted spread indicates corrupt upstream data"
            )
        ms_arr[i] = ms
        ask_arr[i] = ask_int / scale
        bid_arr[i] = bid_int / scale
        ask_vol_arr[i] = float(ask_vol)
        bid_vol_arr[i] = float(bid_vol)

    # Build ts column: hour_ts + ms_from_hour milliseconds, microsecond precision.
    df = pl.DataFrame(
        {
            "ms": pl.Series(ms_arr, dtype=pl.Int64),
            "bid": pl.Series(bid_arr, dtype=pl.Float64),
            "ask": pl.Series(ask_arr, dtype=pl.Float64),
            "bid_vol": pl.Series(bid_vol_arr, dtype=pl.Float64),
            "ask_vol": pl.Series(ask_vol_arr, dtype=pl.Float64),
        }
    )
    df = df.with_columns(
        ts=pl.lit(hour_ts).cast(pl.Datetime(time_unit="us", time_zone="UTC"))
        + pl.duration(milliseconds=pl.col("ms")),
    ).drop("ms")
    return df.select(_SCHEMA)


def _empty_frame() -> pl.DataFrame:
    """Return an empty DataFrame with the canonical Dukascopy tick schema."""
    return pl.DataFrame(
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
            "bid_vol": pl.Float64,
            "ask_vol": pl.Float64,
        }
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_hour_bi5(
    symbol: str, ts_hour: datetime, *, http_client: HttpClient | None = None
) -> bytes:
    """Fetch one hour of raw ``.bi5`` bytes from the Dukascopy datafeed."""
    client = http_client or UrllibHttpClient()
    return client.fetch_bytes(_build_url(symbol, ts_hour))


def _iter_hours(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Return every hour-boundary UTC timestamp h s.t. [h, h+1h) overlaps [start, end)."""
    if end_utc <= start_utc:
        return []
    first = start_utc.replace(minute=0, second=0, microsecond=0)
    hours: list[datetime] = []
    cursor = first
    while cursor < end_utc:
        hours.append(cursor)
        cursor = cursor + timedelta(hours=1)
    return hours


def fetch_ticks(
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    http_client: HttpClient | None = None,
) -> pl.DataFrame:
    """Fetch and parse Dukascopy ticks for ``symbol`` in ``[start_utc, end_utc)``.

    Parameters
    ----------
    symbol:
        Instrument code (must appear in :data:`_DIGITS`).
    start_utc, end_utc:
        Half-open UTC window. Both must be timezone-aware UTC datetimes
        (``tzinfo=timezone.utc``); naive datetimes raise.
    http_client:
        Optional :class:`HttpClient`. Defaults to :class:`UrllibHttpClient`.
        Tests inject a stub returning in-memory fixture bytes.

    Returns
    -------
    polars.DataFrame
        Columns ``ts`` (Datetime[us, UTC], monotonic non-decreasing), ``bid``,
        ``ask``, ``bid_vol``, ``ask_vol``. ``bid < ask`` is enforced per-tick
        by :func:`parse_bi5`.
    """
    _require_utc(start_utc, name="start_utc")
    _require_utc(end_utc, name="end_utc")

    hours = _iter_hours(start_utc, end_utc)
    if not hours:
        return _empty_frame()

    client = http_client or UrllibHttpClient()
    frames: list[pl.DataFrame] = []
    for hour_ts in hours:
        raw = client.fetch_bytes(_build_url(symbol, hour_ts))
        frames.append(parse_bi5(raw, hour_ts=hour_ts, symbol=symbol))

    combined = pl.concat(frames, how="vertical") if frames else _empty_frame()
    # Half-open [start_utc, end_utc) — drop any ticks outside the window. The
    # parser may emit rows from the surrounding hour file that fall before
    # start_utc or at/after end_utc.
    filtered = combined.filter((pl.col("ts") >= start_utc) & (pl.col("ts") < end_utc)).sort("ts")
    return filtered.select(_SCHEMA)
