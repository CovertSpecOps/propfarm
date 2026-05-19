"""Bulk-fetch raw Dukascopy ``.bi5`` files into the on-disk cache layout
that :mod:`propfarm.data.ingest` already consumes (Task 3.3 — the last
Phase-0 code gap).

Cache layout written (relative to ``--raw-root``)::

    {SYMBOL}/{YYYY}/{MM_zero_indexed:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

This is *the same* path shape :func:`propfarm.data.ingest._hour_ts_from_path`
parses back, including Dukascopy's 0-indexed-month quirk
(January = ``00``, December = ``11``).

Empty hours (weekends, holidays, pre-2010 sparse hours) come back as a
zero-byte body from Dukascopy. We persist those as **0-byte files** so the
resume scan can distinguish "checked, legitimately empty" (skip on next
run) from "fetch failed" (retry on next run). :mod:`propfarm.data.ingest`
already treats 0-byte ``.bi5`` files as "skipped with warning".

Usage
-----
For the Phase-1 EDA scope (EURUSD + GBPUSD 2015-2025)::

    python scripts/bulk_fetch_dukascopy.py \\
        --symbol EURUSD --symbol GBPUSD \\
        --year-min 2015 --year-max 2025

Estimated ~25-35 GB on disk, 3-8h wall-clock at default ``--sleep-ms 100``.

Resume support: re-run the same command to continue an interrupted fetch;
the script scans ``data/raw/dukascopy/`` for existing ``.bi5`` files and only
fetches the missing hours.

Chained workflow (see ``docs/runbooks/bulk-fetch-dukascopy.md``)::

    scripts/bulk_fetch_dukascopy.py    # this script — pulls raw .bi5
    scripts/ingest_to_snapshot.py      # parses .bi5 → content-hashed parquet
                                       # Phase-1 EDA notebooks consume the parquet
"""

from __future__ import annotations

import logging
import re
import sys
import time
from calendar import monthrange
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import typer

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.data.vendors.dukascopy import (
    DukascopyError,
    HttpClient,
    UrllibHttpClient,
    fetch_hour_bi5,
)

# --------------------------------------------------------------------------- #
# Logging setup
# --------------------------------------------------------------------------- #
logger = logging.getLogger("propfarm.scripts.bulk_fetch_dukascopy")


def _configure_logging() -> None:
    """Set up a single stdout handler with the runbook-mandated line format."""
    if logger.handlers:  # pragma: no cover — guard against repeated configure
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Same filename pattern as ``propfarm.data.ingest._HOUR_FILENAME_RE`` — kept in
#: sync by reference (we re-declare locally to avoid coupling to a private name).
_HOUR_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<hour>\d{2})h_ticks\.bi5$")

#: Default empirical Dukascopy hour-fetch latency for the ETA formula. The
#: actual RTT varies (~0.15s for cached, ~0.6s on heavy load); 0.3s is the
#: middle of the band based on the Task 3.1 download spike.
_AVG_FETCH_LATENCY_S: Final[float] = 0.3

#: Acceptable bounds for ``--sleep-ms`` (per spec).
_MIN_SLEEP_MS: Final[int] = 50
_MAX_SLEEP_MS: Final[int] = 500


# --------------------------------------------------------------------------- #
# Module-level typer Option singletons (ruff B008 / repo convention)
# --------------------------------------------------------------------------- #
_SYMBOL_OPT = typer.Option(
    None,
    "--symbol",
    help=(
        "Symbol to fetch. Repeat for multiple symbols. "
        f"Must be in SUPPORTED_SYMBOLS: {', '.join(SUPPORTED_SYMBOLS)}."
    ),
)
_YEAR_MIN_OPT = typer.Option(2015, "--year-min", help="Inclusive lower year bound. Default 2015.")
_YEAR_MAX_OPT = typer.Option(
    None,
    "--year-max",
    help="Inclusive upper year bound. Default: current UTC year.",
)
_RAW_ROOT_OPT = typer.Option(
    Path("data/raw/dukascopy"),
    "--raw-root",
    help="Root of the Dukascopy raw cache. Created if absent.",
)
_SLEEP_MS_OPT = typer.Option(
    100,
    "--sleep-ms",
    help=f"Per-request sleep in ms. Range [{_MIN_SLEEP_MS}, {_MAX_SLEEP_MS}].",
)
_MAX_RETRIES_OPT = typer.Option(
    3, "--max-retries", help="Per-hour retry budget on fetch errors. Default 3."
)
_DRY_RUN_OPT = typer.Option(
    False, "--dry-run", help="Print the ETA estimate and exit without fetching."
)


