"""Tests for ``propfarm.data.ingest`` (Task 4.2).

All tests are offline: we synthesize a tiny ``.bi5`` tree under ``tmp_path``
using the helpers in ``tests/data/_synthetic.py`` (created by Task 3.1) and
exercise :func:`discover_plans` / :func:`ingest_plan` against it.

The Dukascopy on-disk layout being faked here::

    {raw_root}/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5

Note the **0-indexed month** in the path segment — see
``test_dukascopy_month_zero_index_preserved`` for the critical regression
guard.

Coverage map (numbered per the task brief in the implementation prompt):

1.  ``test_discover_plans_finds_all_partitions``
2.  ``test_discover_plans_filters_by_symbol``
3.  ``test_discover_plans_filters_by_year_range``
4.  ``test_ingest_plan_writes_snapshot``
5.  ``test_ingest_plan_idempotent``
6.  ``test_ingest_plan_force_overwrites``
7.  ``test_dedupe_by_timestamp``
8.  ``test_sort_monotonic``
9.  ``test_bid_lt_ask_invariant_preserved``
10. ``test_partition_string_hive_style``
11. ``test_dukascopy_month_zero_index_preserved``
12. ``test_empty_bi5_skipped_gracefully``
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from propfarm.data.ingest import (
    discover_plans,
    ingest_plan,
)
from propfarm.data.snapshot import list_snapshots, load_snapshot

from ._synthetic import SyntheticTick, make_synthetic_bi5


# --------------------------------------------------------------------------- #
# Synthetic .bi5 tree builder
# --------------------------------------------------------------------------- #
def _write_bi5(
    raw_root: Path,
    symbol: str,
    year: int,
    month_calendar: int,
    day: int,
    hour: int,
    records: list[SyntheticTick],
) -> Path:
    """Write a synthetic ``.bi5`` to the Dukascopy on-disk layout.

    ``month_calendar`` is the 1-indexed calendar month (Jan=1). The on-disk
    segment is written 0-indexed to match the Dukascopy quirk.
    """
    month0 = month_calendar - 1
    dest = (
        raw_root
        / symbol
        / f"{year:04d}"
        / f"{month0:02d}"
        / f"{day:02d}"
        / f"{hour:02d}h_ticks.bi5"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if records:
        dest.write_bytes(make_synthetic_bi5(records))
    else:
        dest.write_bytes(b"")  # zero-byte for the empty-skipped test.
    return dest


def _three_eurusd_records() -> list[SyntheticTick]:
    """Three valid EURUSD ticks with bid_int < ask_int."""
    return [
        SyntheticTick(ms_from_hour=100, ask_int=109520, bid_int=109510, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(ms_from_hour=500, ask_int=109525, bid_int=109515, ask_vol=2.0, bid_vol=2.0),
        SyntheticTick(ms_from_hour=900, ask_int=109530, bid_int=109520, ask_vol=3.0, bid_vol=3.0),
    ]


def _build_tree(
    raw_root: Path,
    *,
    symbols: tuple[str, ...],
    years: tuple[int, ...],
    months_calendar: tuple[int, ...],
    files_per_partition: int = 5,
) -> None:
    """Populate ``raw_root`` with one ``.bi5`` per (sym, yr, mo, day, hour).

    ``files_per_partition`` hours are written per (symbol, year, month) under
    day=02. Each file contains three valid EURUSD-shaped records (the parser
    accepts any of the six Phase-0 symbols thanks to the matching digits map).
    """
    for sym in symbols:
        for yr in years:
            for mo in months_calendar:
                for h in range(files_per_partition):
                    _write_bi5(raw_root, sym, yr, mo, 2, 10 + h, _three_eurusd_records())


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_discover_plans_finds_all_partitions(tmp_path: Path) -> None:
    """2 symbols x 2 months x 5 hours = 4 plans, each with 5 bi5_files."""
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD", "GBPUSD"),
        years=(2024,),
        months_calendar=(1, 2),
        files_per_partition=5,
    )

    plans = discover_plans(raw_root)
    assert len(plans) == 4
    for p in plans:
        assert len(p.bi5_files) == 5
        assert p.symbol in ("EURUSD", "GBPUSD")
        assert p.year == 2024
        assert p.month in (1, 2)


def test_discover_plans_filters_by_symbol(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD", "GBPUSD"),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=2,
    )

    plans = discover_plans(raw_root, symbols=("EURUSD",))
    assert len(plans) == 1
    assert plans[0].symbol == "EURUSD"


def test_discover_plans_filters_by_year_range(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2023, 2024),
        months_calendar=(1,),
        files_per_partition=2,
    )

    plans = discover_plans(raw_root, year_range=(2024, 2024))
    assert len(plans) == 1
    assert plans[0].year == 2024


def test_ingest_plan_writes_snapshot(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=3,
    )
    (plan,) = discover_plans(raw_root)

    result = ingest_plan(plan, snapshot_root=tmp_path)

    assert result.skipped is False
    # 3 files x 3 records = 9 rows, all distinct ts.
    assert result.row_count == 9
    snapshot = load_snapshot(plan.snapshot_name, root=tmp_path, partition=plan.partition)
    assert snapshot.height == 9


def test_ingest_plan_idempotent(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=2,
    )
    (plan,) = discover_plans(raw_root)

    first = ingest_plan(plan, snapshot_root=tmp_path)
    assert first.skipped is False

    second = ingest_plan(plan, snapshot_root=tmp_path, skip_if_exists=True)
    assert second.skipped is True
    assert second.snapshot_sha256 == first.snapshot_sha256
    assert second.row_count == first.row_count


def test_ingest_plan_force_overwrites(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=2,
    )
    (plan,) = discover_plans(raw_root)

    first = ingest_plan(plan, snapshot_root=tmp_path)
    second = ingest_plan(plan, snapshot_root=tmp_path, skip_if_exists=False)
    assert second.skipped is False
    # Same inputs → byte-stable parquet → same sha.
    assert second.snapshot_sha256 == first.snapshot_sha256
    # Manifest has exactly one entry for this (name, partition).
    entries = [
        e
        for e in list_snapshots(root=tmp_path)
        if e.name == plan.snapshot_name and e.partition == plan.partition
    ]
    assert len(entries) == 1


def test_dedupe_by_timestamp(tmp_path: Path) -> None:
    """Two .bi5 files with overlapping ms_from_hour → no duplicate ts."""
    raw_root = tmp_path / "raw"
    overlap_records = [
        SyntheticTick(ms_from_hour=100, ask_int=109520, bid_int=109510, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(ms_from_hour=200, ask_int=109525, bid_int=109515, ask_vol=2.0, bid_vol=2.0),
    ]
    # Same hour file written twice would be the same file on disk, so instead
    # we use two files that point at the same hour boundary by writing one
    # file legitimately and then introducing duplicates within the same file's
    # records. Polars unique() collapses them in ingest_plan.
    same_hour_dups = overlap_records + overlap_records
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 10, same_hour_dups)
    # Add a second hour with one of the same ms offsets to prove cross-file
    # dedup also works (different hour_ts → different absolute ts, so this
    # case will NOT dedupe — included as a negative control).
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 11, overlap_records)

    (plan,) = discover_plans(raw_root)
    result = ingest_plan(plan, snapshot_root=tmp_path)

    snapshot = load_snapshot(plan.snapshot_name, root=tmp_path, partition=plan.partition)
    ts_series = snapshot["ts"]
    assert ts_series.n_unique() == snapshot.height
    # 2 unique ts in hour 10 + 2 unique ts in hour 11 = 4 rows.
    assert result.row_count == 4


def test_sort_monotonic(tmp_path: Path) -> None:
    """Snapshot's ts column is monotonically non-decreasing."""
    raw_root = tmp_path / "raw"
    # Intentionally write hours out of natural order across two days.
    _write_bi5(raw_root, "EURUSD", 2024, 1, 3, 9, _three_eurusd_records())
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 14, _three_eurusd_records())
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 10, _three_eurusd_records())

    (plan,) = discover_plans(raw_root)
    ingest_plan(plan, snapshot_root=tmp_path)

    snapshot = load_snapshot(plan.snapshot_name, root=tmp_path, partition=plan.partition)
    ts = snapshot["ts"].to_list()
    assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))


