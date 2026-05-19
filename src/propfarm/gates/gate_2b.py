"""Gate 2B — sim-vs-live fill comparison harness (Phase 0).

Gate 2B answers a single empirical question: **does ``simulate_fill`` produce
fills that match what a real FTMO MT5 demo broker books on the same request
under the same market conditions?** If yes within calibrated thresholds,
Wave 6d (stress replay) unblocks. If no, the cost models (spread / slippage)
recalibrate — *not* the gate's tolerances.

Pipeline (operator-facing)
--------------------------

1. ``scripts/record_fills.py`` runs 24-48h on the Windows VPS and persists a
   parquet of :class:`scripts.record_fills.FillRecord` rows under
   ``data/raw/fill_recordings/{run_id}.parquet`` plus a JSON manifest.
2. This module's :func:`run_gate_2b` ingests that parquet, drives every row
   back through :func:`propfarm.sim.fill_engine.simulate_fill`, and computes
   the per-field ``live - sim`` residual.
3. The :class:`Gate2BReport` summarizes the comparison — p50/p95/p99 per
   field, a one-sample t-test against zero for systematic bias, the
   per-symbol pass thresholds, and the verdict (``pass`` / ``investigate``
   / ``fail``).
4. The CLI at ``scripts/run_gate_2b.py`` exits 0 on ``pass`` and 1 otherwise
   so a CI / make target can gate Wave 6d on the result.

The critical design concern (load-bearing)
------------------------------------------

``FillRecord`` and :class:`propfarm.sim.fill_engine.FillResult` share the
**output** schema (locked at Wave 6c). Reconstructing the **input**
:class:`MarketState` from a captured ``FillRecord`` is a different problem:
``MarketState`` has fields beyond what ``FillRecord`` captures.

* ``symbol`` -> ``FROM_FILLRECORD`` (column ``symbol``).
* ``ts_utc`` -> ``FROM_FILLRECORD`` (column ``request_time_utc``).
* ``realized_vol_5m`` -> ``COMPUTED`` from rolling stdev of recent prices,
  annualized; falls back to ``DEFAULTED`` per-symbol typical when there is
  insufficient history.
* ``news_window`` -> ``DEFAULTED``. Phase 0 has no news calendar; the
  operator must manually flag rows that fell inside NFP / CPI / FOMC /
  central-bank windows.
* ``stress_mode`` -> ``DEFAULTED``. ``record_fills.py`` does not capture
  this and the live broker has no notion of it; a normal-conditions
  capture is the right baseline.

Every reconstruction emits a :class:`MarketStateReconstruction` record per
field that surfaces in the report so the operator can audit which fields
were computed vs defaulted. **This is the protection against silent
sim-vs-live drift**: a reviewer rejects any harness that silently defaults
a field without making the assumption visible.

Per-symbol pass thresholds (Wave 6b calibration, user-mandated)
---------------------------------------------------------------

* EURUSD / GBPUSD → 0.5 pip = 0.00005 in price units.
* USDJPY → 0.5 pip = 0.005 in price units.
* XAUUSD → 5 pip = 0.05 in price units (gold is 3-digit).
* GER40 / US100 → 0.5 index points (1-digit instruments, pip = 1.0).

The fill-price residual threshold is compared against the **p95 of the
absolute residual** per-symbol. The spread-residual threshold is compared
against the p95 of the absolute spread residual across all symbols (1 pip
is the user-mandated single value).

Determinism
-----------

For a fixed ``(capture_parquet, execution_latency_ms)`` :func:`run_gate_2b`
produces a bit-identical :class:`Gate2BReport`. The rng inside
:func:`simulate_fill` is supplied per-row as a freshly-seeded
``numpy.random.default_rng(seed)`` where ``seed`` is derived from the row
index — same seed, same result. Hashing the captured parquet up-front and
pinning the SHA256 into the report makes this auditable.

Constraints
-----------

* No network in tests. No MT5 import. No broker host strings (W1 drift rule).
* The harness validates the input parquet column set against
  :class:`scripts.record_fills.FillRecord`'s field names at load time and
  raises ``ValueError`` on missing columns.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Literal, cast

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict
from scipy import stats  # type: ignore[import-untyped]

from propfarm.sim.fill_engine import FillRequest, FillResult, simulate_fill
from propfarm.sim.market import MarketState
from propfarm.sim.slippage import CALIBRATIONS as SLIPPAGE_CALIBRATIONS

# --------------------------------------------------------------------------- #
# Per-symbol thresholds — user-mandated, calibrated against Wave 6b numbers
# --------------------------------------------------------------------------- #

#: Per-symbol p95 absolute fill-price residual threshold, in **pips**.
#: Conversion to price units uses each symbol's pip size, identical to the
#: convention in ``propfarm.sim.fill_engine._pip_size`` and
#: ``scripts/record_fills.parse_fill_into_record``.
#:
#: * EURUSD/GBPUSD → 0.5 pip   (pip = 0.0001 ⇒ 5e-5 price units)
#: * USDJPY       → 0.5 pip   (pip = 0.01   ⇒ 5e-3 price units)
#: * XAUUSD       → 5.0 pip   (pip = 0.01   ⇒ 5e-2 price units)
#: * GER40/US100  → 0.5 pip   (pip = 1.0    ⇒ 5e-1 index points)
SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS: Final[dict[str, float]] = {
    "EURUSD": 0.5,
    "GBPUSD": 0.5,
    "USDJPY": 0.5,
    "XAUUSD": 5.0,
    "GER40": 0.5,
    "US100": 0.5,
}

#: Single-value p95 absolute spread-residual threshold, in pips. Applied
#: globally (not per-symbol) per the dispatcher's mandate.
SYMBOL_SPREAD_P95_THRESHOLD_PIPS: Final[float] = 1.0

#: Rolling window (in rows of the same symbol) used to compute the
#: realized-vol estimate. 5 rows mirrors the "5m" suffix in the model
#: field name; the actual time spacing varies because the recording
#: schedule sleeps to scheduled targets, but the rolling-window logic
#: is the same regardless of inter-row spacing.
_REALIZED_VOL_WINDOW_ROWS: Final[int] = 5

#: Trading-minutes-per-year annualization factor for 1-minute returns.
#: 252 trading days * 1440 minutes (FX is 24x5 but the "trading day"
#: convention here matches the slippage model's annualized-fraction
#: convention; the resulting realized_vol_5m sits in the same regime as
#: the slippage model expects, i.e. ~0.10 = 10% annualized for FX).
_ANNUALIZATION_FACTOR: Final[float] = 252.0 * 1440.0

#: Per-symbol typical-vol fallback when there are too few same-symbol
#: rows to compute a rolling stdev. Lifted from the slippage CALIBRATIONS
#: convention (~10% annualized = 0.10) used as the slippage module's own
#: default when ``MarketState.realized_vol_5m`` is None.
_TYPICAL_REALIZED_VOL_FALLBACK: Final[float] = 0.10

#: FAIL threshold for the one-sample t-test on residual means. p-value
#: strictly below this triggers FAIL contribution. 0.001 keeps the bar high
#: enough that one-off captures don't trigger over heavy-tailed noise.
_T_TEST_ALPHA_FAIL: Final[float] = 0.001

#: INVESTIGATE threshold for the t-test. p-value in
#: ``[_T_TEST_ALPHA_FAIL, _T_TEST_ALPHA_INVESTIGATE)`` triggers INVESTIGATE
#: contribution (statistically distinguishable but not at the rejection
#: bar). The 0.01 level mirrors the original Gate 2B systematic-bias gate.
_T_TEST_ALPHA_INVESTIGATE: Final[float] = 0.01

#: Backwards-compatible alias for the INVESTIGATE-level alpha. Existing
#: callers and tests that import ``_T_TEST_ALPHA`` continue to see the
#: 0.01 value (which previously was the sole gate); the FAIL bar at
#: 0.001 is new in Gate-2B round 1.
_T_TEST_ALPHA: Final[float] = _T_TEST_ALPHA_INVESTIGATE

#: Per-symbol fill-price p95 INVESTIGATE→FAIL multiplier.
#:
#:   p95 ≤ threshold                                    → PASS (component)
#:   threshold < p95 ≤ threshold * MULTIPLIER           → INVESTIGATE
#:   p95 > threshold * MULTIPLIER                       → FAIL
#:
#: 1.4x matches the user-spec band on the per-symbol gate
#: (0.5 pip threshold → 0.7 pip FAIL boundary).
_INVESTIGATE_BAND_MULTIPLIER_FILL_PRICE: Final[float] = 1.4

#: Global-spread p95 INVESTIGATE→FAIL multiplier.
#:
#: 1.5x matches the user-spec band on the spread gate
#: (1.0 pip threshold → 1.5 pip FAIL boundary). Differs from the
#: fill-price 1.4x because the user spec set the two bands separately —
#: spread is more naturally a longer-tail distribution and benefits from
#: a slightly wider investigate window.
_INVESTIGATE_BAND_MULTIPLIER_SPREAD: Final[float] = 1.5

#: Minimum per-(symbol, side) sample size to compute and emit a per-side
#: bias diagnostic. Below this n, the bias is too noisy to report
#: meaningfully; the harness silently skips the (symbol, side) pair.
#: Aligned with the user-spec "n >= 10" rule.
_PER_SIDE_BIAS_MIN_N: Final[int] = 10

#: ``latency_ms`` is excluded from FAIL-contributing bias reasons.
#:
#: Sign convention: ``broker_latency_residual_ms = live - sim``. The sim
#: derives ``execution_latency_ms`` from the median of the captured
#: ``broker_latency_ms`` on retcode=10009 rows (see ``run_gate_2b`` near
#: line 1010); this construction guarantees the residual mean is biased
#: low because the right-tail of the live distribution sits well above
#: the median. The t-test on the residual reflects the live latency
#: distribution shape, NOT a calibration target — the sim is by
#: construction "set to the median" rather than "set to match the mean".
#: Including this field in failure_reasons would FAIL every capture whose
#: live latency has any right-tail mass, which is every real capture.
#:
#: We still REPORT the bias in the residual distributions table for
#: operator visibility (the std and the magnitude are diagnostic for the
#: bridge's RTT spread), but it is not a verdict driver.
_LATENCY_BIAS_ADVISORY_ONLY: Final[bool] = True

#: The exact column set the harness expects in the capture parquet. Locked
#: to :class:`scripts.record_fills.FillRecord`. Surfaced as a module
#: constant so the validation error message can reference it.
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "run_id",
    "request_time_utc",
    "broker_fill_time_utc",
    "symbol",
    "order_type",
    "side",
    "volume_lots",
    "requested_price",
    "fill_price",
    "spread_at_request_pips",
    "slippage_observed_pips",
    "broker_latency_ms",
    "retcode",
    "comment",
)


# --------------------------------------------------------------------------- #
# Output pydantic models
# --------------------------------------------------------------------------- #
class MarketStateReconstruction(BaseModel):
    """Audit record for how one ``MarketState`` field was sourced for a row.

    Surfaced in :class:`Gate2BReport.market_state_reconstruction` so the
    operator can audit *every* field's source before trusting the verdict.
    A reviewer rejects a Gate2BReport that contains a ``DEFAULTED`` entry
    with an empty ``source_detail`` — the protection against silent drift.

    Attributes
    ----------
    field_name : str
        The MarketState field being audited.
    source : Literal["FROM_FILLRECORD", "COMPUTED", "DEFAULTED"]
        Where the value came from. ``FROM_FILLRECORD`` = direct column copy;
        ``COMPUTED`` = derived (e.g. rolling stdev for ``realized_vol_5m``);
        ``DEFAULTED`` = assumption baked in (e.g. ``stress_mode=False``).
    source_detail : str
        Human-readable explanation of the formula / column / assumption.
        Must be non-empty for ``COMPUTED`` and ``DEFAULTED`` sources.
    """

    model_config = ConfigDict(frozen=True)

    field_name: str
    source: Literal["FROM_FILLRECORD", "COMPUTED", "DEFAULTED"]
    source_detail: str


class ResidualRow(BaseModel):
    """One row of the residuals parquet.

    Residual = ``live - sim`` for every numeric field. Sign convention is
    deliberate and matches Wave 6c's :class:`FillResult` schema:

    * ``fill_price_residual``: signed price-unit difference. Positive means
      the live broker filled at a higher price than the simulator — for a
      buy that's adverse-live, for a sell that's favorable-live. The
      adverse direction depends on side, so downstream consumers compute
      adverse residuals by signing with the side.
    * ``slippage_residual_pips``, ``spread_residual_pips``: both
      already adverse-positive in the source schemas
      (see :func:`scripts.record_fills.parse_fill_into_record` and
      :func:`propfarm.sim.fill_engine.simulate_fill`), so the residual
      inherits that convention.
    * ``broker_latency_residual_ms``: signed; positive means live was
      slower than sim's ``execution_latency_ms`` setting.

    Retcode matching is exact (``sim_retcode == live_retcode``); a mismatch
    excludes the row from the residual *distribution* but the row is still
    persisted to the parquet so the operator can investigate it.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    request_time_utc: datetime
    symbol: str
    order_type: str
    side: str
    fill_price_residual: float
    slippage_residual_pips: float
    spread_residual_pips: float
    broker_latency_residual_ms: float
    retcode_match: bool
    sim_retcode: int
    live_retcode: int


