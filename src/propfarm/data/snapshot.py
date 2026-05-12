"""Content-hashed Parquet snapshot writer and loader (Task 4.1).

The snapshot layer is the trust anchor for every downstream backtest. Vendor
archives (Dukascopy, HistData) are *not* trusted directly: they can be updated
retroactively, which silently rewrites historical "facts" and corrupts any
backtest comparison made against a previous pull.

Instead, every vendor pull is frozen into a Parquet file under
``data/snapshots/{name}/{partition}.parquet`` together with a manifest entry in
``data/manifests/snapshot.json`` that records the SHA256 of the file bytes,
row count, min/max timestamps, vendor, schema, and the UTC creation time.

Every loader hashes the on-disk parquet bytes and refuses to return the
DataFrame if the hash disagrees with the manifest — drifted bytes raise
:class:`SnapshotIntegrityError`. This eliminates one entire class of
"my backtest result changed last week and I don't know why" failures.

Public API
----------
* :func:`write_snapshot` — freeze a DataFrame, update the manifest.
* :func:`load_snapshot`  — read a frozen snapshot, verifying the hash.
* :func:`list_snapshots` — list every registered snapshot entry.
* :class:`SnapshotEntry` — the manifest record (pydantic model).
* :class:`SnapshotIntegrityError` — raised on hash drift.

Partitioning convention
-----------------------
The ``partition`` argument is a free-form forward-slash path fragment that
becomes the relative location of the parquet file under
``data/snapshots/{name}/``. Two recommended forms:

* ``"all"`` (or ``None``) — a single unpartitioned snapshot at
  ``data/snapshots/{name}.parquet`` (no subdirectory) when ``partition is None``,
  or ``data/snapshots/{name}/all.parquet`` when ``partition == "all"``.
* ``"year=2024/month=01"`` — Hive-style partitioning for tick storage; the
  recommended scheme per the Phase 0 plan (Day 4, Task 4.1, Step 4).

The manifest stores each ``(name, partition)`` pair as a *separate* entry, so
two partitions of the same snapshot never overwrite each other.

Writer settings
---------------
Mirrors the byte-stable settings established in
``scripts/generate_synthetic_returns.py``:

* ``compression="zstd"`` with ``compression_level=9`` — higher than the
  fixture's level 3 because tick data is larger and rarely re-written.
* ``use_dictionary=False`` — dictionary encoding can introduce non-determinism
  on string columns between runs.
* ``write_statistics=False`` — page statistics can leak non-determinism.

Together these produce byte-identical output across runs of the same DataFrame,
which is what makes SHA256-pinning a meaningful integrity check.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Type aliases and constants
# --------------------------------------------------------------------------- #

#: Relative path fragment identifying one partition of a snapshot.
#:
#: ``None`` means "single, unpartitioned file". Otherwise any forward-slash
#: separated string, e.g. ``"all"``, ``"year=2024"``, or
#: ``"year=2024/month=01"``. The string is used both as the on-disk relative
#: parquet location *and* as the manifest key alongside ``name``.
PartitionKey = str

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_SNAPSHOTS_SUBDIR: Final[str] = "data/snapshots"
_MANIFEST_SUBDIR: Final[str] = "data/manifests"
_MANIFEST_FILENAME: Final[str] = "snapshot.json"

_PARQUET_COMPRESSION: Final[str] = "zstd"
_PARQUET_COMPRESSION_LEVEL: Final[int] = 9
_PARQUET_USE_DICTIONARY: Final[bool] = False
_PARQUET_WRITE_STATISTICS: Final[bool] = False


# --------------------------------------------------------------------------- #
# Manifest record model and integrity error
# --------------------------------------------------------------------------- #
class SnapshotEntry(BaseModel):
    """One row in the snapshot manifest.

    Attributes
    ----------
    name : str
        Logical snapshot name (e.g. ``"dukascopy_eurusd_ticks"``).
    partition : str | None
        Partition key (e.g. ``"year=2024/month=01"``), or ``None`` for an
        unpartitioned snapshot.
    vendor : str
        Source vendor identifier (e.g. ``"dukascopy"``, ``"histdata"``).
    sha256 : str
        Lowercase hex digest of the on-disk parquet bytes.
    row_count : int
        Number of rows in the parquet file.
    min_ts : str
        Minimum value of the ``ts`` column, ISO-8601 UTC. ``""`` if the
        DataFrame has no ``ts`` column.
    max_ts : str
        Maximum value of the ``ts`` column, ISO-8601 UTC. ``""`` if the
        DataFrame has no ``ts`` column.
    created_utc : str
        Manifest-write timestamp, ISO-8601 UTC, second precision.
    schema : dict[str, str]
        Mapping ``column_name -> polars dtype repr``, in insertion order.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    partition: str | None
    vendor: str
    sha256: str
    row_count: int
    min_ts: str
    max_ts: str
    created_utc: str
    schema_: dict[str, str] = Field(alias="schema")

    def matches(self, name: str, partition: str | None) -> bool:
        """Return True if this entry is for the given ``(name, partition)``."""
        return self.name == name and self.partition == partition


