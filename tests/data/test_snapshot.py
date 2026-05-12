"""Tests for ``propfarm.data.snapshot`` (Task 4.1).

The snapshot layer is the trust anchor for every downstream backtest, so the
test contract is strict:

* Round-trip preserves DataFrame equality.
* Manifest records SHA256, row count, ts bounds, vendor.
* Drifted bytes raise ``SnapshotIntegrityError`` with both hashes + path.
* Missing parquet → ``FileNotFoundError``.
* Missing manifest entry → ``KeyError`` (no expected hash → cannot verify).
* Partitioned writes coexist; both load independently.
* Repeated writes are byte-stable (no dictionary / no statistics).
* Every test is isolated via ``tmp_path``; the real ``data/snapshots/`` is
  never touched.

The fixture DataFrames are tiny on purpose — these are spec tests, not
performance tests.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import polars as pl
import polars.testing as pltest
import pytest

from propfarm.data.snapshot import (
    SnapshotIntegrityError,
    list_snapshots,
    load_snapshot,
    write_snapshot,
)


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #
def _tiny_frame() -> pl.DataFrame:
    """Four-row toy DataFrame with a ts column, used by most tests."""
    return pl.DataFrame(
        {
            "ts": [
                datetime(2024, 1, 1, 0, 0, 0),
                datetime(2024, 1, 1, 0, 0, 1),
                datetime(2024, 1, 1, 0, 0, 2),
                datetime(2024, 1, 1, 0, 0, 3),
            ],
            "bid": [1.09510, 1.09511, 1.09512, 1.09513],
            "ask": [1.09512, 1.09513, 1.09514, 1.09515],
            "symbol": ["EURUSD", "EURUSD", "EURUSD", "EURUSD"],
        }
    )


def _sha256_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    df = _tiny_frame()
    entry = write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)

    assert entry.name == "eurusd_ticks"
    assert entry.vendor == "dukascopy"
    assert entry.row_count == df.height

    loaded = load_snapshot("eurusd_ticks", root=tmp_path)
    # polars's assert_frame_equal does a full schema + values comparison.
    pltest.assert_frame_equal(loaded, df)


# --------------------------------------------------------------------------- #
# Manifest contents
# --------------------------------------------------------------------------- #
def test_manifest_records_sha256_and_metadata(tmp_path: Path) -> None:
    df = _tiny_frame()
    entry = write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)

    parquet_path = tmp_path / "data" / "snapshots" / "eurusd_ticks.parquet"
    assert parquet_path.exists()
    expected_sha = _sha256_bytes(parquet_path)
    assert entry.sha256 == expected_sha

    manifest_path = tmp_path / "data" / "manifests" / "snapshot.json"
    assert manifest_path.exists()
    raw = json.loads(manifest_path.read_text())
    assert raw["version"] == 1
    assert len(raw["entries"]) == 1
    rec = raw["entries"][0]

    assert rec["name"] == "eurusd_ticks"
    assert rec["partition"] is None
    assert rec["vendor"] == "dukascopy"
    assert rec["sha256"] == expected_sha
    assert rec["row_count"] == df.height
    assert rec["min_ts"] == "2024-01-01 00:00:00"
    assert rec["max_ts"] == "2024-01-01 00:00:03"
    assert "created_utc" in rec
    # created_utc is ISO-8601 UTC with a trailing offset.
    assert rec["created_utc"].endswith("+00:00")
    # Schema is recorded so reviewers can spot dtype drift even without the file.
    assert set(rec["schema"].keys()) == {"ts", "bid", "ask", "symbol"}


# --------------------------------------------------------------------------- #
# Hash drift
# --------------------------------------------------------------------------- #
def test_load_raises_on_hash_drift(tmp_path: Path) -> None:
    df = _tiny_frame()
    entry = write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)
    parquet_path = tmp_path / "data" / "snapshots" / "eurusd_ticks.parquet"

    # Mutate exactly one byte near the end of the parquet (footer area is safe
    # to perturb — touching the magic header would yield a parse error before
    # the hash check).
    data = bytearray(parquet_path.read_bytes())
    idx = len(data) // 2
    data[idx] ^= 0x01
    parquet_path.write_bytes(bytes(data))

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        load_snapshot("eurusd_ticks", root=tmp_path)

    msg = str(exc_info.value)
    actual = _sha256_bytes(parquet_path)
    # Both hashes and the path must appear in the message for fast diagnosis.
    assert entry.sha256 in msg
    assert actual in msg
    assert str(parquet_path) in msg


# --------------------------------------------------------------------------- #
# Missing file / missing manifest entry
# --------------------------------------------------------------------------- #
def test_load_raises_on_missing_file(tmp_path: Path) -> None:
    df = _tiny_frame()
    write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)

    parquet_path = tmp_path / "data" / "snapshots" / "eurusd_ticks.parquet"
    parquet_path.unlink()

    with pytest.raises(FileNotFoundError) as exc_info:
        load_snapshot("eurusd_ticks", root=tmp_path)
    assert "eurusd_ticks" in str(exc_info.value)


def test_load_raises_on_missing_manifest_entry(tmp_path: Path) -> None:
    df = _tiny_frame()
    write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)

    # Wipe the manifest, leave the parquet in place.
    manifest_path = tmp_path / "data" / "manifests" / "snapshot.json"
    manifest_path.write_text(json.dumps({"version": 1, "entries": []}) + "\n")

    with pytest.raises(KeyError) as exc_info:
        load_snapshot("eurusd_ticks", root=tmp_path)
    assert "eurusd_ticks" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Partitioned writes
# --------------------------------------------------------------------------- #
def test_write_partitioned(tmp_path: Path) -> None:
    df_jan = _tiny_frame()
    df_feb = pl.DataFrame(
        {
            "ts": [datetime(2024, 2, 1, 0, 0, 0), datetime(2024, 2, 1, 0, 0, 1)],
            "bid": [1.09600, 1.09601],
            "ask": [1.09602, 1.09603],
            "symbol": ["EURUSD", "EURUSD"],
        }
    )

    e_jan = write_snapshot(
        df_jan,
        "eurusd_ticks",
        vendor="dukascopy",
        root=tmp_path,
        partition="year=2024/month=01",
    )
    e_feb = write_snapshot(
        df_feb,
        "eurusd_ticks",
        vendor="dukascopy",
        root=tmp_path,
        partition="year=2024/month=02",
    )

    # Each partition is its own manifest entry — neither overwrites the other.
    assert e_jan.sha256 != e_feb.sha256
    entries = list_snapshots(root=tmp_path)
    assert len(entries) == 2
    partitions = {e.partition for e in entries}
    assert partitions == {"year=2024/month=01", "year=2024/month=02"}

    # Both load independently and match what we wrote.
    loaded_jan = load_snapshot("eurusd_ticks", root=tmp_path, partition="year=2024/month=01")
    loaded_feb = load_snapshot("eurusd_ticks", root=tmp_path, partition="year=2024/month=02")
    pltest.assert_frame_equal(loaded_jan, df_jan)
    pltest.assert_frame_equal(loaded_feb, df_feb)

    # On-disk layout follows the documented convention.
    assert (
        tmp_path / "data" / "snapshots" / "eurusd_ticks" / "year=2024" / "month=01.parquet"
    ).exists()
    assert (
        tmp_path / "data" / "snapshots" / "eurusd_ticks" / "year=2024" / "month=02.parquet"
    ).exists()


# --------------------------------------------------------------------------- #
# Byte-stable rewrite
# --------------------------------------------------------------------------- #
def test_byte_stable_rewrite(tmp_path: Path) -> None:
    """Writing the same DataFrame twice must produce identical parquet bytes.

    This is the property that makes SHA256-pinning meaningful. The settings
    that guarantee it (``use_dictionary=False``, ``write_statistics=False``,
    ``compression="zstd"``) are encoded in ``snapshot._write_parquet_atomic``.
    """
    df = _tiny_frame()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"

    e_a = write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=root_a)
    e_b = write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=root_b)

    # The two parquet files must be byte-identical despite living in different
    # roots and having different created_utc timestamps in their manifests.
    bytes_a = (root_a / "data" / "snapshots" / "eurusd_ticks.parquet").read_bytes()
    bytes_b = (root_b / "data" / "snapshots" / "eurusd_ticks.parquet").read_bytes()
    assert bytes_a == bytes_b
    assert e_a.sha256 == e_b.sha256


# --------------------------------------------------------------------------- #
# Isolation under tmp_path
# --------------------------------------------------------------------------- #
def test_isolation_under_tmp_path(tmp_path: Path) -> None:
    """Every write must land under tmp_path and nowhere else.

    Guards against any future refactor that silently drops the ``root``
    argument and writes to the real repo ``data/`` directory.
    """
    df = _tiny_frame()
    write_snapshot(df, "eurusd_ticks", vendor="dukascopy", root=tmp_path)
    write_snapshot(
        df,
        "eurusd_ticks",
        vendor="dukascopy",
        root=tmp_path,
        partition="year=2024/month=01",
    )

    parquet_files = list((tmp_path / "data" / "snapshots").rglob("*.parquet"))
    assert len(parquet_files) == 2
    for p in parquet_files:
        assert tmp_path in p.parents, f"snapshot escaped tmp_path: {p}"

    manifest_path = tmp_path / "data" / "manifests" / "snapshot.json"
    assert manifest_path.exists()
    assert tmp_path in manifest_path.parents


# --------------------------------------------------------------------------- #
# Upsert: re-writing the same (name, partition) replaces, never duplicates
# --------------------------------------------------------------------------- #
def test_rewrite_upserts_manifest_entry(tmp_path: Path) -> None:
    df_v1 = _tiny_frame()
    df_v2 = df_v1.with_columns(pl.col("bid") + 0.00010)

    write_snapshot(df_v1, "eurusd_ticks", vendor="dukascopy", root=tmp_path)
    e2 = write_snapshot(df_v2, "eurusd_ticks", vendor="dukascopy", root=tmp_path)

    entries = list_snapshots(root=tmp_path)
    assert len(entries) == 1
    assert entries[0].sha256 == e2.sha256

    loaded = load_snapshot("eurusd_ticks", root=tmp_path)
    pltest.assert_frame_equal(loaded, df_v2)