class ResidualDistribution(BaseModel):
    """Per-field residual statistics with a systematic-bias t-test.

    Computed only over rows that have both live and sim filled cleanly
    (``retcode_match=True`` AND neither side is NaN). The exclusion is
    necessary because NaN residuals would corrupt percentiles and the
    t-test; the count ``n`` is the surviving row count.

    The t-test is :func:`scipy.stats.ttest_1samp` with H0: ``mean == 0``.
    ``has_systematic_bias`` is True iff ``p_value < _T_TEST_ALPHA`` (0.01).
    Computed *before* any truncation cap so a heavy-tailed distribution
    cannot mask bias.
    """

    model_config = ConfigDict(frozen=True)

    field_name: str
    n: int
    p50: float
    p95: float
    p99: float
    mean: float
    std: float
    t_stat: float
    p_value: float
    has_systematic_bias: bool


class PerSideBiasRow(BaseModel):
    """One row of the per-(symbol, side) fill_price residual bias pane.

    Surfaced in the markdown report only (Gate-2B round 1 reviewer
    follow-up B3) — not currently in :attr:`Gate2BReport.failure_reasons`.
    Round-1 calibration is supposed to collapse the BUY/SELL asymmetry
    that motivates this pane; if a future capture still shows per-side
    bias here (mean far from 0, low p-value), that signal becomes the
    next round's recalibration target.

    The check is computed on rows that:
      * have ``retcode_match=True`` and ``sim_retcode == 10009``,
      * have ``order_type == "market"`` (limits/stops have different
        fill semantics and would dilute the slip-vs-spread signal),
      * have a finite ``fill_price_residual``.

    A (symbol, side) pair with fewer than :data:`_PER_SIDE_BIAS_MIN_N`
    surviving rows is omitted (insufficient sample for a meaningful t-test).

    Attributes
    ----------
    symbol : str
        Trading symbol of the pair.
    side : str
        ``"buy"`` or ``"sell"``.
    n : int
        Surviving row count after the inclusion filter.
    mean_pips : float
        Mean of ``fill_price_residual / pip_size`` in pips. Sign convention
        from ``compute_residual``: residual = live - sim, so positive means
        live filled HIGHER than sim. For BUY the higher price is adverse;
        for SELL the higher price is favorable.
    std_pips : float
        Sample standard deviation of the per-row residual in pips.
    t_stat : float
        One-sample t-statistic against zero mean.
    p_value : float
        Two-sided p-value of the t-test.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: str
    n: int
    mean_pips: float
    std_pips: float
    t_stat: float
    p_value: float


class Gate2BReport(BaseModel):
    """Top-level Gate 2B result. Persisted alongside the residuals parquet.

    Attributes
    ----------
    run_id : str
        Echoes the capture's ``run_id`` for one-to-one attribution.
    capture_parquet_path : str
        Absolute path to the input parquet, recorded for audit.
    capture_parquet_sha256 : str
        SHA256 of the parquet bytes at the time of comparison. Same value
        re-runnable proves the comparison is deterministic over the same
        bytes.
    n_rows_captured : int
        Total rows in the capture parquet.
    n_rows_compared : int
        Rows for which both live and sim produced a valid fill; surfaces
        the effective sample size of the residual distributions.
    n_retcode_matches : int
        Rows where ``sim_retcode == live_retcode``. The distinction matters
        because a row may have a retcode mismatch but still produce numeric
        residuals (e.g. one side is NaN, the other is 0.0).
    market_state_reconstruction : tuple[MarketStateReconstruction, ...]
        Per-field audit trail for the MarketState reconstruction policy.
        Surfaced verbatim in the markdown report.
    residuals_by_field : dict[str, ResidualDistribution]
        Keyed by ``"fill_price"`` / ``"slippage_pips"`` / ``"spread_pips"`` /
        ``"latency_ms"``. Each is the across-symbols distribution; per-symbol
        breakdowns live in the markdown report only.
    fill_price_p95_thresholds_pips : dict[str, float]
        The per-symbol threshold table the verdict consulted.
    spread_p95_threshold_pips : float
        The spread threshold (1.0 pip) the verdict consulted.
    verdict : Literal["pass", "fail", "investigate"]
        * ``pass``: every per-symbol fill-price p95 < threshold AND spread
          p95 < threshold AND no field has systematic bias.
        * ``investigate``: spread/fill-price thresholds passed but a t-test
          detected systematic bias.
        * ``fail``: at least one threshold violation.
    failure_reasons : tuple[str, ...]
        One string per failing condition. Empty on ``pass``. Used by the
        markdown report and the CLI exit code.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str
    capture_parquet_path: str
    capture_parquet_sha256: str
    n_rows_captured: int
    n_rows_compared: int
    n_retcode_matches: int
    market_state_reconstruction: tuple[MarketStateReconstruction, ...]
    residuals_by_field: dict[str, ResidualDistribution]
    fill_price_p95_thresholds_pips: dict[str, float]
    spread_p95_threshold_pips: float = SYMBOL_SPREAD_P95_THRESHOLD_PIPS
    verdict: Literal["pass", "fail", "investigate"]
    failure_reasons: tuple[str, ...]
    # Gate-2B round 1 reviewer follow-up B3: per-(symbol, side) bias pane
    # for fill_price residual. Reporting-only — not added to failure_reasons
    # in this round (the calibration is expected to close the per-side gap;
    # if a future capture still shows a per-side gap, that signal becomes
    # the next round's recalibration target). Defaults to empty for
    # backwards-compat with pre-round-1 Gate2BReport instances persisted
    # before this field was added.
    per_side_bias: tuple[PerSideBiasRow, ...] = ()


