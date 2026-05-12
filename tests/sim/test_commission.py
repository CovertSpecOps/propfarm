"""Tests for ``propfarm.sim.commission`` (Task 6.2) — per-firm commission tables.

Each test is fully offline: snapshot files are committed-to-repo markdown, never
fetched at test time. The numbers asserted below are the round-trip USD/lot
figures recorded in those snapshots, deliberately checked against the typed
``CommissionTable`` so a silent edit of one without the other trips a test.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.sim.commission import (
    ALL_TABLES,
    FTMO_MT5_COMMISSION,
    FUNDEDNEXT_MT5_COMMISSION,
    FUNDINGPIPS_MT5_COMMISSION,
    CommissionTable,
    commission_for_trade,
)

# --------------------------------------------------------------------------- #
# Per-firm round-trip rates: assert each firm-table matches its own snapshot
# at 1 lot, for every symbol in the table. This is property-test style: it
# guards against a future refactor silently dropping or re-numbering a row.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION],
    ids=lambda t: t.firm,
)
def test_firm_one_lot_round_trip_matches_snapshot(table: CommissionTable) -> None:
    """For each (firm, symbol), `commission_for_trade(1 lot)` equals the table value.

    This is the central correctness contract: the snapshot file is the source
    of truth, the BaseModel instance is the typed projection of that snapshot,
    and the function must return the projected value verbatim.
    """
    for symbol, expected in table.per_round_trip_usd.items():
        actual = commission_for_trade(table=table, symbol=symbol, volume_lots=1.0)
        assert actual == expected, (
            f"{table.firm} {symbol}: expected ${expected}/lot RT, got ${actual}"
        )


@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION],
    ids=lambda t: t.firm,
)
def test_commission_is_linear_in_volume(table: CommissionTable) -> None:
    """0.5 lots -> half the per-lot rate; 2.0 lots -> double. For every firm + symbol."""
    for symbol, full in table.per_round_trip_usd.items():
        half = commission_for_trade(table=table, symbol=symbol, volume_lots=0.5)
        double = commission_for_trade(table=table, symbol=symbol, volume_lots=2.0)
        assert half == pytest.approx(full / 2.0), (
            f"{table.firm} {symbol}: 0.5 lots != half. Expected {full / 2.0}, got {half}."
        )
        assert double == pytest.approx(full * 2.0), (
            f"{table.firm} {symbol}: 2.0 lots != double. Expected {full * 2.0}, got {double}."
        )


def test_unknown_symbol_raises() -> None:
    """An unsupported symbol must raise ValueError, not silently return 0.

    A typo like ``"EUR/USD"`` or ``"ZZZ"`` is a programmer error; returning 0
    would let the bug propagate downstream into the placebo gate as "we made
    money with no costs" which is exactly the failure mode this task exists
    to prevent.
    """
    with pytest.raises(ValueError, match="unknown symbol"):
        commission_for_trade(table=FTMO_MT5_COMMISSION, symbol="ZZZ", volume_lots=1.0)


def test_zero_volume_returns_zero() -> None:
    """Edge case: 0 lots → $0 commission. No round-trip happened."""
    assert commission_for_trade(table=FTMO_MT5_COMMISSION, symbol="EURUSD", volume_lots=0.0) == 0.0


def test_negative_volume_raises() -> None:
    """Negative volume is a programmer error.

    Two reasonable choices: (a) raise, treating negative volume as malformed
    input, or (b) silently return 0/abs-value. We pick (a) — the simulator
    represents short trades by side flag, not by negative volume, so a
    negative ``volume_lots`` here always indicates a bug upstream.
    """
    with pytest.raises(ValueError, match="volume_lots must be non-negative"):
        commission_for_trade(table=FTMO_MT5_COMMISSION, symbol="EURUSD", volume_lots=-1.0)


# --------------------------------------------------------------------------- #
# Snapshot file existence — guards against silent loss in a refactor
# --------------------------------------------------------------------------- #
def test_snapshot_files_exist() -> None:
    """The markdown snapshot referenced by each table must exist on disk.

    The snapshots are the legal-trail audit record: which URL was fetched on
    what day for what numbers. If a refactor moves them, the integrity of
    the cost-model calibration evaporates — so we keep a smoke test wired in.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for table in (FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION):
        snapshot = repo_root / table.snapshot_source
        assert snapshot.exists(), (
            f"{table.firm}: snapshot file missing at {snapshot} "
            f"(table.snapshot_source = {table.snapshot_source!r})"
        )
        assert snapshot.suffix == ".md", (
            f"{table.firm}: snapshot must be a .md file, got {snapshot.suffix}"
        )