# --------------------------------------------------------------------------- #
# Path / time helpers
# --------------------------------------------------------------------------- #
def _hour_path(raw_root: Path, symbol: str, ts_hour: datetime) -> Path:
    """Return the on-disk cache path for one hour of one symbol.

    Mirrors :func:`propfarm.data.ingest._hour_ts_from_path` exactly — note the
    0-indexed month (Dukascopy quirk: January = ``00``, December = ``11``).
    """
    month_zero = ts_hour.month - 1
    return (
        raw_root
        / symbol
        / f"{ts_hour.year:04d}"
        / f"{month_zero:02d}"
        / f"{ts_hour.day:02d}"
        / f"{ts_hour.hour:02d}h_ticks.bi5"
    )


def _scan_existing(raw_root: Path, symbol: str) -> set[tuple[str, int, int, int, int]]:
    """Return tuples ``(symbol, year, month_zero, day, hour)`` already on disk.

    Single ``rglob("*.bi5")`` walk per symbol. The skip-check is O(1) per
    candidate hour after the walk finishes — critical for runs at the
    ~96000-file scale (6 symbols x 11 years x 24 x 365 ~= 578k hours but
    only weekday hours land non-empty, ~96k typical for two-symbol runs).
    """
    found: set[tuple[str, int, int, int, int]] = set()
    symbol_dir = raw_root / symbol
    if not symbol_dir.is_dir():
        return found
    for bi5 in symbol_dir.rglob("*.bi5"):
        try:
            rel = bi5.relative_to(symbol_dir)
        except ValueError:  # pragma: no cover — rglob can't escape symbol_dir
            continue
        parts = rel.parts
        if len(parts) != 4:
            continue
        year_s, month_s, day_s, fname = parts
        m = _HOUR_FILENAME_RE.match(fname)
        if m is None:
            continue
        try:
            year = int(year_s)
            month_zero = int(month_s)
            day = int(day_s)
            hour = int(m.group("hour"))
        except ValueError:
            continue
        if not (0 <= month_zero <= 11 and 1 <= day <= 31 and 0 <= hour <= 23):
            continue
        found.add((symbol, year, month_zero, day, hour))
    return found


def _iter_hours_for_year(year: int) -> list[datetime]:
    """Every UTC hour-boundary datetime within calendar year ``year``."""
    hours: list[datetime] = []
    for month in range(1, 13):
        _, days_in_month = monthrange(year, month)
        for day in range(1, days_in_month + 1):
            for hour in range(24):
                hours.append(datetime(year, month, day, hour, tzinfo=UTC))
    return hours


