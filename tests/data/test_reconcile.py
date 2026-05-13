"""Tests for ``propfarm.data.reconcile`` (Task 5.3).

All tests are 100% offline — no network, no real snapshots. Synthetic
polars DataFrames are built in-test so the bps math is exercised against
deliberately constructed disagreements.

The acceptance contract under test:

* The bps formula is ``|a - b| / mid * 10_000`` where ``mid = (a + b) / 2``.
* The market-open filter is the canonical
  :func:`propfarm.data.quality.is_market_open` — a Saturday minute present
  in both vendors must NOT appear in the comparison.
* Inner-join on ``ts`` means non-overlapping ranges are silently trimmed.
* Naive ``ts`` columns raise immediately (no silent DST mis-alignment).
* Unknown symbols raise via :func:`is_market_open`.
* The findings tuple is capped at ``max_findings`` *and* sorted by
  ``abs_diff_bps`` descending so the loudest disagreements survive truncation.
* Percentiles match numpy's ``np.percentile(..., method='linear')`` baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from propfarm.data.reconcile import (
    ReconciliationReport,
    aggregate_dukascopy_ticks_to_1m,
    reconcile_one_minute_bars,
)

# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #
_OHLC = ("open", "high", "low", "close")


def _minute_index_utc(start: datetime, n: int) -> list[datetime]:
    """Return ``n`` consecutive UTC minutes starting at ``start`` (inclusive)."""
    return [start + timedelta(minutes=i) for i in range(n)]


def _ohlc_frame(ts_list: list[datetime], price: float) -> pl.DataFrame:
    """Build an OHLC DataFrame where O=H=L=C=price for every minute."""
    n = len(ts_list)
    return pl.DataFrame(
        {
            "ts": ts_list,
            "open": [price] * n,
            "high": [price] * n,
            "low": [price] * n,
            "close": [price] * n,
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )


# --------------------------------------------------------------------------- #
# 1. Tick aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_ticks_to_1m() -> None:
    """60 ticks across one minute aggregate to: open=first, high=max,
    low=min, close=last, volume=sum."""
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)  # Monday — market open
    # 60 ticks at 1-second spacing with a deliberate min/max in the middle.
    ts = [base + timedelta(seconds=s) for s in range(60)]

    # Symmetric bid/ask around a mid that walks 1.1000 -> 1.1005 -> 1.1001
    # via a max at second 20 and a min at second 40.
    def _bump(s: int) -> float:
        if s == 20:
            return 5.0
        if s == 40:
            return 0.0
        return 1.0 if s < 20 else 2.0

    mids = [1.1000 + 0.0001 * _bump(s) for s in range(60)]
    spread = 0.00002
    bids = [m - spread / 2 for m in mids]
    asks = [m + spread / 2 for m in mids]

    duka = pl.DataFrame(
        {
            "ts": ts,
            "bid": bids,
            "ask": asks,
            "bid_vol": [1.0] * 60,
            "ask_vol": [2.0] * 60,
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
            "bid_vol": pl.Float64,
            "ask_vol": pl.Float64,
        },
    )
    # Use price_source="mid" explicitly here — this test exercises the OHLC
    # aggregation mechanics; the bid/ask/mid switch is tested separately.
    out = aggregate_dukascopy_ticks_to_1m(duka, price_source="mid")
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["open"] == pytest.approx(mids[0])
    assert row["close"] == pytest.approx(mids[59])
    assert row["high"] == pytest.approx(max(mids))
    assert row["low"] == pytest.approx(min(mids))
    # Volume = sum of (bid_vol + ask_vol) across all 60 ticks = 60 * 3.0.
    assert row["volume"] == pytest.approx(180.0)


def test_aggregate_handles_unsorted_input() -> None:
    """Aggregation must sort by ts before computing first/last so OHLC is
    deterministic regardless of input ordering."""
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    ts = [base, base + timedelta(seconds=30), base + timedelta(seconds=15)]
    # Reversed-ish ordering on purpose; mid prices encode the true sequence.
    mids = [1.10000, 1.10020, 1.10010]
    spread = 0.00002
    duka = pl.DataFrame(
        {
            "ts": ts,
            "bid": [m - spread / 2 for m in mids],
            "ask": [m + spread / 2 for m in mids],
            "bid_vol": [1.0, 1.0, 1.0],
            "ask_vol": [1.0, 1.0, 1.0],
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
            "bid_vol": pl.Float64,
            "ask_vol": pl.Float64,
        },
    )
    out = aggregate_dukascopy_ticks_to_1m(duka, price_source="mid")
    row = out.row(0, named=True)
    # Sorted order: 1.10000 (open) -> 1.10010 -> 1.10020 (close).
    assert row["open"] == pytest.approx(1.10000)
    assert row["close"] == pytest.approx(1.10020)


# --------------------------------------------------------------------------- #
# 2. Clean pass (no disagreement)
# --------------------------------------------------------------------------- #
def test_reconcile_clean_pass() -> None:
    """Identical OHLC across both vendors → 0 findings, p95 ≈ 0."""
    ts = _minute_index_utc(datetime(2024, 6, 3, 10, 0, tzinfo=UTC), 10)
    duka = _ohlc_frame(ts, 1.10000)
    hd = _ohlc_frame(ts, 1.10000)

    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")
    assert isinstance(report, ReconciliationReport)
    assert report.n_findings == 0
    assert report.findings == ()
    assert report.p95_diff_bps == pytest.approx(0.0, abs=1e-9)
    assert report.max_diff_bps == pytest.approx(0.0, abs=1e-9)
    assert report.threshold_bps == 5.0


# --------------------------------------------------------------------------- #
# 3. Threshold: 5.5 bps must flag
# --------------------------------------------------------------------------- #
def test_reconcile_threshold_5bps() -> None:
    """Dukascopy close 1.10000 vs HistData 1.10061 → ~5.54 bps, above 5.0
    threshold → exactly one finding on the close field.

    Math: 0.00061 / 1.100305 * 10_000 ≈ 5.544 bps.
    """
    ts = _minute_index_utc(datetime(2024, 6, 3, 10, 0, tzinfo=UTC), 1)
    duka = _ohlc_frame(ts, 1.10000)
    hd = _ohlc_frame(ts, 1.10000)
    # Bump only HistData's close.
    hd = hd.with_columns(pl.Series("close", [1.10061]))

    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")
    assert report.n_findings == 1
    finding = report.findings[0]
    assert finding.field == "close"
    assert finding.dukascopy_value == pytest.approx(1.10000)
    assert finding.histdata_value == pytest.approx(1.10061)

    # Hand-computed bps: |1.10000 - 1.10061| / ((1.10000 + 1.10061) / 2) * 1e4
    expected = abs(1.10000 - 1.10061) / ((1.10000 + 1.10061) / 2) * 10_000.0
    assert finding.abs_diff_bps == pytest.approx(expected, rel=1e-9)
    assert expected > 5.0  # sanity: above threshold


# --------------------------------------------------------------------------- #
# 4. Just under threshold
# --------------------------------------------------------------------------- #
def test_reconcile_just_under_threshold() -> None:
    """1.10000 vs 1.10040 → ~3.6 bps, below 5.0 → 0 findings."""
    ts = _minute_index_utc(datetime(2024, 6, 3, 10, 0, tzinfo=UTC), 1)
    duka = _ohlc_frame(ts, 1.10000)
    hd = _ohlc_frame(ts, 1.10000).with_columns(pl.Series("close", [1.10040]))

    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")
    assert report.n_findings == 0

    # Sanity check on the math: |0.00040| / 1.10020 * 1e4 ≈ 3.63 bps < 5.
    expected = abs(1.10000 - 1.10040) / ((1.10000 + 1.10040) / 2) * 10_000.0
    assert expected < 5.0
    # The percentile summary still records the disagreement, just below threshold.
    assert report.max_diff_bps == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------- #
# 5. Market-open filter — Saturday minute is dropped
# --------------------------------------------------------------------------- #
def test_market_open_filter() -> None:
    """A Saturday minute present in both vendors must be excluded from the
    comparison (FX market is closed Sat). The disagreement on it must not
    surface as a finding."""
    # 2024-06-01 is a Saturday (Sun=2024-06-02; FX reopens Sun 22:00 UTC).
    saturday = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    monday = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    ts = [saturday, monday]

    duka = _ohlc_frame(ts, 1.10000)
    hd = _ohlc_frame(ts, 1.10000)
    # Inject a >5 bps disagreement on the Saturday minute only.
    hd = hd.with_columns(pl.Series("close", [1.10100, 1.10000]))

    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")
    assert report.n_minutes_compared == 2  # both joined
    assert report.n_minutes_market_open == 1  # only Monday survives the filter
    assert report.n_findings == 0  # Saturday's huge disagreement is dropped


# --------------------------------------------------------------------------- #
# 6. max_findings cap
# --------------------------------------------------------------------------- #
def test_max_findings_cap() -> None:
    """5000 mismatches with max_findings=100 → exactly 100 findings, sorted
    by abs_diff_bps descending so the loudest survive truncation."""
    n = 5000
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    ts = _minute_index_utc(base, n)

    # Strictly increasing disagreement on close: i bps-ish per row.
    base_price = 1.10000
    duka_close = [base_price] * n
    # Shift HistData close by a varying amount so bps differs row to row.
    # delta = (i + 1) * 1e-5  → bps ≈ (i+1)/11 ish; all > 5 bps for i >= 60.
    deltas = [(i + 1) * 1e-5 for i in range(n)]
    hd_close = [base_price + d for d in deltas]

    duka = pl.DataFrame(
        {
            "ts": ts,
            "open": duka_close,
            "high": duka_close,
            "low": duka_close,
            "close": duka_close,
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )
    hd = pl.DataFrame(
        {
            "ts": ts,
            "open": duka_close,  # match
            "high": duka_close,
            "low": duka_close,
            "close": hd_close,
        },
        schema=duka.schema,
    )

    report = reconcile_one_minute_bars(
        duka, hd, symbol="EURUSD", threshold_bps=5.0, max_findings=100
    )
    assert len(report.findings) == 100
    # n_findings reflects the *true* count, not the truncated count.
    assert report.n_findings > 100

    # Findings are sorted by abs_diff_bps descending.
    diffs = [f.abs_diff_bps for f in report.findings]
    assert diffs == sorted(diffs, reverse=True)
    # The loudest finding must be the last row (largest delta).
    assert report.findings[0].ts_utc == ts[-1]


# --------------------------------------------------------------------------- #
# 7. p95 matches numpy
# --------------------------------------------------------------------------- #
def test_p95_diff_bps_calculation() -> None:
    """The p50/p95/p99/max headline percentiles match numpy's linear
    interpolation method against 100 known per-minute close-only diffs."""
    n = 100
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    ts = _minute_index_utc(base, n)
    base_price = 1.10000
    deltas = [(i + 1) * 1e-6 for i in range(n)]  # tiny — almost all under threshold
    duka_close = [base_price] * n
    hd_close = [base_price + d for d in deltas]

    duka = pl.DataFrame(
        {
            "ts": ts,
            "open": duka_close,
            "high": duka_close,
            "low": duka_close,
            "close": duka_close,
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )
    hd = pl.DataFrame(
        {
            "ts": ts,
            "open": duka_close,  # match → 0 bps
            "high": duka_close,
            "low": duka_close,
            "close": hd_close,
        },
        schema=duka.schema,
    )

    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")

    # Build the same long-form set of per-field bps the implementation
    # computes: 100 rows x 4 fields = 400 entries. Three fields are 0 bps
    # (O/H/L match exactly), close diverges.
    all_bps: list[float] = []
    for d in deltas:
        for field_delta in (0.0, 0.0, 0.0, d):  # O, H, L, C
            a = base_price
            b = base_price + field_delta
            mid = (a + b) / 2
            all_bps.append(abs(a - b) / mid * 10_000.0)

    arr = np.array(all_bps)
    assert report.p50_diff_bps == pytest.approx(float(np.percentile(arr, 50)), rel=1e-9)
    assert report.p95_diff_bps == pytest.approx(float(np.percentile(arr, 95)), rel=1e-9)
    assert report.p99_diff_bps == pytest.approx(float(np.percentile(arr, 99)), rel=1e-9)
    assert report.max_diff_bps == pytest.approx(float(arr.max()), rel=1e-9)


# --------------------------------------------------------------------------- #
# 8. Inner-join semantics
# --------------------------------------------------------------------------- #
def test_inner_join_on_ts() -> None:
    """Dukascopy covers minutes 1-10, HistData covers 5-15 → 6 overlapping
    minutes (5, 6, 7, 8, 9, 10)."""
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    duka_ts = _minute_index_utc(base, 10)  # 0..9 offset from base
    hd_ts = [base + timedelta(minutes=i) for i in range(4, 15)]  # 4..14

    duka = _ohlc_frame(duka_ts, 1.10000)
    hd = _ohlc_frame(hd_ts, 1.10000)
    report = reconcile_one_minute_bars(duka, hd, symbol="EURUSD")
    # Overlap = {4,5,6,7,8,9} → 6 minutes.
    assert report.n_minutes_compared == 6


# --------------------------------------------------------------------------- #
# 9. Naive ts raises
# --------------------------------------------------------------------------- #
def test_tz_aware_required() -> None:
    """Naive (tz-unaware) ts column must raise ValueError immediately."""
    naive_ts = [datetime(2024, 6, 3, 10, 0)]  # NO tzinfo
    duka = pl.DataFrame(
        {
            "ts": naive_ts,
            "open": [1.10000],
            "high": [1.10000],
            "low": [1.10000],
            "close": [1.10000],
        }
    )
    aware_ts = [datetime(2024, 6, 3, 10, 0, tzinfo=UTC)]
    hd = _ohlc_frame(aware_ts, 1.10000)

    with pytest.raises(ValueError, match="tz-aware"):
        reconcile_one_minute_bars(duka, hd, symbol="EURUSD")

    # Symmetric: naive on the HistData side also raises.
    duka_ok = _ohlc_frame(aware_ts, 1.10000)
    hd_naive = pl.DataFrame(
        {
            "ts": naive_ts,
            "open": [1.10000],
            "high": [1.10000],
            "low": [1.10000],
            "close": [1.10000],
        }
    )
    with pytest.raises(ValueError, match="tz-aware"):
        reconcile_one_minute_bars(duka_ok, hd_naive, symbol="EURUSD")


# --------------------------------------------------------------------------- #
# 10. Unknown symbol
# --------------------------------------------------------------------------- #
def test_unknown_symbol_raises() -> None:
    """Passing a symbol outside SUPPORTED_SYMBOLS must raise via is_market_open."""
    ts = _minute_index_utc(datetime(2024, 6, 3, 10, 0, tzinfo=UTC), 3)
    duka = _ohlc_frame(ts, 1.10000)
    hd = _ohlc_frame(ts, 1.10000)
    with pytest.raises(ValueError, match="unknown symbol"):
        reconcile_one_minute_bars(duka, hd, symbol="ZZZ")


# --------------------------------------------------------------------------- #
# Bonus: end-to-end via aggregate_dukascopy_ticks_to_1m -> reconcile
# --------------------------------------------------------------------------- #
def test_end_to_end_tick_to_reconcile() -> None:
    """Round-trip: build synthetic ticks at second granularity, aggregate
    to 1-minute, reconcile against an artificially perturbed HistData frame.

    Confirms the integration of the two public helpers and that the
    aggregated mid feeds into reconciliation without schema drift.
    """
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    # Two minutes of ticks at one-second granularity. Constant mid in each.
    ts_ticks: list[datetime] = []
    bids: list[float] = []
    asks: list[float] = []
    for minute_idx, mid in enumerate([1.10000, 1.10010]):
        for s in range(60):
            ts_ticks.append(base + timedelta(minutes=minute_idx, seconds=s))
            bids.append(mid - 0.00001)
            asks.append(mid + 0.00001)
    duka_ticks = pl.DataFrame(
        {
            "ts": ts_ticks,
            "bid": bids,
            "ask": asks,
            "bid_vol": [1.0] * len(ts_ticks),
            "ask_vol": [1.0] * len(ts_ticks),
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
            "bid_vol": pl.Float64,
            "ask_vol": pl.Float64,
        },
    )
    duka_1m = aggregate_dukascopy_ticks_to_1m(duka_ticks)
    assert duka_1m.height == 2

    hd_ts = [base, base + timedelta(minutes=1)]
    # Perturb the second minute beyond threshold to force one finding.
    hd = pl.DataFrame(
        {
            "ts": hd_ts,
            "open": [1.10000, 1.10100],  # large disagreement on minute 2 open
            "high": [1.10000, 1.10010],
            "low": [1.10000, 1.10010],
            "close": [1.10000, 1.10010],
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )

    report = reconcile_one_minute_bars(duka_1m, hd, symbol="EURUSD")
    assert report.n_findings >= 1
    # The loudest finding should be on the open field of minute 2.
    top = report.findings[0]
    assert top.field == "open"
    assert top.ts_utc == base + timedelta(minutes=1)


def test_aggregate_default_is_bid_price() -> None:
    """Default price_source is "bid" to match the HistData ASCII M1 FX convention.

    W6a reviewer flagged that aggregating Dukascopy ticks to mid against
    HistData bid bars introduces a systemic half-spread offset on every
    minute (~0.5 bps on EURUSD; can approach the 5-bps reconciliation
    threshold during spread-widening events and produce false positives).
    The default switched to "bid"; this test locks it.
    """
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    ts = [base + timedelta(seconds=s) for s in range(3)]
    duka = pl.DataFrame(
        {
            "ts": ts,
            "bid": [1.10000, 1.10010, 1.10020],
            "ask": [1.10002, 1.10012, 1.10022],
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
        },
    )
    out = aggregate_dukascopy_ticks_to_1m(duka)  # no price_source kwarg -> default
    row = out.row(0, named=True)
    # If default were "mid", open would be 1.10001 (mid of first tick).
    # If default is "bid", open is 1.10000 (bid of first tick).
    assert row["open"] == pytest.approx(1.10000), (
        "Default price_source must be 'bid' to match HistData M1 FX convention"
    )
    assert row["close"] == pytest.approx(1.10020)


def test_aggregate_ask_price_source() -> None:
    """price_source='ask' uses the ask column for OHLC."""
    base = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    duka = pl.DataFrame(
        {
            "ts": [base, base + timedelta(seconds=1)],
            "bid": [1.10000, 1.10010],
            "ask": [1.10002, 1.10012],
        },
        schema={
            "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
            "bid": pl.Float64,
            "ask": pl.Float64,
        },
    )
    out = aggregate_dukascopy_ticks_to_1m(duka, price_source="ask")
    row = out.row(0, named=True)
    assert row["open"] == pytest.approx(1.10002)
    assert row["close"] == pytest.approx(1.10012)