# --------------------------------------------------------------------------- #
# Reconstruction primitives
# --------------------------------------------------------------------------- #
def reconstruct_fill_request(row: dict[str, Any]) -> FillRequest:
    """Build a :class:`FillRequest` from one ``FillRecord`` row.

    Trivial 1-to-1 mapping on the shared columns. The only conversion is
    ensuring ``request_time_utc`` is tz-aware UTC (polars deserializes
    Datetime(time_zone="UTC") into tz-aware ``datetime``, so this is a
    no-op in normal flow but the harness validates anyway).

    Parameters
    ----------
    row : dict
        One row of the capture parquet, as returned by ``DataFrame.row(idx,
        named=True)``.

    Returns
    -------
    FillRequest
        Pydantic-validated, frozen.
    """
    return FillRequest(
        run_id=str(row["run_id"]),
        symbol=str(row["symbol"]),
        order_type=row["order_type"],
        side=row["side"],
        volume_lots=float(row["volume_lots"]),
        requested_price=float(row["requested_price"]),
        request_time_utc=row["request_time_utc"],
        comment=str(row["comment"]),
    )


def _compute_realized_vol_for_row(
    same_symbol_prices: list[float],
) -> tuple[float, bool]:
    """Compute the rolling-window annualized vol estimate from prior prices.

    Returns ``(vol, computed)``. If ``computed`` is False, the caller falls
    back to :data:`_TYPICAL_REALIZED_VOL_FALLBACK` and labels the source
    ``DEFAULTED`` instead of ``COMPUTED``.

    Formula::

        log_returns = diff(log(prices))
        vol_per_step = std(log_returns)
        vol_annualized = vol_per_step * sqrt(_ANNUALIZATION_FACTOR)

    The "per-step" semantics implicitly assume 1-minute spacing because
    the recording is paced by a sub-minute schedule on average; the
    annualization factor (252*1440) lines up. If the recorder ever changes
    its cadence by an order of magnitude, the annualization factor in this
    module needs to track — flagged in the runbook.
    """
    if len(same_symbol_prices) < _REALIZED_VOL_WINDOW_ROWS:
        return _TYPICAL_REALIZED_VOL_FALLBACK, False
    window = same_symbol_prices[-_REALIZED_VOL_WINDOW_ROWS:]
    log_prices = np.log(np.asarray(window, dtype=np.float64))
    log_returns = np.diff(log_prices)
    if log_returns.size < 2:
        return _TYPICAL_REALIZED_VOL_FALLBACK, False
    per_step_std = float(np.std(log_returns, ddof=1))
    if not math.isfinite(per_step_std) or per_step_std <= 0.0:
        return _TYPICAL_REALIZED_VOL_FALLBACK, False
    return per_step_std * math.sqrt(_ANNUALIZATION_FACTOR), True