# --------------------------------------------------------------------------- #
# Core fetch primitive (exported for tests)
# --------------------------------------------------------------------------- #
def fetch_one_hour_with_retries(
    symbol: str,
    ts_hour: datetime,
    *,
    http_client: HttpClient,
    max_retries: int,
    sleep_ms: int,
) -> bytes | None:
    """Fetch a single hour with bounded retries; return bytes or ``None`` on
    give-up.

    Returns:

    * ``bytes`` (possibly empty) on the first successful call.
    * ``None`` after ``max_retries`` consecutive :class:`DukascopyError`s.
      The caller logs this as ``failed=1`` and moves on; the next run will
      pick up the missing hour (no on-disk marker is written for failures).

    The ``sleep_ms`` parameter is applied **between retry attempts** (not the
    initial attempt). The caller is responsible for the inter-hour sleep so
    that successful first-attempts also throttle.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_hour_bi5(symbol, ts_hour, http_client=http_client)
        except DukascopyError as exc:
            last_exc = exc
            if attempt < max_retries:
                # Sleep before the next retry, not after the final failure.
                time.sleep(sleep_ms / 1000.0)
    # Exhausted retries — log and signal give-up.
    logger.warning(
        "give-up on %s %s after %d attempts: %r",
        symbol,
        ts_hour.isoformat(),
        max_retries,
        last_exc,
    )
    return None


# --------------------------------------------------------------------------- #
# Per-day fetch loop (exported for tests)
# --------------------------------------------------------------------------- #
class _DayCounters:
    """Per-day mutable tally — kept tiny so the loop is cheap to test."""

    __slots__ = ("bytes_written", "cached", "empty", "failed", "fetched")

    def __init__(self) -> None:
        self.fetched = 0
        self.cached = 0
        self.empty = 0
        self.failed = 0
        self.bytes_written = 0


def _process_one_hour(
    symbol: str,
    ts_hour: datetime,
    *,
    raw_root: Path,
    existing: set[tuple[str, int, int, int, int]],
    http_client: HttpClient,
    sleep_ms: int,
    max_retries: int,
    counters: _DayCounters,
) -> None:
    """Fetch, write, and update counters for one hour.

    Cache hits short-circuit (no network). Successful fetches sleep
    ``sleep_ms`` *after* writing so the next iteration is throttled. Failures
    do NOT write any file — the missing path is what signals "retry me on
    the next run".
    """
    key = (symbol, ts_hour.year, ts_hour.month - 1, ts_hour.day, ts_hour.hour)
    if key in existing:
        counters.cached += 1
        return

    raw = fetch_one_hour_with_retries(
        symbol,
        ts_hour,
        http_client=http_client,
        max_retries=max_retries,
        sleep_ms=sleep_ms,
    )
    if raw is None:
        counters.failed += 1
        return

    out_path = _hour_path(raw_root, symbol, ts_hour)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    existing.add(key)  # update in-memory so a re-pass doesn't refetch.

    if len(raw) == 0:
        counters.empty += 1
    else:
        counters.fetched += 1
        counters.bytes_written += len(raw)

    # Throttle after every successful network call (cache hits don't sleep).
    time.sleep(sleep_ms / 1000.0)


def _process_day(
    symbol: str,
    year: int,
    month: int,
    day: int,
    *,
    raw_root: Path,
    existing: set[tuple[str, int, int, int, int]],
    http_client: HttpClient,
    sleep_ms: int,
    max_retries: int,
) -> _DayCounters:
    """Fetch every hour of one (symbol, day); log one summary line."""
    counters = _DayCounters()
    day_start = time.monotonic()
    for hour in range(24):
        ts = datetime(year, month, day, hour, tzinfo=UTC)
        _process_one_hour(
            symbol,
            ts,
            raw_root=raw_root,
            existing=existing,
            http_client=http_client,
            sleep_ms=sleep_ms,
            max_retries=max_retries,
            counters=counters,
        )
    elapsed = time.monotonic() - day_start
    logger.info(
        "%s %04d-%02d-%02d fetched=%d cached=%d empty=%d failed=%d bytes=%s elapsed=%.1fs",
        symbol,
        year,
        month,
        day,
        counters.fetched,
        counters.cached,
        counters.empty,
        counters.failed,
        f"{counters.bytes_written:,}",
        elapsed,
    )
    return counters


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def _eta_seconds(hours_to_fetch: int, sleep_ms: int) -> float:
    """ETA formula: ``hours * (sleep_s + avg_fetch_latency_s)``."""
    sleep_s = sleep_ms / 1000.0
    return hours_to_fetch * (sleep_s + _AVG_FETCH_LATENCY_S)


def _format_eta(seconds: float) -> str:
    """Format ETA in hours (one decimal) for the startup banner."""
    return f"{seconds / 3600.0:.1f} hours"


def _validate_args(
    symbols: list[str],
    year_min: int,
    year_max: int,
    sleep_ms: int,
    max_retries: int,
) -> None:
    """Raise typer.BadParameter on any invalid CLI input."""
    if not symbols:
        raise typer.BadParameter("at least one --symbol is required")
    unknown = [s for s in symbols if s not in SUPPORTED_SYMBOLS]
    if unknown:
        raise typer.BadParameter(f"unknown symbols {unknown}; supported: {list(SUPPORTED_SYMBOLS)}")
    if year_min > year_max:
        raise typer.BadParameter(f"--year-min ({year_min}) must be <= --year-max ({year_max})")
    if not (_MIN_SLEEP_MS <= sleep_ms <= _MAX_SLEEP_MS):
        raise typer.BadParameter(
            f"--sleep-ms must be in [{_MIN_SLEEP_MS}, {_MAX_SLEEP_MS}]; got {sleep_ms}"
        )
    if max_retries < 1:
        raise typer.BadParameter(f"--max-retries must be >= 1; got {max_retries}")


def run_bulk_fetch(
    symbols: list[str],
    year_min: int,
    year_max: int,
    raw_root: Path,
    *,
    sleep_ms: int = 100,
    max_retries: int = 3,
    dry_run: bool = False,
    http_client: HttpClient | None = None,
) -> int:
    """Pure-Python entrypoint exported for tests.

    Returns the total count of hours newly written (fetched + empty markers)
    across the run. Tests assert on this and on the on-disk filesystem state.
    """
    _validate_args(symbols, year_min, year_max, sleep_ms, max_retries)
    _configure_logging()

    raw_root = raw_root.resolve()
    raw_root.mkdir(parents=True, exist_ok=True)

    client = http_client if http_client is not None else UrllibHttpClient()

    # Resume scan + ETA accounting.
    print(
        f"[bulk_fetch_dukascopy] symbols={symbols} years={year_min}-{year_max}",
        flush=True,
    )
    existing_per_symbol: dict[str, set[tuple[str, int, int, int, int]]] = {}
    total_existing = 0
    for symbol in symbols:
        found = _scan_existing(raw_root, symbol)
        existing_per_symbol[symbol] = found
        total_existing += len(found)
    print(
        f"[bulk_fetch_dukascopy] discovered {total_existing} existing .bi5 files "
        f"in raw_root={raw_root}",
        flush=True,
    )

    # Hours-to-fetch count = sum over symbols of (year-span hours minus skips).
    total_hours = 0
    hours_to_fetch = 0
    for symbol in symbols:
        existing = existing_per_symbol[symbol]
        for year in range(year_min, year_max + 1):
            year_hours = _iter_hours_for_year(year)
            total_hours += len(year_hours)
            for ts in year_hours:
                key = (symbol, ts.year, ts.month - 1, ts.day, ts.hour)
                if key not in existing:
                    hours_to_fetch += 1
    skipping = total_hours - hours_to_fetch
    print(
        f"[bulk_fetch_dukascopy] hours to fetch: {hours_to_fetch} "
        f"(skipping {skipping} already-cached)",
        flush=True,
    )
    print(
        f"[bulk_fetch_dukascopy] sleep_ms={sleep_ms} max_retries={max_retries}",
        flush=True,
    )
    eta_s = _eta_seconds(hours_to_fetch, sleep_ms)
    print(
        f"[bulk_fetch_dukascopy] ETA: ~{_format_eta(eta_s)} "
        f"({hours_to_fetch} * {sleep_ms / 1000.0 + _AVG_FETCH_LATENCY_S:.1f}s = {eta_s:.0f}s)",
        flush=True,
    )

    if dry_run:
        print("[bulk_fetch_dukascopy] dry-run: exiting without fetch", flush=True)
        return 0

    print("[bulk_fetch_dukascopy] starting fetch...", flush=True)

    grand_fetched = 0
    grand_cached = 0
    grand_empty = 0
    grand_failed = 0
    grand_bytes = 0

    for symbol in symbols:
        existing = existing_per_symbol[symbol]
        for year in range(year_min, year_max + 1):
            year_fetched = 0
            year_cached = 0
            year_empty = 0
            year_failed = 0
            year_bytes = 0
            for month in range(1, 13):
                _, days_in_month = monthrange(year, month)
                for day in range(1, days_in_month + 1):
                    day_counters = _process_day(
                        symbol,
                        year,
                        month,
                        day,
                        raw_root=raw_root,
                        existing=existing,
                        http_client=client,
                        sleep_ms=sleep_ms,
                        max_retries=max_retries,
                    )
                    year_fetched += day_counters.fetched
                    year_cached += day_counters.cached
                    year_empty += day_counters.empty
                    year_failed += day_counters.failed
                    year_bytes += day_counters.bytes_written
            logger.info(
                "%s %04d year-summary: fetched=%d cached=%d empty=%d failed=%d bytes=%s",
                symbol,
                year,
                year_fetched,
                year_cached,
                year_empty,
                year_failed,
                f"{year_bytes:,}",
            )
            grand_fetched += year_fetched
            grand_cached += year_cached
            grand_empty += year_empty
            grand_failed += year_failed
            grand_bytes += year_bytes

    logger.info(
        "GRAND TOTAL: fetched=%d cached=%d empty=%d failed=%d bytes=%s",
        grand_fetched,
        grand_cached,
        grand_empty,
        grand_failed,
        f"{grand_bytes:,}",
    )
    return grand_fetched + grand_empty


# --------------------------------------------------------------------------- #
# Typer CLI
# --------------------------------------------------------------------------- #
app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)


@app.command()
def main(
    symbol: list[str] | None = _SYMBOL_OPT,
    year_min: int = _YEAR_MIN_OPT,
    year_max: int | None = _YEAR_MAX_OPT,
    raw_root: Path = _RAW_ROOT_OPT,
    sleep_ms: int = _SLEEP_MS_OPT,
    max_retries: int = _MAX_RETRIES_OPT,
    dry_run: bool = _DRY_RUN_OPT,
) -> None:
    """Bulk-fetch raw Dukascopy ``.bi5`` files into the on-disk cache layout."""
    symbols = list(symbol) if symbol else []
    ymax = year_max if year_max is not None else datetime.now(UTC).year
    run_bulk_fetch(
        symbols=symbols,
        year_min=year_min,
        year_max=ymax,
        raw_root=raw_root,
        sleep_ms=sleep_ms,
        max_retries=max_retries,
        dry_run=dry_run,
    )


__all__ = [
    "app",
    "fetch_one_hour_with_retries",
    "main",
    "run_bulk_fetch",
]


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    app()
