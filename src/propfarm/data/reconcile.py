"""Vendor reconciliation: Dukascopy ticks vs HistData 1-minute bars (Task 5.3).

Dukascopy (primary tick source) and HistData (secondary 1-minute cross-check)
both deliver historical FX data for the Phase-0 symbol set. Either vendor can
silently corrupt a slice of history — bad exchange feeds, off-by-one DST
shifts, or post-publication retroactive edits. Reconciliation scans both
vendors at the same minute granularity and flags any minute where OHLC
differs by more than ``threshold_bps`` (default 5 basis points).

The threshold is asymmetric to the cost model: 5 bps on EURUSD ≈ 0.5 pips,
which is one half-spread of the median quiet-hour quote. Anything quieter
than that is below the cost-model's resolution; anything noisier is a real
disagreement that warrants manual review before the simulator is run on
that window.

Trust anchors and contracts
---------------------------
* Reconciliation reads **snapshots** (hash-verified by ``snapshot.py``),
  not vendor APIs directly. This means a failing manifest hash would
  surface as a ``SnapshotIntegrityError`` *before* reconciliation begins,
  so any diff this module reports is a vendor-vs-vendor disagreement,
  not a local-corruption disagreement.
* The market-open filter is the canonical
  :func:`propfarm.data.quality.is_market_open` — we never reinvent
  session windows. Symbols not in :data:`SUPPORTED_SYMBOLS` raise
  ``ValueError`` via that function.
* All timestamps must be tz-aware UTC. Naive datetimes are rejected
  early; DST boundary minutes appear in both vendors as the same UTC
  instant, so the inner-join is naturally DST-safe.
* Production code uses only polars + pydantic. No pandas, no network.

Formula
-------
For each OHLC field we compute::

    bps = |a - b| / mid * 10_000,   where mid = (a + b) / 2

This is symmetric in the two vendors (neither is "ground truth"). Mid is
guaranteed strictly positive for FX/commodity prices, so division is safe.

Public API
----------
* :class:`ReconciliationFinding` — one flagged (symbol, ts, field) row.
* :class:`ReconciliationReport` — per-symbol summary + capped findings.
* :func:`aggregate_dukascopy_ticks_to_1m`
* :func:`reconcile_one_minute_bars`
* :func:`reconcile_snapshots`
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import is_market_open
from propfarm.data.snapshot import load_snapshot

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: OHLC fields compared between vendors. Order is pinned so finding ordering
#: is deterministic across runs (the report's "first 50" table is stable).
_OHLC_FIELDS: Final[tuple[Literal["open", "high", "low", "close"], ...]] = (
    "open",
    "high",
    "low",
    "close",
)

#: Column name used internally for the aggregated/joined minute timestamp.
_TS_COL: Final[str] = "ts"


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class ReconciliationFinding(BaseModel):
    """One vendor disagreement beyond the threshold.

    Attributes
    ----------
    symbol : str
        Trading symbol (matches :data:`SUPPORTED_SYMBOLS`).
    ts_utc : datetime.datetime
        Minute boundary in UTC (tz-aware).
    field : Literal["open", "high", "low", "close"]
        Which OHLC field disagreed.
    dukascopy_value : float
        Dukascopy's value for this field.
    histdata_value : float
        HistData's value for this field.
    abs_diff_bps : float
        ``|duka - hd| / mid * 10_000`` where ``mid = (duka + hd) / 2``.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts_utc: datetime
    field: Literal["open", "high", "low", "close"]
    dukascopy_value: float
    histdata_value: float
    abs_diff_bps: float


