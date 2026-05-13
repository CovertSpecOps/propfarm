"""Tests for ``propfarm.sim.swap`` (Task 6.3) — swap/financing & triple-Wednesday.

The swap module answers two intertwined questions:

1. *How many "swap nights" does a position cross?* — counted as crossings of
   22:00 New York time (the global FX rollover instant). The **Wednesday
   rollover** counts as **3 nights** (the "triple-Wednesday" rule, covering
   the Sat+Sun+Mon T+2 settlement gap). Crossings at Fri 22:00 ET, Sat
   22:00 ET, and Sun 22:00 ET-pre-FX-open are not counted because the
   market is closed and brokers do not assess swap there.
2. *Given a swap-rate table for a firm, what USD cost does that produce?* —
   resolved via :func:`propfarm.sim.swap.swap_for_position`. The output
   uses the **simulator's "positive = cost" PnL sign convention**, which
   is the *inverse* of the MT5 server's swap_long / swap_short fields
   (broker convention: positive = trader is credited).

Every test is offline. No MT5, broker, or VPS strings appear.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.sim.swap import (
    FTMO_MT5_SWAP,
    FUNDEDNEXT_MT5_SWAP,
    FUNDINGPIPS_MT5_SWAP,
    SwapTable,
    nights_held,
    swap_for_position,
)

# --------------------------------------------------------------------------- #
# Test fixtures: a small synthetic SwapTable that we can reason about.
# --------------------------------------------------------------------------- #
# 1.0 point / lot / night, long pays 10 USD/night cost (broker convention:
# -10 in the table means broker charges trader 10 USD/lot/night).
_TEST_TABLE = SwapTable(
    firm="TEST",
    account_type="DEMO",
    snapshot_date=date(2026, 5, 12),
    snapshot_source="tests/sim/test_swap.py fixture",
    triple_rollover_weekday=2,  # Wednesday
    swap_long_points={s: -10.0 for s in SUPPORTED_SYMBOLS},
    swap_short_points={s: +4.0 for s in SUPPORTED_SYMBOLS},
    point_value_usd={s: 1.0 for s in SUPPORTED_SYMBOLS},
)


# --------------------------------------------------------------------------- #
# nights_held
# --------------------------------------------------------------------------- #
def test_intraday_hold_no_swap() -> None:
    """Open and close both before the 22:00 ET rollover on the same UTC day → 0 nights."""
    # Monday 2024-06-03, 10:00 → 16:00 UTC. The 22:00 ET rollover on Monday
    # is at 02:00 UTC Tuesday (EDT) — well after close at 16:00.
    open_ts = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 3, 16, 0, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_one_night_hold_charges_one_night_swap() -> None:
    """Hold Mon 23:00 UTC → Tue 23:30 UTC: crosses Tue 02:00 UTC (Mon→Tue rollover, EDT)."""
    # 2024-06-03 (Mon) 23:00 UTC → 2024-06-04 (Tue) 23:30 UTC.
    # The 22:00 ET rollover that night is at 02:00 UTC on 2024-06-04 (EDT).
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 1


def test_wednesday_rollover_charges_triple() -> None:
    """Holding over the Wed→Thu 22:00 ET rollover charges 3x."""
    # 2024-06-05 is Wednesday. 22:00 ET on 2024-06-05 = 02:00 UTC 2024-06-06 (EDT).
    # Open Wed 12:00 UTC, close Thu 12:00 UTC: one crossing, on Wednesday → 3 nights.
    open_ts = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 6, 12, 0, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 3


def test_wednesday_rollover_via_swap_for_position() -> None:
    """The 3x multiplier flows through to USD swap cost on a Wed hold."""
    # Same window as above. 1 lot long EURUSD on the test table.
    # Table: swap_long = -10 pts/lot/night * point_value 1.0 USD/pt = -10 USD/lot/night
    # in broker convention. The simulator inverts to +10 USD/lot/night cost.
    # Three nights -> +30 USD cost.
    open_ts = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 6, 12, 0, tzinfo=UTC)
    cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert cost == pytest.approx(30.0)


def test_one_night_hold_charges_one_night_swap_in_dollars() -> None:
    """Confirm 1-night hold is exactly 1x the daily swap (no accidental triple)."""
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert cost == pytest.approx(10.0)


def test_friday_to_monday_weekend_hold_charges_zero_nights() -> None:
    """Hold Fri 21:00 UTC → Mon 09:00 UTC.

    The FX market closes Fri 22:00 UTC and reopens Sun 22:00 UTC. The
    Friday 22:00 ET, Saturday 22:00 ET, and Sunday 22:00 ET rollover
    instants all fall while the FX market is closed (Friday rollover
    instant) or while the position has not yet been re-exposed to a new
    trading session (Saturday and Sunday). The **industry-standard FX
    convention** — and the one this implementation locks — is:

    * **Weekend rollovers are not charged.** Saturday and Sunday swap is
      pre-paid via the **Wednesday 3x** triple rollover (covering the
      Sat+Sun+Mon T+2 settlement gap).
    * **Fri 22:00 ET** also does not charge — it sits at the moment of
      weekly close.

    Therefore: open Fri 21:00 UTC, close Mon 09:00 UTC → **0 nights**.
    Any strategy that holds *through* Wednesday already paid for this
    weekend; any strategy that opens Friday and closes Monday morning
    crosses no chargeable rollover.

    This test locks that behavior. If a future firm publishes a different
    convention (e.g., charging Sunday-evening reopen swap), it must be
    expressed via a *firm-specific* `SwapTable` extension, not by
    silently shifting the default counter.
    """
    # 2024-06-07 is Friday, 2024-06-10 is Monday.
    open_ts = datetime(2024, 6, 7, 21, 0, tzinfo=UTC)  # Fri 21:00 UTC
    close_ts = datetime(2024, 6, 10, 9, 0, tzinfo=UTC)  # Mon 09:00 UTC
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_full_week_mon_to_fri_charges_four_weekday_plus_wed_triple() -> None:
    """Mon 21:00 UTC → Fri 21:00 UTC crosses Mon-night, Tue-night, Wed-night (3x), Thu-night.

    Expected nights: 1 + 1 + 3 + 1 = 6. Locks the documented behaviour for
    a strategy holding the whole working week, which is the common
    swing-trade boundary.
    """
    open_ts = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)  # Mon 21:00 UTC
    close_ts = datetime(2024, 6, 7, 21, 0, tzinfo=UTC)  # Fri 21:00 UTC
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 6


def test_dst_winter_rollover_at_03_utc() -> None:
    """In US winter (EST = UTC-5) 22:00 ET = 03:00 UTC next day."""
    # 2024-01-08 (Mon) 23:00 UTC → 2024-01-09 (Tue) 04:00 UTC.
    # In EST, 22:00 ET on Mon = 03:00 UTC on Tuesday. So we cross 1 rollover.
    open_ts = datetime(2024, 1, 8, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 1, 9, 4, 0, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 1


def test_dst_winter_just_before_rollover() -> None:
    """In winter, closing at 02:59 UTC next day misses the 03:00 UTC rollover."""
    open_ts = datetime(2024, 1, 8, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 1, 9, 2, 59, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_dst_summer_rollover_at_02_utc() -> None:
    """In US summer (EDT = UTC-4) 22:00 ET = 02:00 UTC next day."""
    # 2024-06-03 (Mon) 23:00 UTC → 2024-06-04 (Tue) 02:30 UTC.
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 2, 30, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 1


def test_dst_summer_just_before_rollover() -> None:
    """In summer, closing at 01:59 UTC next day misses the 02:00 UTC rollover."""
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 1, 59, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_no_triple_when_weekday_disabled() -> None:
    """If triple_rollover_weekday is None, Wednesday is just 1 night (e.g. indices)."""
    open_ts = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)  # Wed
    close_ts = datetime(2024, 6, 6, 12, 0, tzinfo=UTC)  # Thu
    assert (
        nights_held(
            open_ts_utc=open_ts,
            close_ts_utc=close_ts,
            triple_rollover_weekday=None,
        )
        == 1
    )


def test_close_before_open_returns_zero() -> None:
    """If the close timestamp precedes or equals open, no nights are held."""
    open_ts = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 5, 11, 0, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_open_equals_close_returns_zero() -> None:
    open_ts = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=open_ts) == 0


def test_naive_datetime_raises() -> None:
    """Both timestamps must be tz-aware UTC."""
    naive = datetime(2024, 6, 5, 12, 0)
    with pytest.raises(ValueError, match="tz-aware"):
        nights_held(open_ts_utc=naive, close_ts_utc=datetime(2024, 6, 5, 13, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="tz-aware"):
        nights_held(open_ts_utc=datetime(2024, 6, 5, 12, 0, tzinfo=UTC), close_ts_utc=naive)


# --------------------------------------------------------------------------- #
# swap_for_position
# --------------------------------------------------------------------------- #
def test_swap_sign_is_consistent_with_table() -> None:
    """Broker-positive swap_long → simulator returns negative (credit) cost.

    Sign convention contract:
      * `swap_long_points` / `swap_short_points` follow the **broker MT5
        server convention** (positive = trader receives a credit).
      * `swap_for_position` returns "positive = cost charged to trader",
        matching the simulator's PnL convention.
      * Therefore a positive broker-table value flips to a negative
        simulator output.
    """
    # Build a table where short is positive (credit) on EURUSD.
    table_credit = _TEST_TABLE.model_copy(
        update={
            "swap_long_points": dict(_TEST_TABLE.swap_long_points),
            "swap_short_points": {**_TEST_TABLE.swap_short_points, "EURUSD": +5.0},
        }
    )
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    cost = swap_for_position(
        table=table_credit,
        symbol="EURUSD",
        direction="short",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    # +5 broker points * 1.0 USD/point * 1 lot * 1 night, flipped sign -> -5 USD cost.
    assert cost == pytest.approx(-5.0)


def test_long_and_short_swaps_can_differ() -> None:
    """Long EURUSD and short EURUSD typically differ (one earns, one pays).

    The test table is set so long is -10 (cost) and short is +4 (credit).
    Confirms the function reads the right table column per direction.
    """
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    long_cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    short_cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="short",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert long_cost != short_cost
    # Long pays (broker -10 → simulator +10), short earns (broker +4 → simulator -4).
    assert long_cost > 0
    assert short_cost < 0


def test_unknown_symbol_raises() -> None:
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"unknown symbol|not in"):
        swap_for_position(
            table=_TEST_TABLE,
            symbol="BOGUS",
            direction="long",
            volume_lots=1.0,
            open_ts_utc=open_ts,
            close_ts_utc=close_ts,
        )


def test_invalid_direction_raises() -> None:
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="direction"):
        swap_for_position(
            table=_TEST_TABLE,
            symbol="EURUSD",
            direction="sideways",
            volume_lots=1.0,
            open_ts_utc=open_ts,
            close_ts_utc=close_ts,
        )


def test_zero_volume_returns_zero() -> None:
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=0.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert cost == 0.0


def test_volume_scales_linearly() -> None:
    """Doubling volume doubles swap cost — linear scaling property."""
    open_ts = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 4, 23, 30, tzinfo=UTC)
    cost_1 = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    cost_2 = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=2.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert cost_2 == pytest.approx(2 * cost_1)


def test_intraday_hold_zero_swap_cost() -> None:
    open_ts = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    close_ts = datetime(2024, 6, 3, 16, 0, tzinfo=UTC)
    cost = swap_for_position(
        table=_TEST_TABLE,
        symbol="EURUSD",
        direction="long",
        volume_lots=1.0,
        open_ts_utc=open_ts,
        close_ts_utc=close_ts,
    )
    assert cost == 0.0


# --------------------------------------------------------------------------- #
# Firm-table coverage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_SWAP, FUNDEDNEXT_MT5_SWAP, FUNDINGPIPS_MT5_SWAP],
    ids=["FTMO", "FundedNext", "FundingPips"],
)
def test_all_supported_symbols_have_swap_rates(table: SwapTable) -> None:
    """Every firm's table must cover every symbol in SUPPORTED_SYMBOLS."""
    for symbol in SUPPORTED_SYMBOLS:
        assert symbol in table.swap_long_points, f"{table.firm}: missing swap_long for {symbol}"
        assert symbol in table.swap_short_points, f"{table.firm}: missing swap_short for {symbol}"
        assert symbol in table.point_value_usd, (
            f"{table.firm}: missing point_value_usd for {symbol}"
        )