class SnapshotIntegrityError(RuntimeError):
    """Raised when a loaded snapshot's bytes do not match the manifest hash.

    The error message always includes both hashes (manifest-expected vs.
    on-disk-actual) and the absolute parquet path, so the operator can
    diagnose drift without re-running anything.
    """


# --------------------------------------------------------------------------- #
# Helpers (private)
# --------------------------------------------------------------------------- #
def _resolve_root(root: Path | None) -> Path:
    """Return ``root`` if provided, else the repo root (parents[3] of this file)."""
    return root if root is not None else _REPO_ROOT


def _snapshot_path(root: Path, name: str, partition: PartitionKey | None) -> Path:
    """Compute the on-disk parquet path for one ``(name, partition)`` pair.

    Path layout:

    * ``partition is None`` → ``{root}/data/snapshots/{name}.parquet``.
    * Otherwise            → ``{root}/data/snapshots/{name}/{partition}.parquet``.
    """
    base = root / _SNAPSHOTS_SUBDIR
    if partition is None:
        return base / f"{name}.parquet"
    # Reject absolute paths (POSIX or Windows) and backslash separators outright
    # — partition keys are Hive-style logical paths, never filesystem paths.
    if partition.startswith(("/", "\\")) or "\\" in partition:
        raise ValueError(
            f"partition must not be absolute or contain backslashes, got {partition!r}"
        )
    # Normalize partition: split on '/' and join — defends against accidental
    # leading slashes or double slashes in the caller's input.
    parts = [p for p in partition.split("/") if p]
    if not parts:
        raise ValueError(f"partition must be None or a non-empty string, got {partition!r}")
    # Reject any '..' component: prevents partition keys derived from filenames
    # or vendor metadata from escaping the snapshots tree.
    if any(p == ".." or p == "." for p in parts):
        raise ValueError(f"partition must not contain '..' or '.' components, got {partition!r}")
    return base.joinpath(name, *parts).with_suffix(".parquet")


def _manifest_path(root: Path) -> Path:
    return root / _MANIFEST_SUBDIR / _MANIFEST_FILENAME


def _sha256_file(path: Path) -> str:
    """Stream-hash the file at ``path`` and return its lowercase hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso_utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _ts_bounds(df: pl.DataFrame) -> tuple[str, str]:
    """Return ``(min_ts, max_ts)`` ISO-8601 strings for the ``ts`` column.

    If the DataFrame has no ``ts`` column (or is empty), returns ``("", "")``.
    Non-datetime ``ts`` columns are coerced via ``str()``.
    """
    if "ts" not in df.columns or df.height == 0:
        return ("", "")
    ts_col = df["ts"]
    lo = ts_col.min()
    hi = ts_col.max()
    return (str(lo) if lo is not None else "", str(hi) if hi is not None else "")


def _schema_repr(df: pl.DataFrame) -> dict[str, str]:
    """Return a stable ``{column: dtype-str}`` map for manifest storage."""
    return {col: str(dtype) for col, dtype in df.schema.items()}


def _write_parquet_atomic(df: pl.DataFrame, dest: Path) -> None:
    """Write ``df`` to ``dest`` via PyArrow with the byte-stable settings.

    Uses an os.replace from a sibling temp file so a crashed/partial write
    never leaves a half-written parquet in place to be hashed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    table: pa.Table = df.to_arrow()
    # NamedTemporaryFile gives us a path in the same directory as ``dest`` so
    # os.replace is atomic on POSIX. delete=False because we close+rename it.
    with tempfile.NamedTemporaryFile(
        dir=dest.parent,
        prefix=f".{dest.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        pq.write_table(  # type: ignore[no-untyped-call]
            table,
            tmp_path,
            compression=_PARQUET_COMPRESSION,
            compression_level=_PARQUET_COMPRESSION_LEVEL,
            use_dictionary=_PARQUET_USE_DICTIONARY,
            write_statistics=_PARQUET_WRITE_STATISTICS,
        )
        os.replace(tmp_path, dest)
    except BaseException:
        # Best-effort cleanup of the partial temp; never mask the original error.
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def _load_manifest(manifest_path: Path) -> list[SnapshotEntry]:
    """Return all entries in the manifest, or [] if the file does not exist."""
    if not manifest_path.exists():
        return []
    raw = manifest_path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    payload = json.loads(raw)
    entries_raw = payload.get("entries", [])
    return [SnapshotEntry.model_validate(e) for e in entries_raw]