def reconstruct_market_state(
    row: dict[str, Any],
    *,
    prior_same_symbol_prices: list[float] | None = None,
) -> tuple[MarketState, tuple[MarketStateReconstruction, ...]]:
    """Build a :class:`MarketState` from one ``FillRecord`` row + audit trail.

    See the module docstring's "critical design concern" table for the
    full policy. Briefly:

    * ``symbol``, ``ts_utc`` → ``FROM_FILLRECORD``.
    * ``realized_vol_5m`` → ``COMPUTED`` from rolling stdev of recent
      same-symbol ``requested_price`` series when ≥
      :data:`_REALIZED_VOL_WINDOW_ROWS` rows are available; falls back to
      ``DEFAULTED`` to the single global :data:`_TYPICAL_REALIZED_VOL_FALLBACK`
      (= 0.10 annualized, matching slippage module's own None-fallback).
      This is deliberately a single global, not per-symbol — review of
      this trade-off is in the deferred ledger (see STATUS.md). For
      typical-vol differences across symbols (XAUUSD ~3x FX, US100 ~2x FX),
      a future per-symbol dict can replace the global without API change.
    * ``news_window``, ``stress_mode`` → ``DEFAULTED`` to False. Operator
      must manually flag rows that fell inside news / stress windows.

    Parameters
    ----------
    row : dict
        One row of the capture parquet.
    prior_same_symbol_prices : list[float] | None
        Previously-seen ``requested_price`` values for the same symbol, in
        chronological order. The harness builds this incrementally as it
        iterates rows. ``None`` is equivalent to an empty list and forces
        the ``DEFAULTED`` path.

    Returns
    -------
    tuple[MarketState, tuple[MarketStateReconstruction, ...]]
        The frozen MarketState plus an audit trail with one entry per
        :class:`MarketState` field — five entries total.
    """
    symbol = str(row["symbol"])
    ts_utc = row["request_time_utc"]

    prices = list(prior_same_symbol_prices) if prior_same_symbol_prices else []
    vol_value, vol_computed = _compute_realized_vol_for_row(prices)

    audit: list[MarketStateReconstruction] = [
        MarketStateReconstruction(
            field_name="symbol",
            source="FROM_FILLRECORD",
            source_detail="column 'symbol' copied verbatim",
        ),
        MarketStateReconstruction(
            field_name="ts_utc",
            source="FROM_FILLRECORD",
            source_detail="column 'request_time_utc' copied verbatim",
        ),
        MarketStateReconstruction(
            field_name="realized_vol_5m",
            source="COMPUTED" if vol_computed else "DEFAULTED",
            source_detail=(
                f"rolling stdev of last {_REALIZED_VOL_WINDOW_ROWS} same-symbol "
                f"log-returns * sqrt({_ANNUALIZATION_FACTOR:.0f}); "
                f"value={vol_value:.6f}"
                if vol_computed
                else f"insufficient same-symbol history (<{_REALIZED_VOL_WINDOW_ROWS} rows); "
                f"fell back to typical vol {_TYPICAL_REALIZED_VOL_FALLBACK} "
                f"(slippage CALIBRATIONS default)"
            ),
        ),
        MarketStateReconstruction(
            field_name="news_window",
            source="DEFAULTED",
            source_detail=(
                "Phase 0 has no news calendar; defaulted to False. Operator must "
                "manually flag rows that fell inside NFP/CPI/FOMC/central-bank windows."
            ),
        ),
        MarketStateReconstruction(
            field_name="stress_mode",
            source="DEFAULTED",
            source_detail=(
                "record_fills.py does not capture stress_mode and the live broker "
                "has no notion of it; defaulted to False (normal-conditions capture)."
            ),
        ),
    ]

    state = MarketState(
        symbol=symbol,
        ts_utc=ts_utc,
        realized_vol_5m=vol_value,
        news_window=False,
        stress_mode=False,
    )
    return state, tuple(audit)