def test_bid_lt_ask_invariant_preserved(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=4,
    )
    (plan,) = discover_plans(raw_root)
    ingest_plan(plan, snapshot_root=tmp_path)

    snapshot = load_snapshot(plan.snapshot_name, root=tmp_path, partition=plan.partition)
    diff = (snapshot["ask"] - snapshot["bid"]).to_list()
    assert all(d > 0 for d in diff)


def test_partition_string_hive_style(tmp_path: Path) -> None:
    """Partition is exactly 'year=2024/month=01' (leading-zero month)."""
    raw_root = tmp_path / "raw"
    _build_tree(
        raw_root,
        symbols=("EURUSD",),
        years=(2024,),
        months_calendar=(1,),
        files_per_partition=1,
    )
    (plan,) = discover_plans(raw_root)
    assert plan.partition == "year=2024/month=01"


def test_dukascopy_month_zero_index_preserved(tmp_path: Path) -> None:
    """A path .../EURUSD/2024/00/02/10h_ticks.bi5 → January 2024 partition."""
    raw_root = tmp_path / "raw"
    # Build the path directly with the 0-indexed month segment.
    dest = raw_root / "EURUSD" / "2024" / "00" / "02" / "10h_ticks.bi5"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(make_synthetic_bi5(_three_eurusd_records()))

    (plan,) = discover_plans(raw_root)
    assert plan.month == 1  # 1-indexed calendar month
    assert plan.year == 2024
    assert plan.partition == "year=2024/month=01"


def test_empty_bi5_skipped_gracefully(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A 0-byte .bi5 is skipped with a logged warning; ingest still succeeds."""
    raw_root = tmp_path / "raw"
    # One valid + one empty file in the same partition.
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 10, _three_eurusd_records())
    _write_bi5(raw_root, "EURUSD", 2024, 1, 2, 11, [])  # zero-byte

    (plan,) = discover_plans(raw_root)
    assert len(plan.bi5_files) == 2

    with caplog.at_level(logging.WARNING, logger="propfarm.data.ingest"):
        result = ingest_plan(plan, snapshot_root=tmp_path)

    assert result.skipped is False
    assert result.row_count == 3  # Only the valid file's records.
    assert any("empty bi5" in rec.message for rec in caplog.records)
