"""Ingest raw Dukascopy ``.bi5`` archives into content-hashed snapshots (Task 4.2).

The Task 3.1 downloader produces a tree of LZMA-compressed hourly tick files on
disk::

    data/raw/dukascopy/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

where ``MM-1`` is the Dukascopy 0-indexed-month quirk (Jan = ``00``, Dec =
``11``). The Task 4.1 snapshot writer (:func:`propfarm.data.snapshot.write_snapshot`)
freezes a polars DataFrame into a content-hashed Parquet file under
``data/snapshots/{name}/{partition}.parquet`` and appends an integrity entry to
``data/manifests/snapshot.json``.

This module is the glue. It walks the raw tree, groups every ``.bi5`` by
``(symbol, calendar-year, calendar-month)``, parses each hour with
:func:`propfarm.data.vendors.dukascopy.parse_bi5`, concatenates, deduplicates by
timestamp, sorts, and writes exactly one snapshot per partition.

The CLI wrapper lives in ``scripts/ingest_to_snapshot.py``; this module exposes
the pure-Python primitives so tests can exercise them without subprocessing.

Public surface
--------------

* :class:`IngestPlan` — one symbol-month worth of work (frozen pydantic model).
* :class:`IngestResult` — the outcome of running :func:`ingest_plan` on a plan.
* :func:`discover_plans` — walk the raw tree and emit one :class:`IngestPlan`
  per ``(symbol, year, month)`` partition. Handles the 0-indexed-month quirk.
* :func:`ingest_plan` — parse every ``.bi5`` referenced by a plan, dedupe + sort
  on ``ts``, and write the result via :func:`write_snapshot`. Idempotent by
  default (``skip_if_exists=True`` skips the parquet write when the manifest
  already has an entry for the target ``(name, partition)``).
* :data:`PHASE0_SYMBOLS` — the six instruments Phase 0 targets, in canonical
  order. The CLI uses this as the default when ``--symbol`` is not supplied.

Empty (zero-byte) ``.bi5`` files are skipped with a logged warning. Dukascopy
serves a zero-byte body for hours with no ticks (typical weekend hours on FX
majors); raising on these would force the operator to pre-filter the raw tree,
which is exactly what this module exists to absorb.

Invariants preserved end-to-end
-------------------------------

* ``ts`` is monotonically non-decreasing in the written snapshot.
* No duplicate ``ts`` values (dedupe keeps the first occurrence).
* ``bid < ask`` per row (enforced upstream by :func:`parse_bi5`).
* The partition key is Hive-style ``year=YYYY/month=MM`` with a leading-zero
  month — matching the recommendation in :mod:`propfarm.data.snapshot`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import polars as pl
from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.data.snapshot import (
    SnapshotEntry,
    _load_manifest,
    _manifest_path,
    _resolve_root,
    write_snapshot,
)
from propfarm.data.vendors.dukascopy import parse_bi5

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: The six instruments Phase 0 (and the Phase-0 plan, Day 3 Task 3.1 Step 5)
#: targets. Imported from the canonical `propfarm.data.quality.SUPPORTED_SYMBOLS`
#: rather than re-declared inline — W6a reviewer flagged the previous duplicate
#: as a silent drift risk. Order matches the registry's tuple iteration order.
PHASE0_SYMBOLS: Final[tuple[str, ...]] = SUPPORTED_SYMBOLS

#: Regex for Dukascopy hourly tick filenames: e.g. ``10h_ticks.bi5``.
_HOUR_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<hour>\d{2})h_ticks\.bi5$")

#: Vendor identifier recorded in the snapshot manifest.
_VENDOR: Final[str] = "dukascopy"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class IngestPlan(BaseModel):
    """One symbol-month worth of work to ingest.

    All paths are absolute so the plan is self-contained (callable from any
    working directory).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    year: int
    #: 1-indexed calendar month (1=January). NOT Dukascopy's 0-indexed form.
    month: int
    bi5_files: tuple[Path, ...]
    #: Hive-style partition key, e.g. ``"year=2024/month=01"``.
    partition: str
    #: Snapshot logical name, e.g. ``"dukascopy_EURUSD_ticks"``.
    snapshot_name: str


class IngestResult(BaseModel):
    """The outcome of running :func:`ingest_plan` on one plan."""

    model_config = ConfigDict(frozen=True)

    plan: IngestPlan
    row_count: int
    snapshot_sha256: str
    #: Path to the on-disk parquet, relative to the snapshot root. Forward
    #: slashes regardless of OS so the value is reproducible across platforms.
    snapshot_path: str
    skipped: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _partition_for(year: int, month: int) -> str:
    """Return the Hive-style partition key for a calendar (year, month) pair."""
    return f"year={year:04d}/month={month:02d}"