@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_SWAP, FUNDEDNEXT_MT5_SWAP, FUNDINGPIPS_MT5_SWAP],
    ids=["FTMO", "FundedNext", "FundingPips"],
)
def test_triple_rollover_weekday_is_wednesday(table: SwapTable) -> None:
    """Every firm uses the industry-standard Wednesday triple rollover."""
    assert table.triple_rollover_weekday == 2, (
        f"{table.firm}: expected Wednesday=2 triple rollover, got {table.triple_rollover_weekday}"
    )


@pytest.mark.parametrize(
    "table",
    [FTMO_MT5_SWAP, FUNDEDNEXT_MT5_SWAP, FUNDINGPIPS_MT5_SWAP],
    ids=["FTMO", "FundedNext", "FundingPips"],
)
def test_firm_table_is_frozen(table: SwapTable) -> None:
    """SwapTable is pydantic-frozen — mutation raises."""
    with pytest.raises((ValueError, TypeError)):
        table.firm = "MUTATED"


# --------------------------------------------------------------------------- #
# Snapshot docs presence
# --------------------------------------------------------------------------- #
def test_snapshot_files_exist() -> None:
    """The three firm swap-rate snapshots must be checked in.

    Reviewer gate: the module's constant tables must trace back to a dated
    markdown record under ``docs/firm-tos-snapshots/``. If this test fails,
    a firm snapshot was deleted or the implementation drifted to numbers
    that have no recorded source.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    for firm in ("ftmo", "fundednext", "fundingpips"):
        p = repo_root / "docs" / "firm-tos-snapshots" / f"{firm}-swap-2026-05-12.md"
        assert p.exists(), f"missing swap snapshot: {p}"


def test_swaptable_validates_keys() -> None:
    """Constructing a SwapTable with mismatched long/short keys should fail loudly."""
    with pytest.raises(ValueError, match="symbol"):
        SwapTable(
            firm="BROKEN",
            account_type="DEMO",
            snapshot_date=date(2026, 5, 12),
            snapshot_source="test",
            triple_rollover_weekday=2,
            swap_long_points={"EURUSD": -1.0},
            swap_short_points={"GBPUSD": +1.0},  # different symbol set
            point_value_usd={"EURUSD": 1.0},
        )


# --------------------------------------------------------------------------- #
# Reviewer follow-ups: confidence field + DST-crossing triple-Wed +
# Wed-rollover boundary tests.
# --------------------------------------------------------------------------- #
def test_all_shipped_swap_tables_are_marked_uncertain() -> None:
    """Every shipped swap table was seeded from community references — the
    canonical source is the MT5 Symbol Specification dialog which is not
    reachable from this implementation host. Until live-account recalibration,
    the runtime model must advertise the uncertainty."""
    for table in (FTMO_MT5_SWAP, FUNDEDNEXT_MT5_SWAP, FUNDINGPIPS_MT5_SWAP):
        assert table.confidence == "uncertain", (
            f"{table.firm}: confidence={table.confidence!r}; should be 'uncertain' "
            "until live-broker calibration"
        )


def test_wed_rollover_boundary_zero_nights_no_crossing_edt() -> None:
    """Open Wed 21:30 UTC, close Wed 22:30 UTC (both before the EDT 02:00 UTC
    rollover that happens at Thu 02:00 UTC) → 0 nights, no Wed-triple."""
    open_ts = datetime(2024, 3, 13, 21, 30, tzinfo=UTC)  # Wed 17:30 EDT
    close_ts = datetime(2024, 3, 13, 22, 30, tzinfo=UTC)  # Wed 18:30 EDT
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 0


def test_wed_rollover_boundary_triple_when_crossing_edt() -> None:
    """Open Wed 21:30 UTC, close Thu 02:30 UTC (crosses Thu 02:00 UTC = Wed
    22:00 EDT, the triple rollover) → 3 nights."""
    open_ts = datetime(2024, 3, 13, 21, 30, tzinfo=UTC)  # Wed 17:30 EDT
    close_ts = datetime(2024, 3, 14, 2, 30, tzinfo=UTC)  # Thu 22:30 EDT (next day local)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 3


def test_wed_rollover_boundary_triple_when_crossing_est() -> None:
    """EST equivalent: open Wed 22:30 UTC, close Thu 03:30 UTC (crosses
    Thu 03:00 UTC = Wed 22:00 EST, the triple rollover) → 3 nights."""
    open_ts = datetime(2024, 11, 13, 22, 30, tzinfo=UTC)  # Wed 17:30 EST
    close_ts = datetime(2024, 11, 14, 3, 30, tzinfo=UTC)  # Thu 22:30 EST (next day local)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 3


def test_dst_crossing_triple_wed_hold() -> None:
    """Hold a position spanning US DST spring-forward (Sun 2024-03-10
    02:00 EST → 03:00 EDT). Open Fri 2024-03-08 21:00 UTC, close Thu
    2024-03-14 03:00 UTC.

    Expected nights:
    - Fri 22-ET rollover (weekend, not charged) -> 0
    - Sat, Sun, Mon NY-local weekdays: only Mon 22-ET is a chargeable
      weekday rollover -> 1
    - Tue 22-ET (NY) -> 1
    - Wed 22-ET (NY) -> 3 (triple)
    Total: 5. EDT begins Sun 2024-03-10; module must handle the
    DST transition without crashing or miscounting.
    """
    open_ts = datetime(2024, 3, 8, 21, 0, tzinfo=UTC)
    close_ts = datetime(2024, 3, 14, 3, 0, tzinfo=UTC)  # Thu 03:00 UTC > Wed 22-EDT rollover
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 5


def test_close_exactly_at_rollover_instant_is_counted() -> None:
    """The half-open window convention `open_ts < T_roll <= close_ts`
    means: closing exactly at the rollover counts the night. Locks the
    boundary semantics so a refactor can't silently flip it."""
    open_ts = datetime(2024, 3, 13, 21, 0, tzinfo=UTC)  # before Wed EDT rollover
    close_ts = datetime(2024, 3, 14, 2, 0, tzinfo=UTC)  # exactly at Wed 22-EDT (= Thu 02 UTC)
    assert nights_held(open_ts_utc=open_ts, close_ts_utc=close_ts) == 3