class ReconciliationReport(BaseModel):
    """Per-symbol reconciliation summary.

    The ``findings`` tuple is capped at ``max_findings`` (passed to
    :func:`reconcile_one_minute_bars`); the *summary* percentiles
    (``p50``/``p95``/``p99``/``max``) are computed over **all** breaches
    before truncation so a flood of small breaches cannot hide a large one.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    symbol: str
    n_minutes_compared: int
    n_minutes_market_open: int
    n_findings: int
    threshold_bps: float
    findings: tuple[ReconciliationFinding, ...]
    p50_diff_bps: float
    p95_diff_bps: float
    p99_diff_bps: float
    max_diff_bps: float


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_tz_aware_utc_ts(df: pl.DataFrame, label: str) -> None:
    """Reject DataFrames whose ``ts`` column is naive (no time_zone).

    Both vendors emit tz-aware UTC timestamps in production (see
    ``dukascopy.parse_bi5`` and ``histdata.parse_csv_bytes``). A naive
    column here means an upstream regression — fail loud rather than
    silently inner-joining on wall-clock minutes.
    """
    if _TS_COL not in df.columns:
        raise ValueError(f"{label}: missing required 'ts' column")
    dtype = df.schema[_TS_COL]
    if not isinstance(dtype, pl.Datetime):
        raise ValueError(f"{label}: 'ts' must be Datetime, got {dtype!r}")
    if dtype.time_zone is None:
        raise ValueError(
            f"{label}: 'ts' must be tz-aware UTC; got naive Datetime. "
            "Inner-joining naive vs tz-aware would silently mis-align DST minutes."
        )


# --------------------------------------------------------------------------- #
# Aggregation: tick mid -> 1m OHLC
# --------------------------------------------------------------------------- #
def aggregate_dukascopy_ticks_to_1m(duka_ticks: pl.DataFrame) -> pl.DataFrame:
    """Aggregate Dukascopy ticks into 1-minute OHLC bars on mid price.

    Each tick has a ``bid`` and ``ask``; we compute ``mid = (bid + ask) / 2``
    and resample to 1-minute floor boundaries. ``volume`` is the sum of
    ``bid_vol + ask_vol`` across the minute (a coarse activity proxy; the
    cost model does not consume this column, but the reconciliation report
    surfaces it as a sanity check).

    Parameters
    ----------
    duka_ticks : polars.DataFrame
        Columns ``ts`` (tz-aware UTC Datetime), ``bid``, ``ask``, optionally
        ``bid_vol`` / ``ask_vol``. Order need not be sorted — we sort
        deterministically inside the function so OHLC are stable.

    Returns
    -------
    polars.DataFrame
        Columns ``ts`` (tz-aware UTC, minute precision), ``open``, ``high``,
        ``low``, ``close``, ``volume``. One row per minute that contained at
        least one tick. Sorted ascending by ``ts``.

    Raises
    ------
    ValueError
        If ``ts`` is missing or not tz-aware Datetime, or ``bid``/``ask``
        columns are missing.
    """
    _require_tz_aware_utc_ts(duka_ticks, label="duka_ticks")
    for required in ("bid", "ask"):
        if required not in duka_ticks.columns:
            raise ValueError(f"duka_ticks: missing required column {required!r}")

    has_vol = "bid_vol" in duka_ticks.columns and "ask_vol" in duka_ticks.columns

    # Sort first so .first()/.last() resolve deterministically per minute.
    # Empty input fast-path: build the canonical empty schema.
    if duka_ticks.height == 0:
        return pl.DataFrame(
            schema={
                _TS_COL: duka_ticks.schema[_TS_COL],
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            }
        )

    with_mid = duka_ticks.sort(_TS_COL).with_columns(
        ((pl.col("bid") + pl.col("ask")) / 2.0).alias("_mid"),
        ((pl.col("bid_vol") + pl.col("ask_vol")) if has_vol else pl.lit(0.0)).alias("_vol"),
    )

    aggs = [
        pl.col("_mid").first().alias("open"),
        pl.col("_mid").max().alias("high"),
        pl.col("_mid").min().alias("low"),
        pl.col("_mid").last().alias("close"),
        pl.col("_vol").sum().cast(pl.Float64).alias("volume"),
    ]

    # group_by_dynamic floors timestamps to minute boundaries deterministically.
    # closed="left" matches the [t, t+1m) convention used by HistData M1 bars.
    return with_mid.group_by_dynamic(_TS_COL, every="1m", closed="left").agg(aggs).sort(_TS_COL)


# --------------------------------------------------------------------------- #
# Core reconciliation
# --------------------------------------------------------------------------- #
def _compute_bps_diff(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """Return |a - b| / mid * 10_000 with mid = (a + b) / 2.

    For positive FX/commodity prices, ``mid`` is strictly positive, so
    division never hits a zero denominator. We do not special-case
    ``a == b == 0`` because that case cannot arise from real vendor data
    and would itself be a data-corruption signal that *should* fail loud.
    """
    return (a - b).abs() / ((a + b) / 2.0) * 10_000.0


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (matches numpy's default 'linear' method).

    Implemented locally to avoid a numpy dependency in the production path
    and to keep the math fully visible: with ``n`` values sorted ascending,
    the q-th percentile (0 <= q <= 100) is at fractional index
    ``(n - 1) * q / 100``, interpolated between the bracketing values.

    Returns ``0.0`` for an empty input — there is no breach to report when
    no minutes were compared.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def reconcile_one_minute_bars(
    duka_1m: pl.DataFrame,
    histdata_1m: pl.DataFrame,
    *,
    symbol: str,
    threshold_bps: float = 5.0,
    max_findings: int = 1000,
) -> ReconciliationReport:
    """Reconcile two vendors' 1-minute OHLC bars for one symbol.

    Algorithm:

    1. Inner-join on ``ts`` (must be tz-aware UTC on both sides).
    2. Filter to minutes where :func:`is_market_open` returns ``True`` —
       a Saturday-minute disagreement is meaningless because no live
       strategy could have traded it.
    3. For each OHLC field, compute ``|duka - hd| / mid * 10_000``.
    4. Flag rows whose field-bps exceeds ``threshold_bps``.
    5. Sort findings by ``abs_diff_bps`` descending and truncate to
       ``max_findings``.

    Parameters
    ----------
    duka_1m, histdata_1m : polars.DataFrame
        Both must have columns ``ts`` (tz-aware UTC Datetime), ``open``,
        ``high``, ``low``, ``close``.
    symbol : str
        Must be a member of :data:`SUPPORTED_SYMBOLS` (validated via
        :func:`is_market_open`).
    threshold_bps : float
        Per-field disagreement above which a row is flagged. Default 5 bps.
    max_findings : int
        Hard cap on the returned ``findings`` tuple. The summary
        percentiles are computed over *all* breaches before truncation,
        so a flood of small breaches cannot hide a large one.

    Returns
    -------
    ReconciliationReport
        See class docstring.

    Raises
    ------
    ValueError
        If either DataFrame's ``ts`` is naive, or if the symbol is unknown
        (the latter surfaces via :func:`is_market_open`).
    """
    _require_tz_aware_utc_ts(duka_1m, label="duka_1m")
    _require_tz_aware_utc_ts(histdata_1m, label="histdata_1m")

    # Inner-join on ts. Both sides are tz-aware UTC, so the join is naturally
    # DST-safe — a 02:30 BST minute and a 01:30 UTC minute are the same row.
    duka = duka_1m.select([_TS_COL, "open", "high", "low", "close"]).rename(
        {f: f"duka_{f}" for f in _OHLC_FIELDS}
    )
    hd = histdata_1m.select([_TS_COL, "open", "high", "low", "close"]).rename(
        {f: f"hd_{f}" for f in _OHLC_FIELDS}
    )
    joined = duka.join(hd, on=_TS_COL, how="inner").sort(_TS_COL)
    n_minutes_compared = joined.height

    if n_minutes_compared == 0:
        return ReconciliationReport(
            symbol=symbol,
            n_minutes_compared=0,
            n_minutes_market_open=0,
            n_findings=0,
            threshold_bps=threshold_bps,
            findings=(),
            p50_diff_bps=0.0,
            p95_diff_bps=0.0,
            p99_diff_bps=0.0,
            max_diff_bps=0.0,
        )

    # Validate the symbol once via a known-non-naive minute. is_market_open
    # raises ValueError on unknown symbols; we want that to surface before
    # iterating, not midway through.
    sample_ts = joined.item(0, _TS_COL)
    # Polars returns a Python datetime here (tz-aware UTC). The call below
    # raises if `symbol` is not in SUPPORTED_SYMBOLS, which is exactly the
    # behavior the spec demands.
    is_market_open(symbol, sample_ts)

    # Vectorized market-open mask. We extract ts to a Python list once and
    # iterate in Python — is_market_open is a per-symbol stateful classifier
    # (DST/holiday/session), faster to call directly than to push into polars.
    ts_list: list[datetime] = joined.get_column(_TS_COL).to_list()
    open_mask = [is_market_open(symbol, t) for t in ts_list]
    joined = joined.with_columns(pl.Series("_open", open_mask, dtype=pl.Boolean))
    open_only = joined.filter(pl.col("_open"))
    n_minutes_market_open = open_only.height

    # Compute per-field bps. We keep all four columns so we can melt below.
    bps_cols = [
        _compute_bps_diff(pl.col(f"duka_{f}"), pl.col(f"hd_{f}")).alias(f"bps_{f}")
        for f in _OHLC_FIELDS
    ]
    enriched = open_only.with_columns(bps_cols)

    # Unpivot to long form: one row per (ts, field). Polars' melt was renamed
    # to unpivot in 1.0+; we use the modern name.
    long = enriched.unpivot(
        on=[f"bps_{f}" for f in _OHLC_FIELDS],
        index=[_TS_COL] + [f"duka_{f}" for f in _OHLC_FIELDS] + [f"hd_{f}" for f in _OHLC_FIELDS],
        variable_name="_field_col",
        value_name="abs_diff_bps",
    ).with_columns(
        pl.col("_field_col").str.replace("^bps_", "").alias("field"),
    )

    # Match the OHLC value of the field that's being compared on this row.
    long = long.with_columns(
        pl.when(pl.col("field") == "open")
        .then(pl.col("duka_open"))
        .when(pl.col("field") == "high")
        .then(pl.col("duka_high"))
        .when(pl.col("field") == "low")
        .then(pl.col("duka_low"))
        .otherwise(pl.col("duka_close"))
        .alias("dukascopy_value"),
        pl.when(pl.col("field") == "open")
        .then(pl.col("hd_open"))
        .when(pl.col("field") == "high")
        .then(pl.col("hd_high"))
        .when(pl.col("field") == "low")
        .then(pl.col("hd_low"))
        .otherwise(pl.col("hd_close"))
        .alias("histdata_value"),
    )

    breaches = long.filter(pl.col("abs_diff_bps") > threshold_bps).sort(
        "abs_diff_bps", descending=True
    )
    n_findings = breaches.height

    # Percentiles over ALL per-field bps (including non-breach minutes) —
    # this gives a fair "typical disagreement" headline that does not change
    # just because the threshold was bumped.
    all_bps: list[float] = long.get_column("abs_diff_bps").to_list()

    findings_tuple: tuple[ReconciliationFinding, ...] = tuple(
        ReconciliationFinding(
            symbol=symbol,
            ts_utc=row[_TS_COL],
            field=row["field"],
            dukascopy_value=float(row["dukascopy_value"]),
            histdata_value=float(row["histdata_value"]),
            abs_diff_bps=float(row["abs_diff_bps"]),
        )
        for row in breaches.head(max_findings).iter_rows(named=True)
    )

    return ReconciliationReport(
        symbol=symbol,
        n_minutes_compared=n_minutes_compared,
        n_minutes_market_open=n_minutes_market_open,
        n_findings=n_findings,
        threshold_bps=threshold_bps,
        findings=findings_tuple,
        p50_diff_bps=_percentile(all_bps, 50.0),
        p95_diff_bps=_percentile(all_bps, 95.0),
        p99_diff_bps=_percentile(all_bps, 99.0),
        max_diff_bps=max(all_bps) if all_bps else 0.0,
    )


# --------------------------------------------------------------------------- #
# Snapshot wiring
# --------------------------------------------------------------------------- #
def reconcile_snapshots(
    *,
    symbol: str,
    duka_snapshot_name: str | None = None,
    histdata_snapshot_name: str | None = None,
    snapshot_root: Path | None = None,
    threshold_bps: float = 5.0,
    max_findings: int = 1000,
) -> ReconciliationReport:
    """Load both vendors' snapshots and reconcile them.

    The Dukascopy snapshot is expected to contain tick-granularity rows
    (``ts``, ``bid``, ``ask``, ``bid_vol``, ``ask_vol``) — they are
    aggregated to 1-minute OHLC on mid price inside this function.
    The HistData snapshot is expected to already be at 1-minute OHLC
    (``ts``, ``open``, ``high``, ``low``, ``close``, ``volume``).

    Snapshot names default to the convention used by Task 4.2's ingest:

    * Dukascopy: ``f"dukascopy_{symbol.lower()}_ticks"``
    * HistData:  ``f"histdata_{symbol.lower()}_1m"``

    Parameters
    ----------
    symbol : str
        Phase-0 trading symbol.
    duka_snapshot_name, histdata_snapshot_name : str, optional
        Override the default snapshot names.
    snapshot_root : pathlib.Path, optional
        Forwarded to :func:`load_snapshot`. ``None`` resolves to the repo
        root, matching production. Tests pass a temp directory.
    threshold_bps : float
        Forwarded to :func:`reconcile_one_minute_bars`.
    max_findings : int
        Forwarded to :func:`reconcile_one_minute_bars`.

    Returns
    -------
    ReconciliationReport

    Raises
    ------
    FileNotFoundError, KeyError, SnapshotIntegrityError
        Propagated from :func:`load_snapshot` for missing files, missing
        manifest entries, or hash drift respectively.
    ValueError
        Propagated from :func:`reconcile_one_minute_bars` (naive ts) or
        :func:`is_market_open` (unknown symbol).
    """
    duka_name = duka_snapshot_name or f"dukascopy_{symbol.lower()}_ticks"
    hd_name = histdata_snapshot_name or f"histdata_{symbol.lower()}_1m"

    duka_ticks = load_snapshot(duka_name, root=snapshot_root)
    histdata_1m = load_snapshot(hd_name, root=snapshot_root)

    duka_1m = aggregate_dukascopy_ticks_to_1m(duka_ticks)

    return reconcile_one_minute_bars(
        duka_1m,
        histdata_1m,
        symbol=symbol,
        threshold_bps=threshold_bps,
        max_findings=max_findings,
    )


__all__ = [
    "ReconciliationFinding",
    "ReconciliationReport",
    "aggregate_dukascopy_ticks_to_1m",
    "reconcile_one_minute_bars",
    "reconcile_snapshots",
]