def _snapshot_name_for(symbol: str) -> str:
    """Return the canonical snapshot name for a symbol's tick archive."""
    return f"dukascopy_{symbol}_ticks"


def _hour_ts_from_path(symbol_dir: Path, bi5_path: Path) -> datetime | None:
    """Reconstruct the UTC hour-boundary datetime from a ``.bi5`` file path.

    Expected structure (relative to ``symbol_dir``)::

        {YYYY}/{MM_zero_indexed:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

    Returns ``None`` if any component is unparseable. The 0-indexed month is
    translated back to a 1-indexed calendar month (e.g. ``00`` → January).
    """
    try:
        rel = bi5_path.relative_to(symbol_dir)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) != 4:
        return None
    year_s, month_s, day_s, fname = parts
    m = _HOUR_FILENAME_RE.match(fname)
    if m is None:
        return None
    try:
        year = int(year_s)
        month0 = int(month_s)  # 0-indexed on disk (Dukascopy quirk).
        day = int(day_s)
        hour = int(m.group("hour"))
    except ValueError:
        return None
    if not (0 <= month0 <= 11):
        return None
    if not (1 <= day <= 31):
        return None
    if not (0 <= hour <= 23):
        return None
    month = month0 + 1
    try:
        return datetime(year, month, day, hour, tzinfo=UTC)
    except ValueError:
        # e.g. Feb 30 on disk. Skip silently — the file is malformed.
        return None


def _iter_symbol_dirs(
    raw_root: Path, symbols: tuple[str, ...] | None
) -> Iterable[tuple[str, Path]]:
    """Yield ``(symbol, symbol_dir)`` for every present symbol under ``raw_root``.

    If ``symbols`` is provided, restrict to that allowlist (preserving its
    order). Otherwise, iterate alphabetically over the directories that exist.
    """
    if not raw_root.exists():
        return
    if symbols is not None:
        for sym in symbols:
            symbol_dir = raw_root / sym
            if symbol_dir.is_dir():
                yield sym, symbol_dir
        return
    for child in sorted(raw_root.iterdir()):
        if child.is_dir():
            yield child.name, child


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def discover_plans(
    raw_root: Path,
    *,
    symbols: tuple[str, ...] | None = None,
    year_range: tuple[int, int] | None = None,
) -> tuple[IngestPlan, ...]:
    """Walk a raw Dukascopy tree and emit one :class:`IngestPlan` per partition.

    Path layout consumed (relative to ``raw_root``)::

        {SYMBOL}/{YYYY}/{MM_zero_indexed:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

    The ``MM_zero_indexed`` segment is Dukascopy's quirk (Jan = ``00``); the
    returned :attr:`IngestPlan.month` is the **1-indexed calendar month**.

    Parameters
    ----------
    raw_root:
        The root of the Dukascopy raw cache, e.g. ``data/raw/dukascopy``.
    symbols:
        Optional allowlist of instrument codes (case-sensitive). When ``None``,
        every symbol-directory under ``raw_root`` is considered.
    year_range:
        Optional inclusive ``(year_min, year_max)`` filter applied to the
        calendar year derived from the on-disk path.

    Returns
    -------
    tuple[IngestPlan, ...]
        Plans sorted by ``(symbol, year, month)`` for deterministic output.
        Each plan's ``bi5_files`` tuple is sorted by absolute path.
    """
    plans: list[IngestPlan] = []
    for symbol, symbol_dir in _iter_symbol_dirs(raw_root, symbols):
        grouped: dict[tuple[int, int], list[Path]] = {}
        for bi5_path in sorted(symbol_dir.rglob("*.bi5")):
            if not bi5_path.is_file():
                continue
            hour_ts = _hour_ts_from_path(symbol_dir, bi5_path)
            if hour_ts is None:
                logger.warning("skipping unparseable bi5 path: %s", bi5_path)
                continue
            if year_range is not None:
                year_min, year_max = year_range
                if not (year_min <= hour_ts.year <= year_max):
                    continue
            key = (hour_ts.year, hour_ts.month)
            grouped.setdefault(key, []).append(bi5_path)
        for (year, month), files in sorted(grouped.items()):
            plans.append(
                IngestPlan(
                    symbol=symbol,
                    year=year,
                    month=month,
                    bi5_files=tuple(sorted(files)),
                    partition=_partition_for(year, month),
                    snapshot_name=_snapshot_name_for(symbol),
                )
            )
    return tuple(plans)