# --------------------------------------------------------------------------- #
# Reviewer follow-up: ALL_TABLES loader-pattern symmetry with commission.
# --------------------------------------------------------------------------- #
def test_swap_all_tables_loader_pattern_mirrors_commission() -> None:
    """W4 reviewer flagged that swap had no ALL_TABLES dict while commission
    did. This test locks the symmetry so a generic loader can iterate
    ``costs.{commission,swap}.ALL_TABLES["ftmo"]`` and read ``.confidence``
    uniformly across both."""
    from propfarm.sim.commission import ALL_TABLES as COMMISSION_TABLES
    from propfarm.sim.swap import ALL_TABLES as SWAP_TABLES

    # Same firm keys exposed by both registries (sanity).
    assert set(COMMISSION_TABLES.keys()) == set(SWAP_TABLES.keys())

    # Every table has a queryable .confidence attribute. Locks the W3/W4
    # loader-pattern contract: ``.confidence`` is uniform across cost and
    # rule registries.
    for firm in COMMISSION_TABLES:
        assert hasattr(COMMISSION_TABLES[firm], "confidence")
        assert hasattr(SWAP_TABLES[firm], "confidence")
        assert COMMISSION_TABLES[firm].confidence in ("high", "uncertain")
        assert SWAP_TABLES[firm].confidence in ("high", "uncertain")