# --------------------------------------------------------------------------- #
# Coverage — every supported symbol must have a quoted commission per firm
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION],
    ids=lambda t: t.firm,
)
def test_all_supported_symbols_have_commissions(table: CommissionTable) -> None:
    """Every member of ``data.quality.SUPPORTED_SYMBOLS`` is keyed in each firm's table.

    Adding a new symbol to the platform is a coordinated change: it lands in
    ``SUPPORTED_SYMBOLS``, in every firm's commission table, in every firm's
    swap table, etc. This test makes a missing commission entry visible the
    moment the new symbol is registered, before any backtest is run.
    """
    missing = [s for s in SUPPORTED_SYMBOLS if s not in table.per_round_trip_usd]
    assert not missing, (
        f"{table.firm}: missing commissions for {missing}. "
        f"Every SUPPORTED_SYMBOLS member needs a quoted round-trip."
    )


# --------------------------------------------------------------------------- #
# CommissionTable frozen-ness — pydantic v2 ConfigDict(frozen=True)
# --------------------------------------------------------------------------- #
def test_commission_table_is_frozen() -> None:
    """Tables must be immutable after construction.

    A mutable cost table is a backtest correctness hazard: any module that
    accidentally bumps a fee would corrupt every subsequent run within the
    same process. ``BaseModel(frozen=True)`` makes the field-level mutation
    raise ``ValidationError``.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FTMO_MT5_COMMISSION.firm = "evil"


def test_all_tables_dict_keys_match_firm_field() -> None:
    """``ALL_TABLES`` is keyed by firm name; that key must match each table's ``firm`` field.

    Off-by-one keying ("ftmo_demo" in the dict, "ftmo" on the model) is the
    sort of bug that breaks at runtime in the rules engine months later.
    """
    for firm_key, table in ALL_TABLES.items():
        assert firm_key == table.firm, (
            f"ALL_TABLES['{firm_key}'].firm == {table.firm!r}; key and field must agree."
        )


# --------------------------------------------------------------------------- #
# Spot-check the headline numbers — guards against typos in the literal table
# --------------------------------------------------------------------------- #
def test_ftmo_eurusd_round_trip_one_lot_is_five_dollars() -> None:
    """FTMO EURUSD 1-lot round-trip: $5.00 per the 2025-09-29 schedule (2.50/side * 2).

    This is the canonical Phase-0-brief sanity check. If this number ever
    changes via a future trading-update, the snapshot file must be replaced
    AND this test updated together.
    """
    assert FTMO_MT5_COMMISSION.per_round_trip_usd["EURUSD"] == 5.00


def test_fundednext_eurusd_round_trip_one_lot_is_seven_dollars() -> None:
    """FundedNext Stellar 2-Step EURUSD 1-lot round-trip: $7.00."""
    assert FUNDEDNEXT_MT5_COMMISSION.per_round_trip_usd["EURUSD"] == 7.00


def test_fundingpips_eurusd_round_trip_one_lot_is_five_dollars() -> None:
    """FundingPips Raw Assessment EURUSD 1-lot round-trip: $5.00."""
    assert FUNDINGPIPS_MT5_COMMISSION.per_round_trip_usd["EURUSD"] == 5.00


def test_indices_are_zero_for_all_three_firms() -> None:
    """GER40 and US100 are zero-commission at all three firms per their 2025/2026 schedules.

    If any firm reintroduces an index commission, this test fails and the
    snapshot file plus the table literal must be updated together.
    """
    for table in (FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION):
        assert table.per_round_trip_usd["GER40"] == 0.0, f"{table.firm}: GER40 should be zero."
        assert table.per_round_trip_usd["US100"] == 0.0, f"{table.firm}: US100 should be zero."


# --------------------------------------------------------------------------- #
# Snapshot-date sanity: must be a real date, not a default-of-today bug
# --------------------------------------------------------------------------- #
def test_snapshot_date_matches_filename() -> None:
    """Each table's ``snapshot_date`` matches the date encoded in the snapshot filename.

    Snapshots are filename-dated for human discoverability. If the model's
    ``snapshot_date`` drifts from the filename, the audit trail breaks.
    """
    for table in (FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION):
        expected_suffix = f"-{table.snapshot_date.isoformat()}.md"
        assert table.snapshot_source.endswith(expected_suffix), (
            f"{table.firm}: snapshot_source {table.snapshot_source!r} does not end with "
            f"{expected_suffix!r} — date drift between filename and model."
        )


def test_snapshot_dates_are_today_2026_05_12() -> None:
    """All three snapshots were retrieved on the task's nominal date.

    A late refresh that updates one snapshot but not the others would leave a
    skewed audit trail; this test surfaces that drift loudly.
    """
    target = date(2026, 5, 12)
    for table in (FTMO_MT5_COMMISSION, FUNDEDNEXT_MT5_COMMISSION, FUNDINGPIPS_MT5_COMMISSION):
        assert table.snapshot_date == target, (
            f"{table.firm}: snapshot_date {table.snapshot_date} != expected {target}"
        )