def _read_bi5(path: Path, symbol: str, symbol_dir: Path) -> pl.DataFrame | None:
    """Parse one ``.bi5`` file and return its DataFrame, or ``None`` to skip.

    Skipping conditions (each emits a WARNING log line):

    * The file is zero-byte (Dukascopy returns these for hours with no ticks).
    * The path is unparseable (defensive — :func:`discover_plans` already
      filters these out, but we re-check so callers passing hand-built plans
      get the same behaviour).
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("cannot stat %s: %s; skipping", path, exc)
        return None
    if size == 0:
        logger.warning("empty bi5 file %s; skipping", path)
        return None
    hour_ts = _hour_ts_from_path(symbol_dir, path)
    if hour_ts is None:
        logger.warning("cannot infer hour from %s; skipping", path)
        return None
    raw = path.read_bytes()
    return parse_bi5(raw, hour_ts=hour_ts, symbol=symbol)


def _symbol_root_from_files(files: tuple[Path, ...]) -> Path:
    """Recover ``symbol_dir`` from a plan's ``bi5_files`` tuple.

    The plan does not carry ``symbol_dir`` explicitly because it can be derived
    deterministically from the file paths: every file lives four levels deep
    inside the symbol directory (YYYY/MM/DD/HH_ticks.bi5).
    """
    if not files:
        raise ValueError("cannot recover symbol root from empty bi5_files tuple")
    return files[0].parents[3]


def ingest_plan(
    plan: IngestPlan,
    *,
    snapshot_root: Path | None = None,
    skip_if_exists: bool = True,
) -> IngestResult:
    """Parse every ``.bi5`` in ``plan``, dedupe + sort, and write one snapshot.

    Parameters
    ----------
    plan:
        The :class:`IngestPlan` describing one symbol-month partition.
    snapshot_root:
        Forwarded as ``root`` to :func:`write_snapshot`. ``None`` selects the
        repo root (the snapshot module's default). Tests pass ``tmp_path``.
    skip_if_exists:
        If ``True`` (default) and the manifest already has an entry for
        ``(plan.snapshot_name, plan.partition)``, return early with
        ``skipped=True`` and do NOT touch the parquet on disk. The returned
        ``snapshot_sha256`` is the existing manifest entry's hash. Set to
        ``False`` to force a rewrite (the CLI's ``--force`` flag).

    Returns
    -------
    IngestResult
        The outcome record. ``skipped=False`` means we wrote (or rewrote) the
        parquet; ``skipped=True`` means the manifest already had the entry and
        we returned without parsing any ``.bi5`` files.
    """
    root_dir = _resolve_root(snapshot_root)
    snapshot_rel = f"data/snapshots/{plan.snapshot_name}/{plan.partition}.parquet"

    if skip_if_exists:
        existing = _find_manifest_entry(root_dir, plan.snapshot_name, plan.partition)
        if existing is not None:
            return IngestResult(
                plan=plan,
                row_count=existing.row_count,
                snapshot_sha256=existing.sha256,
                snapshot_path=snapshot_rel,
                skipped=True,
            )

    symbol_dir = _symbol_root_from_files(plan.bi5_files)
    frames: list[pl.DataFrame] = []
    for bi5_path in plan.bi5_files:
        df = _read_bi5(bi5_path, plan.symbol, symbol_dir)
        if df is None or df.height == 0:
            continue
        frames.append(df)

    if frames:
        combined = pl.concat(frames, how="vertical")
        # Dedupe-by-ts: two overlapping hour files can legitimately emit the
        # same ts if upstream reposts ticks; keep the first occurrence so the
        # operation is deterministic given a sorted bi5_files tuple.
        deduped = combined.unique(subset=["ts"], keep="first").sort("ts")
    else:
        # Empty plan (every bi5 was zero-byte): emit an empty frame with the
        # canonical schema so the snapshot manifest still records the
        # partition. Downstream code can detect "no ticks" via row_count==0.
        deduped = pl.DataFrame(
            schema={
                "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
                "bid": pl.Float64,
                "ask": pl.Float64,
                "bid_vol": pl.Float64,
                "ask_vol": pl.Float64,
            }
        )

    entry = write_snapshot(
        deduped,
        plan.snapshot_name,
        vendor=_VENDOR,
        root=snapshot_root,
        partition=plan.partition,
    )
    return IngestResult(
        plan=plan,
        row_count=entry.row_count,
        snapshot_sha256=entry.sha256,
        snapshot_path=snapshot_rel,
        skipped=False,
    )


def _find_manifest_entry(root_dir: Path, name: str, partition: str) -> SnapshotEntry | None:
    """Return the manifest entry for ``(name, partition)`` or ``None``."""
    entries = _load_manifest(_manifest_path(root_dir))
    return next((e for e in entries if e.matches(name, partition)), None)


__all__ = [
    "PHASE0_SYMBOLS",
    "IngestPlan",
    "IngestResult",
    "discover_plans",
    "ingest_plan",
]