def _write_manifest(manifest_path: Path, entries: list[SnapshotEntry]) -> None:
    """Write the manifest atomically, with stable ordering for diffability."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries, key=lambda e: (e.name, e.partition or ""))
    payload = {
        "version": 1,
        "entries": [e.model_dump(by_alias=True) for e in ordered],
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        dir=manifest_path.parent,
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(serialized)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, manifest_path)


def _upsert_entry(entries: list[SnapshotEntry], new_entry: SnapshotEntry) -> list[SnapshotEntry]:
    """Replace any entry with the same ``(name, partition)``, else append."""
    return [e for e in entries if not e.matches(new_entry.name, new_entry.partition)] + [new_entry]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def write_snapshot(
    df: pl.DataFrame,
    name: str,
    *,
    vendor: str,
    root: Path | None = None,
    partition: PartitionKey | None = None,
) -> SnapshotEntry:
    """Freeze ``df`` to disk and upsert its entry in the snapshot manifest.

    Parameters
    ----------
    df : polars.DataFrame
        The data to snapshot. If it contains a ``ts`` column, its min/max are
        recorded; otherwise both are stored as the empty string.
    name : str
        Logical snapshot name. Becomes part of the on-disk path and the
        manifest key.
    vendor : str
        Source vendor identifier (e.g. ``"dukascopy"``).
    root : pathlib.Path, optional
        Root directory under which ``data/snapshots/`` and ``data/manifests/``
        live. Defaults to the repo root (three levels above this file). Tests
        pass ``tmp_path`` here to isolate from real on-disk state.
    partition : str, optional
        Partition key. ``None`` writes ``{root}/data/snapshots/{name}.parquet``;
        a string writes ``{root}/data/snapshots/{name}/{partition}.parquet``.
        Two partitions of the same name are stored as separate manifest
        entries and never overwrite each other.

    Returns
    -------
    SnapshotEntry
        The freshly-written manifest entry. Already persisted to disk.
    """
    root_dir = _resolve_root(root)
    parquet_path = _snapshot_path(root_dir, name, partition)
    _write_parquet_atomic(df, parquet_path)

    digest = _sha256_file(parquet_path)
    min_ts, max_ts = _ts_bounds(df)
    entry = SnapshotEntry(
        name=name,
        partition=partition,
        vendor=vendor,
        sha256=digest,
        row_count=df.height,
        min_ts=min_ts,
        max_ts=max_ts,
        created_utc=_iso_utc_now(),
        schema=_schema_repr(df),
    )

    manifest_path = _manifest_path(root_dir)
    entries = _upsert_entry(_load_manifest(manifest_path), entry)
    _write_manifest(manifest_path, entries)
    return entry


def load_snapshot(
    name: str,
    *,
    root: Path | None = None,
    partition: PartitionKey | None = None,
) -> pl.DataFrame:
    """Load a previously-written snapshot, verifying its SHA256 against the manifest.

    Parameters
    ----------
    name : str
        Logical snapshot name passed to :func:`write_snapshot`.
    root : pathlib.Path, optional
        See :func:`write_snapshot`. Must match the ``root`` the snapshot was
        written under.
    partition : str, optional
        Partition key passed to :func:`write_snapshot`.

    Returns
    -------
    polars.DataFrame
        The original DataFrame, restored from the parquet file.

    Raises
    ------
    FileNotFoundError
        If the parquet file does not exist on disk.
    KeyError
        If the parquet exists but there is no matching manifest entry. (A
        snapshot without a manifest entry has no expected hash, so its
        integrity cannot be verified — refusing to load is the safe default.)
    SnapshotIntegrityError
        If the on-disk SHA256 disagrees with the manifest entry. The message
        contains both hashes and the absolute parquet path.
    """
    root_dir = _resolve_root(root)
    parquet_path = _snapshot_path(root_dir, name, partition)
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"snapshot parquet not found: {parquet_path} (name={name!r}, partition={partition!r})"
        )

    manifest_path = _manifest_path(root_dir)
    entries = _load_manifest(manifest_path)
    match = next((e for e in entries if e.matches(name, partition)), None)
    if match is None:
        raise KeyError(
            f"no manifest entry for snapshot (name={name!r}, partition={partition!r}); "
            f"refusing to load unhashed bytes from {parquet_path}"
        )

    actual = _sha256_file(parquet_path)
    if actual != match.sha256:
        raise SnapshotIntegrityError(
            "snapshot SHA256 mismatch — file bytes have drifted from the manifest.\n"
            f"  path:     {parquet_path}\n"
            f"  expected: {match.sha256}\n"
            f"  actual:   {actual}\n"
            f"  name={name!r}, partition={partition!r}"
        )

    return pl.read_parquet(parquet_path)


def list_snapshots(root: Path | None = None) -> list[SnapshotEntry]:
    """Return every entry in the snapshot manifest under ``root``.

    Returns an empty list if no manifest file exists yet. The result is sorted
    by ``(name, partition)`` for stable iteration.
    """
    root_dir = _resolve_root(root)
    entries = _load_manifest(_manifest_path(root_dir))
    return sorted(entries, key=lambda e: (e.name, e.partition or ""))


__all__ = [
    "PartitionKey",
    "SnapshotEntry",
    "SnapshotIntegrityError",
    "list_snapshots",
    "load_snapshot",
    "write_snapshot",
]