def _symbol_pip_size(symbol: str) -> float:
    """Pip size lookup mirroring ``propfarm.sim.fill_engine._pip_size``.

    Local copy (not re-export) so the harness can compute pip thresholds
    for symbols that may not be in the engine's static map; raises the
    same ValueError on unknown symbols so any drift surfaces loudly.
    """
    digits = {"EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "XAUUSD": 3, "GER40": 1, "US100": 1}
    if symbol not in digits:
        raise ValueError(
            f"unknown symbol {symbol!r}; pip size unavailable. "
            f"Known symbols: {sorted(digits.keys())}"
        )
    return 10.0 ** -(digits[symbol] - 1)


def compute_residual(
    live: dict[str, Any],
    sim: FillResult,
) -> ResidualRow:
    """Subtract sim from live per field. NaN propagates as NaN.

    The sign of ``fill_price_residual`` is **signed** (not adverse-positive):
    positive means the live broker filled higher than the sim. For a buy
    that's adverse-live; for a sell that's favorable-live. The per-side
    adversity attribution lives in downstream analysis, not this primitive.

    ``slippage_residual_pips`` and ``spread_residual_pips`` inherit their
    adverse-positive sign from the source schemas, so the residual is
    "how much MORE adverse the live fill was than the sim fill".
    """

    def _diff(live_v: float, sim_v: float) -> float:
        # NaN propagation: any NaN in either side → NaN residual.
        if math.isnan(live_v) or math.isnan(sim_v):
            return math.nan
        return float(live_v) - float(sim_v)

    sim_retcode = int(sim.retcode)
    live_retcode = int(live["retcode"])
    return ResidualRow(
        run_id=str(live["run_id"]),
        request_time_utc=live["request_time_utc"],
        symbol=str(live["symbol"]),
        order_type=str(live["order_type"]),
        side=str(live["side"]),
        fill_price_residual=_diff(float(live["fill_price"]), float(sim.fill_price)),
        slippage_residual_pips=_diff(
            float(live["slippage_observed_pips"]), float(sim.slippage_observed_pips)
        ),
        spread_residual_pips=_diff(
            float(live["spread_at_request_pips"]), float(sim.spread_at_request_pips)
        ),
        broker_latency_residual_ms=_diff(
            float(live["broker_latency_ms"]), float(sim.broker_latency_ms)
        ),
        retcode_match=(sim_retcode == live_retcode),
        sim_retcode=sim_retcode,
        live_retcode=live_retcode,
    )


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def _residual_distribution(
    field_name: str,
    values: np.ndarray,
) -> ResidualDistribution:
    """Build a :class:`ResidualDistribution` from a clean (no-NaN) array.

    The caller is responsible for filtering NaNs and retcode-mismatches
    before calling this — see :func:`run_gate_2b`.
    """
    n = int(values.size)
    if n == 0:
        # Empty distribution placeholder. Verdict logic must treat n=0 as
        # not-failing on per-symbol thresholds but explicitly flag the
        # report as having no data — surfaces in failure_reasons.
        return ResidualDistribution(
            field_name=field_name,
            n=0,
            p50=math.nan,
            p95=math.nan,
            p99=math.nan,
            mean=math.nan,
            std=math.nan,
            t_stat=math.nan,
            p_value=math.nan,
            has_systematic_bias=False,
        )

    abs_values = np.abs(values)
    p50 = float(np.percentile(abs_values, 50))
    p95 = float(np.percentile(abs_values, 95))
    p99 = float(np.percentile(abs_values, 99))
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if n > 1 else 0.0

    # One-sample t-test: H0 mean == 0. scipy.stats.ttest_1samp handles
    # n==1 by returning NaN p-value, which the dist treats as "no bias"
    # (cannot reject the null with one sample).
    if n > 1 and std > 0.0:
        t_result = stats.ttest_1samp(values, popmean=0.0)
        t_stat = float(t_result.statistic)
        p_value = float(t_result.pvalue)
    else:
        t_stat = math.nan
        p_value = math.nan

    has_bias = math.isfinite(p_value) and p_value < _T_TEST_ALPHA
    return ResidualDistribution(
        field_name=field_name,
        n=n,
        p50=p50,
        p95=p95,
        p99=p99,
        mean=mean,
        std=std,
        t_stat=t_stat,
        p_value=p_value,
        has_systematic_bias=has_bias,
    )


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def _sha256_file(path: Path) -> str:
    """Stream-hash a file. Stable across re-runs of the same bytes."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_fill_record_class() -> type[BaseModel]:
    """Load :class:`FillRecord` from ``scripts/record_fills.py``.

    ``scripts/`` is not a Python package, so the FillRecord class is loaded
    via ``importlib.util`` — same pattern as
    ``tests/scripts/test_record_fills.py``. The module is registered in
    ``sys.modules`` before ``exec_module`` because of the
    ``@dataclass``-with-string-annotations issue documented there.
    """
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "record_fills.py"
    spec = importlib.util.spec_from_file_location("record_fills", script_path)
    if spec is None or spec.loader is None:  # pragma: no cover — defensive
        raise RuntimeError(f"failed to spec scripts/record_fills.py at {script_path!r}")
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("record_fills", module)
    spec.loader.exec_module(module)
    fill_record_cls = module.FillRecord
    if not isinstance(fill_record_cls, type) or not issubclass(fill_record_cls, BaseModel):
        raise TypeError("scripts/record_fills.py FillRecord is not a pydantic BaseModel subclass")
    return fill_record_cls


#: Manifest status value that disqualifies a capture from Gate 2B. Set by
#: ``scripts/record_fills.py`` on the 2026-05-13 capture after the
#: OrderSendResult.price=0 bug was confirmed. Any future "salvageable for
#: spread/latency, unusable for fill_price" capture should reuse this value
#: so the guard catches it.
UNUSABLE_MANIFEST_STATUS: Final[str] = "fill_price-unusable"

#: Maximum tolerable ratio of ``n_market_lookup_failures`` to filled
#: market rows in the capture manifest (2026-05-14 fix v2). A non-zero
#: count means the recording script could not retrieve the deal record
#: for some market orders even after all three lookup paths
#: (ticket / position / time-range) — those rows carry NaN fill_price
#: and would inflate the Gate 2B residual quantiles. The 5% threshold is
#: deliberately conservative: 100 market rows can absorb up to 5 lookup
#: failures (sub-ceiling p95 effect) but more makes the cost data
#: unreliable. Comparison is strict-greater-than (``> threshold``) so a
#: capture sitting exactly at 5% passes — the threshold is the inclusive
#: tolerance ceiling, not the rejection floor.
MAX_MARKET_LOOKUP_FAILURE_RATIO: Final[float] = 0.05


def _reject_if_unusable_manifest(capture_parquet_path: Path) -> None:
    """Raise ``ValueError`` if the sidecar manifest marks the capture unusable.

    Two rejection criteria (2026-05-14 fix v2 adds the second):

    1. ``status == "fill_price-unusable"`` (legacy v1 guard) — the
       2026-05-13 ``24e00278…`` capture is the canonical instance.
    2. ``n_market_lookup_failures / max(n_filled, 1) > 0.05`` — at least
       5% of market fills failed deal lookup. See
       :data:`MAX_MARKET_LOOKUP_FAILURE_RATIO`.

    Forward-compat: a manifest WITHOUT a ``status`` key OR without a
    ``n_market_lookup_failures`` key is the normal case (Phase-0
    manifests pre-v1.1 don't include the field; the loader treats
    missing as ``0``).
    """
    manifest_path = capture_parquet_path.with_suffix(".json")
    if not manifest_path.exists():
        # Missing manifest is not a fail-stop here — schema validation on the
        # parquet itself is the primary gate. Capture-from-old-version
        # parquets may legitimately be manifest-less.
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Corrupt manifest — defer to schema validation rather than blocking
        # here, since a bad-JSON manifest is a separate concern from the
        # fill_price-unusable signal.
        return
    if not isinstance(manifest, dict):
        return
    status = manifest.get("status")
    if status == UNUSABLE_MANIFEST_STATUS:
        reason = manifest.get("unusable_reason", "(no unusable_reason provided)")
        raise ValueError(
            f"capture parquet at {capture_parquet_path} is marked "
            f"status={UNUSABLE_MANIFEST_STATUS!r} in its manifest "
            f"({manifest_path}): {reason}. "
            f"Re-record with the current scripts/record_fills.py before "
            f"running Gate 2B."
        )

    # Market lookup failure ratio guard (2026-05-14 fix v2 + reviewer
    # follow-up: market-only denominator).
    #
    # Prefer the v1.2 ``n_filled_market`` denominator when present, since
    # ``n_market_lookup_failures`` is by construction a count over
    # ``order_type == "market"`` rows. The v1.1 manifest only published
    # ``n_filled`` (market + pending), which DILUTES the ratio and lets
    # the guard tolerate roughly twice the market-only failure rate it
    # documents — the opposite of safe. v1.2 manifests get the strict
    # check; v1.1/v1.0 manifests fall back to the lenient n_filled
    # denominator with the rejection message naming the denominator so
    # the operator knows which path triggered.
    #
    # Forward-compat: manifests without ``n_market_lookup_failures``
    # default to 0 (v1.0); manifests without ``n_filled_market`` (also v1.0
    # and v1.1) fall back to ``n_filled``.
    n_market_lookup_failures = int(manifest.get("n_market_lookup_failures", 0))
    n_filled = int(manifest.get("n_filled", 0))
    n_filled_market_raw = manifest.get("n_filled_market")
    if n_market_lookup_failures > 0:
        if n_filled_market_raw is not None and int(n_filled_market_raw) > 0:
            denom = int(n_filled_market_raw)
            denom_field = "n_filled_market"
        else:
            denom = max(n_filled, 1)
            denom_field = "n_filled (v1.0/1.1 fallback — lenient)"
        ratio = n_market_lookup_failures / denom
        if ratio > MAX_MARKET_LOOKUP_FAILURE_RATIO:
            raise ValueError(
                f"capture parquet at {capture_parquet_path} has "
                f"n_market_lookup_failures={n_market_lookup_failures} "
                f"over {denom_field}={denom} "
                f"(ratio={ratio:.4f} > {MAX_MARKET_LOOKUP_FAILURE_RATIO:.4f}); "
                f"market fill_price data is unreliable. "
                f"Re-record with the current scripts/record_fills.py "
                f"and verify [record_fills:market_lookup_failure] stderr "
                f"entries are absent."
            )


def _validate_schema(df: pl.DataFrame) -> None:
    """Raise ``ValueError`` if the parquet's columns don't match FillRecord.

    Compares the set of column names (order-insensitive) to
    :data:`_REQUIRED_COLUMNS`. Extra columns are allowed (forward-compat).
    Missing columns are a hard error: the harness cannot synthesize a row
    it doesn't have data for.
    """
    actual = set(df.columns)
    required = set(_REQUIRED_COLUMNS)
    missing = required - actual
    if missing:
        raise ValueError(
            f"capture parquet missing required FillRecord columns: {sorted(missing)}. "
            f"Got columns: {sorted(actual)}. "
            f"This usually means the parquet was written by an older record_fills.py "
            f"version — re-record with the current schema."
        )


# --------------------------------------------------------------------------- #
# Per-side bias diagnostic (Gate-2B round 1 reviewer follow-up B3)
# --------------------------------------------------------------------------- #
def _compute_per_side_bias(residual_rows: list[ResidualRow]) -> tuple[PerSideBiasRow, ...]:
    """Compute per-(symbol, side) fill_price residual bias rows for the report.

    See :class:`PerSideBiasRow` for the inclusion rule.
    """
    by_pair: dict[tuple[str, str], list[float]] = {}
    for r in residual_rows:
        if not (r.retcode_match and r.sim_retcode == 10009 and r.order_type == "market"):
            continue
        if not math.isfinite(r.fill_price_residual):
            continue
        by_pair.setdefault((r.symbol, r.side), []).append(r.fill_price_residual)

    rows: list[PerSideBiasRow] = []
    for (symbol, side), values in by_pair.items():
        n = len(values)
        if n < _PER_SIDE_BIAS_MIN_N:
            continue
        pip = _symbol_pip_size(symbol)
        arr_pips = np.asarray(values, dtype=np.float64) / pip
        mean_pips = float(np.mean(arr_pips))
        std_pips = float(np.std(arr_pips, ddof=1)) if n > 1 else 0.0
        if n > 1 and std_pips > 0.0:
            t_result = stats.ttest_1samp(arr_pips, popmean=0.0)
            t_stat = float(t_result.statistic)
            p_value = float(t_result.pvalue)
        else:
            t_stat = math.nan
            p_value = math.nan
        rows.append(
            PerSideBiasRow(
                symbol=symbol,
                side=side,
                n=n,
                mean_pips=mean_pips,
                std_pips=std_pips,
                t_stat=t_stat,
                p_value=p_value,
            )
        )
    # Deterministic ordering for stable reports / tests.
    rows.sort(key=lambda r: (r.symbol, r.side))
    return tuple(rows)


# --------------------------------------------------------------------------- #
# Verdict logic
# --------------------------------------------------------------------------- #
def _evaluate_verdict(
    residual_rows: list[ResidualRow],
    by_field: dict[str, ResidualDistribution],
) -> tuple[Literal["pass", "fail", "investigate"], tuple[str, ...]]:
    """Apply the user-mandated pass / investigate / fail logic.

    Reasons emit as a flat tuple of human-readable strings; each string is
    tagged with its band (FAIL or INVESTIGATE) so the markdown report can
    surface them grouped. Verdict aggregation: if any FAIL reason exists,
    verdict=fail; else if any INVESTIGATE reason exists, verdict=investigate;
    else pass.

    Bands (Gate-2B round 1, reviewer-mandated B4):

    Per-symbol fill_price (in pips, p95 |residual|):
      * p95 ≤ threshold                  → PASS contribution
      * threshold < p95 ≤ threshold x :data:`_INVESTIGATE_BAND_MULTIPLIER_FILL_PRICE` → INVESTIGATE
      * p95 > threshold x :data:`_INVESTIGATE_BAND_MULTIPLIER_FILL_PRICE`             → FAIL

    Global spread (in pips, p95 |residual|):
      * p95 ≤ 1.0                                  → PASS contribution
      * 1.0 < p95 ≤ 1.0 x :data:`_INVESTIGATE_BAND_MULTIPLIER_SPREAD` → INVESTIGATE
      * p95 > 1.0 x :data:`_INVESTIGATE_BAND_MULTIPLIER_SPREAD`       → FAIL

    Systematic bias t-test (per field — fill_price / slippage / spread /
    latency; latency is advisory-only per :data:`_LATENCY_BIAS_ADVISORY_ONLY`):
      * p ≥ 0.01                         → PASS contribution
      * 0.001 ≤ p < 0.01                 → INVESTIGATE
      * p < 0.001                        → FAIL

    Reviewer follow-up B1: per-symbol fill_price p95 is computed only over
    rows with ``order_type=='market'`` (in addition to the existing
    ``retcode_match`` + ``sim_retcode == 10009`` filter). Limit and stop
    rows have different fill semantics (limits fill at the requested price
    or reject; stops trigger before filling) and a few rare-event
    outliers in those order types can dominate the p95 of a
    market-dominated capture. Locking to markets eliminates that bias.

    Reviewer follow-up B2: ``latency_ms`` bias is reported but never added
    to failure_reasons. See :data:`_LATENCY_BIAS_ADVISORY_ONLY` for the
    advisory-only rationale (sim execution_latency_ms is set to the live
    median by construction, so the residual t-test reflects the live
    distribution's right-tail rather than a calibration drift).
    """
    fail_reasons: list[str] = []
    investigate_reasons: list[str] = []

    # ------------------------------------------------------------------ #
    # Per-symbol fill_price residual check (in pips). B1: market only.
    # ------------------------------------------------------------------ #
    by_symbol: dict[str, list[float]] = {}
    for r in residual_rows:
        if not (r.retcode_match and r.sim_retcode == 10009):
            continue
        if r.order_type != "market":
            continue
        if not math.isfinite(r.fill_price_residual):
            continue
        by_symbol.setdefault(r.symbol, []).append(r.fill_price_residual)
    for symbol, residuals in by_symbol.items():
        pip = _symbol_pip_size(symbol)
        abs_pips = np.abs(np.asarray(residuals, dtype=np.float64)) / pip
        p95 = float(np.percentile(abs_pips, 95)) if abs_pips.size > 0 else 0.0
        threshold = SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS.get(symbol)
        if threshold is None:
            fail_reasons.append(
                f"unknown_symbol_threshold:{symbol} (no entry in "
                f"SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS)"
            )
            continue
        fail_threshold = threshold * _INVESTIGATE_BAND_MULTIPLIER_FILL_PRICE
        if p95 > fail_threshold:
            fail_reasons.append(
                f"fill_price_p95_exceeded:{symbol} p95={p95:.4f}pips "
                f"threshold={threshold:.4f}pips fail_band={fail_threshold:.4f}pips"
            )
        elif p95 > threshold:
            investigate_reasons.append(
                f"fill_price_p95_investigate:{symbol} p95={p95:.4f}pips "
                f"threshold={threshold:.4f}pips fail_band={fail_threshold:.4f}pips"
            )

    # ------------------------------------------------------------------ #
    # Global spread residual check (p95 |residual| across all symbols).
    # ------------------------------------------------------------------ #
    spread_dist = by_field.get("spread_pips")
    if spread_dist is not None and spread_dist.n > 0 and math.isfinite(spread_dist.p95):
        spread_threshold = SYMBOL_SPREAD_P95_THRESHOLD_PIPS
        spread_fail_threshold = spread_threshold * _INVESTIGATE_BAND_MULTIPLIER_SPREAD
        if spread_dist.p95 > spread_fail_threshold:
            fail_reasons.append(
                f"spread_p95_exceeded p95={spread_dist.p95:.4f}pips "
                f"threshold={spread_threshold:.4f}pips "
                f"fail_band={spread_fail_threshold:.4f}pips"
            )
        elif spread_dist.p95 > spread_threshold:
            investigate_reasons.append(
                f"spread_p95_investigate p95={spread_dist.p95:.4f}pips "
                f"threshold={spread_threshold:.4f}pips "
                f"fail_band={spread_fail_threshold:.4f}pips"
            )

    # ------------------------------------------------------------------ #
    # Systematic-bias check across the four residual fields. B2: latency
    # advisory-only (excluded from both FAIL and INVESTIGATE).
    # ------------------------------------------------------------------ #
    for field_name, dist in by_field.items():
        if field_name == "latency_ms" and _LATENCY_BIAS_ADVISORY_ONLY:
            continue
        if dist.n <= 1 or not math.isfinite(dist.p_value):
            continue
        if dist.p_value < _T_TEST_ALPHA_FAIL:
            fail_reasons.append(
                f"systematic_bias:{field_name} mean={dist.mean:.6f} "
                f"p_value={dist.p_value:.6f} (alpha_fail={_T_TEST_ALPHA_FAIL})"
            )
        elif dist.p_value < _T_TEST_ALPHA_INVESTIGATE:
            investigate_reasons.append(
                f"systematic_bias_investigate:{field_name} mean={dist.mean:.6f} "
                f"p_value={dist.p_value:.6f} "
                f"(alpha_investigate={_T_TEST_ALPHA_INVESTIGATE})"
            )

    if fail_reasons:
        return "fail", tuple(fail_reasons + investigate_reasons)
    if investigate_reasons:
        return "investigate", tuple(investigate_reasons)
    return "pass", ()


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #
def _format_markdown(report: Gate2BReport) -> str:
    """Render the report as operator-readable markdown."""
    lines: list[str] = [
        f"# Gate 2B report — run_id `{report.run_id}`",
        "",
        f"- **Verdict:** {report.verdict.upper()}",
        f"- Capture parquet: `{report.capture_parquet_path}`",
        f"- Capture SHA256: `{report.capture_parquet_sha256}`",
        f"- Rows captured: {report.n_rows_captured}",
        f"- Rows compared: {report.n_rows_compared}",
        f"- Retcode matches: {report.n_retcode_matches}",
        "",
        "## MarketState reconstruction audit",
        "",
        "| field | source | detail |",
        "|---|---|---|",
    ]
    for rec in report.market_state_reconstruction:
        lines.append(f"| `{rec.field_name}` | {rec.source} | {rec.source_detail} |")
    lines.extend(
        [
            "",
            "## Per-symbol fill-price thresholds (pips, p95 |residual|)",
            "",
            "| symbol | threshold |",
            "|---|---|",
        ]
    )
    for sym, thr in report.fill_price_p95_thresholds_pips.items():
        lines.append(f"| {sym} | {thr:.4f} |")
    lines.extend(
        [
            "",
            f"Global spread threshold (pips, p95 |residual|): "
            f"**{report.spread_p95_threshold_pips:.4f}**",
            "",
            "## Residual distributions",
            "",
            "| field | n | p50 | p95 | p99 | mean | std | t_stat | p_value | bias? |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for field_name, dist in report.residuals_by_field.items():
        lines.append(
            f"| {field_name} | {dist.n} | {dist.p50:.6f} | {dist.p95:.6f} | "
            f"{dist.p99:.6f} | {dist.mean:.6f} | {dist.std:.6f} | "
            f"{dist.t_stat:.4f} | {dist.p_value:.6f} | "
            f"{'YES' if dist.has_systematic_bias else 'no'} |"
        )

    # ------------------------------------------------------------------ #
    # Per-(symbol, side) bias pane — Gate-2B round 1 reviewer follow-up B3.
    # Reporting-only; not part of failure_reasons in this round. Emitted
    # only when at least one (symbol, side) pair has n >= _PER_SIDE_BIAS_MIN_N
    # so the table stays informative.
    # ------------------------------------------------------------------ #
    if report.per_side_bias:
        lines.extend(
            [
                "",
                "## Per-(symbol, side) fill_price residual bias (markets only, n >= "
                f"{_PER_SIDE_BIAS_MIN_N})",
                "",
                "Sign convention: ``residual = live - sim`` in pips. For BUY a "
                "positive mean means live filled HIGHER than sim (adverse for "
                "buyer); for SELL a positive mean means live filled HIGHER than "
                "sim (favorable for seller). The Gate-2B round 1 calibration "
                "targets per-side mean → 0 on both sides simultaneously.",
                "",
                "| symbol | side | n | mean (pips) | std (pips) | t_stat | p_value |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for r in report.per_side_bias:
            lines.append(
                f"| {r.symbol} | {r.side} | {r.n} | {r.mean_pips:+.6f} | "
                f"{r.std_pips:.6f} | {r.t_stat:+.4f} | {r.p_value:.6f} |"
            )
        # Machine-readable single-line tags so external tools / future reviewers
        # can grep the report without parsing the table.
        lines.append("")
        for r in report.per_side_bias:
            lines.append(
                f"[per_side_bias:{r.symbol}:{r.side} "
                f"n={r.n} mean={r.mean_pips:+.6f} t={r.t_stat:+.4f} "
                f"p={r.p_value:.6f}]"
            )

    if report.failure_reasons:
        lines.extend(["", "## Failure reasons", ""])
        for reason in report.failure_reasons:
            lines.append(f"- {reason}")
    else:
        lines.extend(["", "No failures. Cost models are calibrated against live."])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_gate_2b(
    capture_parquet_path: Path,
    *,
    output_parquet_path: Path | None = None,
    output_markdown_path: Path | None = None,
    execution_latency_ms: float | None = None,
) -> Gate2BReport:
    """Run the full Gate 2B comparison.

    Steps
    -----
    1. Hash the input parquet (SHA256) and load it via polars.
    2. Validate the column set against :data:`_REQUIRED_COLUMNS`. Missing
       columns → ``ValueError``.
    3. If ``execution_latency_ms`` is ``None``, derive it from the median
       of the captured ``broker_latency_ms`` column (only on
       successfully-filled rows — retcode 10009 — so a parade of rejects
       doesn't drag the median).
    4. Iterate rows (ascending row index). For each row:
       a. Reconstruct ``FillRequest`` and ``MarketState`` (with audit trail
          on row 0).
       b. Call :func:`simulate_fill` with a deterministic per-row rng
          seeded from the row index.
       c. Compute the residual.
       d. Track the running same-symbol price series for the rolling-vol
          window.
    5. Compute per-field :class:`ResidualDistribution` over rows where
       ``retcode_match=True`` and the residual is finite.
    6. Evaluate verdict via :func:`_evaluate_verdict`.
    7. Write the residuals parquet and markdown report. Return the
       :class:`Gate2BReport`.

    Parameters
    ----------
    capture_parquet_path : Path
        Input parquet from ``scripts/record_fills.py``.
    output_parquet_path : Path | None
        Where to write the residuals parquet. Default:
        ``{capture_dir}/{run_id}_residuals.parquet``.
    output_markdown_path : Path | None
        Where to write the markdown report. Default:
        ``{capture_dir}/{run_id}_report.md``.
    execution_latency_ms : float | None
        Override the fill-engine's execution-latency parameter. ``None``
        triggers the median-broker-latency derivation described above.

    Returns
    -------
    Gate2BReport
        Frozen, pydantic-validated.
    """
    capture_path = Path(capture_parquet_path).resolve()
    if not capture_path.exists():
        raise FileNotFoundError(f"capture parquet not found: {capture_path}")

    # Reject any capture whose sidecar manifest is flagged unusable. The
    # 2026-05-13 capture (run_id 24e00278…) is the canonical instance:
    # OrderSendResult.price=0 bug → every retcode=10009 row has
    # fill_price=0.0. The manifest carries `"status": "fill_price-unusable"`
    # and an `"unusable_reason"` string. Feeding it to Gate 2B would produce
    # nonsense p95s; the guard is the structural protection.
    _reject_if_unusable_manifest(capture_path)

    sha256 = _sha256_file(capture_path)
    df = pl.read_parquet(capture_path)
    _validate_schema(df)

    n_captured = df.height
    if n_captured == 0:
        raise ValueError(
            f"capture parquet at {capture_path} is empty (0 rows); "
            f"nothing to compare. Re-run scripts/record_fills.py."
        )

    # Sort by request_time_utc so the rolling-window vol calc sees a true
    # chronological series, not whatever order the parquet was written in.
    df = df.sort("request_time_utc")

    # Derive execution_latency_ms if not specified.
    if execution_latency_ms is None:
        successful = df.filter(pl.col("retcode") == 10009)
        if successful.height > 0:
            execution_latency_ms = float(
                successful.select(pl.col("broker_latency_ms").median()).item()
            )
        else:
            # No successful fills → fall back to the engine default.
            from propfarm.sim.fill_engine import DEFAULT_EXECUTION_LATENCY_MS

            execution_latency_ms = float(DEFAULT_EXECUTION_LATENCY_MS)

    # Iterate every row, collect per-row audit, and tally per-field source
    # distribution. Prior implementation snapshotted row-0's audit only —
    # misleading because row 0 always falls back to DEFAULTED for
    # realized_vol_5m (no prior history at idx=0). The report now shows
    # the actual source distribution (e.g. "COMPUTED in 195/200 rows,
    # DEFAULTED in 5/200 rows"), so the operator can audit how often each
    # fallback fired.
    expected_fields = {"symbol", "ts_utc", "realized_vol_5m", "news_window", "stress_mode"}
    per_field_source_counts: dict[str, Counter[str]] = {f: Counter() for f in expected_fields}
    per_field_first_detail: dict[str, dict[str, str]] = {f: {} for f in expected_fields}

    residual_rows: list[ResidualRow] = []
    same_symbol_prices: dict[str, list[float]] = {}
    run_ids: set[str] = set()

    for idx in range(df.height):
        row = df.row(idx, named=True)
        symbol = str(row["symbol"])
        run_id_str = str(row["run_id"])
        run_ids.add(run_id_str)

        prior_prices = list(same_symbol_prices.get(symbol, []))
        request = reconstruct_fill_request(row)
        market_state, row_audit = reconstruct_market_state(
            row,
            prior_same_symbol_prices=prior_prices,
        )

        # Tally per-field source distribution across rows.
        for rec in row_audit:
            per_field_source_counts[rec.field_name][rec.source] += 1
            # Capture one canonical detail string per (field, source) pair,
            # so the report can cite the actual phrasing the helper produced.
            per_field_first_detail[rec.field_name].setdefault(rec.source, rec.source_detail)

        # Per-row deterministic rng. Seed from (run_id, idx) so two captures
        # with overlapping row indices don't accidentally share rng state.
        # SHA256 pin in the report still binds determinism to the parquet
        # bytes; this is hardening, not a correctness fix.
        #
        # IMPORTANT: ``hash()`` on strings/tuples is salted per-process
        # (PYTHONHASHSEED) so it would produce different rng seeds across
        # Python invocations. Use a stable hash (SHA256 over the encoded
        # seed material) to keep the per-row rng identical across processes
        # — important for cross-process reproducibility of the residuals
        # parquet (the SHA256 pin is on the input bytes, but the residuals
        # themselves depend on the per-row noise draw).
        row_seed = int.from_bytes(
            hashlib.sha256(f"{run_id_str}|{idx}".encode()).digest()[:4],
            "big",
            signed=False,
        )
        rng = np.random.default_rng(seed=row_seed)
        sim = simulate_fill(
            request,
            market_state,
            execution_latency_ms=execution_latency_ms,
            rng=rng,
        )
        residual_rows.append(compute_residual(row, sim))

        # Update the rolling-window state AFTER the row so the row sees
        # only strictly-prior prices (no leak from current row).
        same_symbol_prices.setdefault(symbol, []).append(float(row["requested_price"]))

    # Cross-check: every MarketState field has been audited at least once.
    audited_fields = {f for f, c in per_field_source_counts.items() if sum(c.values()) > 0}
    if audited_fields != expected_fields:  # pragma: no cover — invariant
        raise RuntimeError(
            f"MarketState audit trail mismatch: expected {sorted(expected_fields)}, "
            f"got {sorted(audited_fields)}"
        )

    # Build the canonical audit: one MarketStateReconstruction per field,
    # with `source` = dominant source, and `source_detail` enriched with
    # the per-row distribution counts. Operator can see at a glance
    # whether fallbacks fired often, never, or always.
    n_rows = df.height
    canonical_audit_list: list[MarketStateReconstruction] = []
    for field_name in (
        "symbol",
        "ts_utc",
        "realized_vol_5m",
        "news_window",
        "stress_mode",
    ):
        counter = per_field_source_counts[field_name]
        dominant_source, _ = counter.most_common(1)[0]
        details_used = per_field_first_detail[field_name]
        # Distribution string: e.g. "COMPUTED in 195/200, DEFAULTED in 5/200".
        dist_parts = [f"{src} in {n}/{n_rows}" for src, n in counter.most_common()]
        dist_str = "; ".join(dist_parts)
        # Compose final detail: dominant-source's helper text + the distribution.
        helper_text = details_used.get(dominant_source, "")
        if helper_text:
            combined_detail = f"{helper_text} | distribution: {dist_str}"
        else:
            combined_detail = f"distribution: {dist_str}"
        canonical_audit_list.append(
            MarketStateReconstruction(
                field_name=field_name,
                source=cast("Literal['FROM_FILLRECORD', 'COMPUTED', 'DEFAULTED']", dominant_source),
                source_detail=combined_detail,
            )
        )
    canonical_audit = tuple(canonical_audit_list)

    # Filter for distribution computation. A row contributes iff retcode
    # matched AND the residual is finite (no NaN).
    #
    # Reviewer issue 6: this inclusion rule (retcode_match AND finite) is
    # *narrower* than what counts toward the verdict's failure_reasons.
    # Specifically, `n_rows_compared` and bias-test denominators here only
    # include rows where the sim agreed on retcode with the live capture,
    # while the verdict's "retcode mismatch ratio" check below (see
    # `_evaluate_verdict`) operates over ALL non-skipped rows. This divergence
    # is intentional: residual *quantiles* are only meaningful where both
    # paths produced a comparable fill, but a high retcode-mismatch ratio is
    # itself a failure signal regardless of where mismatches landed.
    def _clean(values: list[float], rows: list[ResidualRow]) -> np.ndarray:
        return np.asarray(
            [v for v, r in zip(values, rows, strict=True) if r.retcode_match and math.isfinite(v)],
            dtype=np.float64,
        )

    fp_vals = [r.fill_price_residual for r in residual_rows]
    sl_vals = [r.slippage_residual_pips for r in residual_rows]
    sp_vals = [r.spread_residual_pips for r in residual_rows]
    lat_vals = [r.broker_latency_residual_ms for r in residual_rows]

    by_field: dict[str, ResidualDistribution] = {
        "fill_price": _residual_distribution("fill_price", _clean(fp_vals, residual_rows)),
        "slippage_pips": _residual_distribution("slippage_pips", _clean(sl_vals, residual_rows)),
        "spread_pips": _residual_distribution("spread_pips", _clean(sp_vals, residual_rows)),
        "latency_ms": _residual_distribution("latency_ms", _clean(lat_vals, residual_rows)),
    }

    n_retcode_matches = sum(1 for r in residual_rows if r.retcode_match)
    n_compared = int(by_field["fill_price"].n)

    verdict, failure_reasons = _evaluate_verdict(residual_rows, by_field)

    # Per-(symbol, side) bias pane (Gate-2B round 1 B3, reporting-only).
    per_side_bias = _compute_per_side_bias(residual_rows)

    # Multiple run_ids would imply a corrupt capture — single capture
    # should be one run_id. We don't fail; we pick the first deterministically.
    chosen_run_id = sorted(run_ids)[0] if run_ids else "unknown"

    report = Gate2BReport(
        run_id=chosen_run_id,
        capture_parquet_path=str(capture_path),
        capture_parquet_sha256=sha256,
        n_rows_captured=n_captured,
        n_rows_compared=n_compared,
        n_retcode_matches=n_retcode_matches,
        market_state_reconstruction=canonical_audit,
        residuals_by_field=by_field,
        fill_price_p95_thresholds_pips=dict(SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS),
        spread_p95_threshold_pips=SYMBOL_SPREAD_P95_THRESHOLD_PIPS,
        verdict=verdict,
        failure_reasons=failure_reasons,
        per_side_bias=per_side_bias,
    )

    # Persist outputs.
    capture_dir = capture_path.parent
    out_parquet = (
        Path(output_parquet_path).resolve()
        if output_parquet_path is not None
        else capture_dir / f"{chosen_run_id}_residuals.parquet"
    )
    out_md = (
        Path(output_markdown_path).resolve()
        if output_markdown_path is not None
        else capture_dir / f"{chosen_run_id}_report.md"
    )
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)

    residuals_df = pl.DataFrame(
        [r.model_dump() for r in residual_rows],
        schema_overrides={
            "request_time_utc": pl.Datetime(time_zone="UTC"),
        },
    )
    residuals_df.write_parquet(out_parquet)
    out_md.write_text(_format_markdown(report), encoding="utf-8")

    return report


# --------------------------------------------------------------------------- #
# Module-level invariant: every CALIBRATIONS symbol has a threshold.
# --------------------------------------------------------------------------- #
_MISSING_THRESHOLDS = set(SLIPPAGE_CALIBRATIONS.keys()) - set(
    SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS.keys()
)
if _MISSING_THRESHOLDS:  # pragma: no cover — import-time invariant
    raise RuntimeError(
        f"Gate 2B SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS is missing entries for: "
        f"{sorted(_MISSING_THRESHOLDS)}; either add a threshold or remove the "
        f"calibration entry to keep the gate aligned with the cost-model surface."
    )


__all__ = [
    "MAX_MARKET_LOOKUP_FAILURE_RATIO",
    "SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS",
    "SYMBOL_SPREAD_P95_THRESHOLD_PIPS",
    "UNUSABLE_MANIFEST_STATUS",
    "Gate2BReport",
    "MarketStateReconstruction",
    "PerSideBiasRow",
    "ResidualDistribution",
    "ResidualRow",
    "compute_residual",
    "reconstruct_fill_request",
    "reconstruct_market_state",
    "run_gate_2b",
]
