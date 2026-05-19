"""Historical stress-event replay harness (Task 10.2, Wave 6d).

Why this module exists
----------------------
The cost models — spread (Task 6.1), slippage (Task 7.1), fill engine
(Task 7.2) — were calibrated against quiet, mid-session FTMO MT5 demo data
(Gate 2B round 1+2). That capture is empirically representative of the
~95% of the trading week that LOOKS LIKE normal liquidity. The remaining
~5% — the regimes a prop-firm strategy actually loses money in — are
historical event tails: Lehman 2008, SNB 2015, COVID 2020, UK gilts 2022,
SVB 2023.

This module is the Phase-0 gate that proves the calibrated pipeline still
produces SANE FILLS (no NaN, no negative price, fills inside the quoted
spread within tolerance) when it is driven through those five stress
windows. It is **not** a calibration step: it does not change a single
``CALIBRATIONS`` entry. It is a regression-style harness that loads
representative historical tick streams (Dukascopy fixtures when
available, otherwise synthetic reproductions of the documented vol
shape), drives every tick through :func:`propfarm.sim.fill_engine.simulate_fill`,
and asserts the engine survives every regime.

The five mandated stress windows
--------------------------------
Per the Phase-0 spec the windows are named, dated, and symbol-anchored
verbatim:

1. **lehman_2008** — Lehman / financial crisis week. Sep 15-19 2008.
   EURUSD. Major FX vol; USD funding stress; ALL pairs widened.
2. **snb_2015** — SNB peg removal. Jan 15 2015 09:30 UTC.
   EURUSD (proxy for EURCHF which is not in :data:`SUPPORTED_SYMBOLS`).
   1900-pip EURCHF gap; correlated ~150-200 pip EURUSD slide.
3. **covid_2020** — COVID crash week. Mar 9-13 2020.
   US100. Multiple circuit breakers + reflective gap moves in equity
   index CFDs.
4. **gilt_2022** — UK gilt crisis / mini-budget. Sep 23-30 2022.
   GBPUSD. Intraday vol; BoE intervention day Sep 28.
5. **svb_2023** — SVB bank run week. Mar 10-17 2023.
   EURUSD. Bank stress + USD curve repricing; EURUSD vol.

EURCHF substitution (SNB 2015)
------------------------------
The user spec named EURCHF for SNB. EURCHF is not in
:data:`propfarm.data.quality.SUPPORTED_SYMBOLS` (the Phase-0 instrument
universe is EURUSD / GBPUSD / USDJPY / XAUUSD / GER40 / US100). To test
the SAME structural behavior — broker-spread blowout, gap-fill price
ambiguity, sub-second 1000+ pip move — we run the SNB window on **EURUSD**,
which correlated with the move (~150-200 pip slide between 09:30 and
09:45 UTC on Jan 15 2015 per the documented sequence: EURCHF dropped, EUR
funding stress hit, EURUSD followed). The adversarial test
(``test_snb_2015_long_through_gap``) uses the same shape: a long position
with SL inside the 09:30-09:45 UTC slide must NOT fill at the SL price as
if the gap didn't happen.

A future task could add EURCHF to :data:`SUPPORTED_SYMBOLS` (it would
need a session-hours rule and cost-model calibration entries). Until
then, EURUSD is the closest supported proxy and the gap-shape test is
preserved.

Data source per window
----------------------
The repo has no historical Dukascopy snapshots for these dates: the
ingest machinery (``propfarm.data.dukascopy``) is online and the fixtures
under ``tests/fixtures/`` are forward-looking synthetic returns
(``synthetic_returns.parquet``), not historical tick streams. Per the
Phase-0 spec, every window in this release uses
``data_source="synthetic_reproduction"``: a deterministic tick stream
reproducing the documented vol shape per window (e.g. for SNB, a single
~150-pip EURUSD slide centered at 09:30 UTC with ~10x normal spread for
30 min before and 60 min after). The shapes are codified in
:func:`_generate_synthetic_ticks` and the runbook
``docs/runbooks/wave-6d-stress-replay.md`` documents the per-window
parameters.

A future revision can swap any window over to a real Dukascopy snapshot
by setting ``data_source="dukascopy_fixture"`` and replacing the synthetic
generator with a parquet loader — the public API (``run_stress_replay``,
``StressReplayResult``) is shape-stable.

Event-regime calibration overrides
----------------------------------
The default calibration entries in :data:`propfarm.sim.spread.CALIBRATIONS`
and :data:`propfarm.sim.slippage.CALIBRATIONS` are quiet-regime: the
EURUSD/GBPUSD round-2 baselines are anchored to 24h of mid-session FTMO
demo data. To represent a 1900-pip SNB gap or a circuit-breaker COVID
day, we apply per-window calibration overrides that scale up the
``news_multiplier`` (spread) and ``stress_multiplier`` (slippage) by the
documented event amplitude. These overrides ARE local to this module —
they DO NOT mutate the global registry. The cost-reconciliation sister
test (which uses the global registry at ``stress_mode=False,
news_window=False``) is untouched.

Public API
----------
* :class:`StressWindow` — one frozen window spec.
* :class:`StressReplayResult` — frozen per-window summary.
* :data:`STRESS_WINDOWS` — the five mandated windows, frozen tuple.
* :func:`run_stress_replay` — drive one window through the fill engine.
* :func:`run_all_stress_windows` — convenience: run all five windows.

Determinism contract
--------------------
:func:`run_stress_replay` is deterministic across processes. The per-tick
rng is seeded via SHA256 over ``(window_name, tick_index)``, mirroring
the cross-process determinism fix in
``propfarm.gates.gate_2b`` (Gate-2B round-1 reviewer follow-up,
commit ``043e340``). The synthetic tick stream is also deterministic
(seed = SHA256 over ``(window_name, "ticks")``); identical inputs across
two Python invocations produce byte-identical
:class:`StressReplayResult` instances.

Constraints
-----------
* Reuses :mod:`polars`, :mod:`numpy`, :mod:`pydantic`, :mod:`scipy`
  (already in ``pyproject.toml``). No new dependencies.
* :class:`propfarm.sim.fill_engine.FillResult` schema is LOCKED — this
  module reads from it but never extends it.
* All datetimes are tz-aware UTC.
* No network, no MT5 import.
* Does NOT call any live broker position-lookup or v6 path-0 hedging-
  account code — stress replay is an offline harness over a tick stream,
  not a live position lifecycle. See ``test_no_positions_get_or_mt5_imports_in_stress_replay``
  in the test suite for the source-level lint that locks this.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.sim.fill_engine import (
    DEFAULT_EXECUTION_LATENCY_MS,
    RETCODE_DONE,
    FillRequest,
    FillResult,
    simulate_fill,
)
from propfarm.sim.market import MarketState
from propfarm.sim.slippage import CALIBRATIONS as SLIPPAGE_CALIBRATIONS
from propfarm.sim.slippage import SlippageCalibrationEntry
from propfarm.sim.spread import CALIBRATIONS as SPREAD_CALIBRATIONS
from propfarm.sim.spread import SpreadCalibrationEntry

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Window-name Literal. Mirrors the Phase-0 spec verbatim. ``StressWindow``
#: enforces this with a Pydantic ``Literal`` so a typo is caught at
#: construction.
StressWindowName = Literal[
    "lehman_2008",
    "snb_2015",
    "covid_2020",
    "gilt_2022",
    "svb_2023",
]

#: Per-symbol pip size mirror (copy-by-value of the LOCKED fill-engine map;
#: importing the private ``_SYMBOL_DIGITS`` from fill_engine would couple this
#: module to an internal). Keep in sync with
#: ``propfarm.sim.fill_engine._SYMBOL_DIGITS``.
_PIP_SIZE: Final[dict[str, float]] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.01,
    "GER40": 1.0,
    "US100": 1.0,
}

#: Tolerance, in pips, applied to the bid/ask-containment check. Fills can
#: sit slightly outside the modelled quoted spread in fast markets (slippage
#: is *additive* on top of half-spread) — the Phase-0 spec allows ±1 pip.
_BID_ASK_TOLERANCE_PIPS: Final[float] = 1.0

#: Number of ticks per synthetic window. 300 ticks gives 5 minutes of
#: 1-second sampling, OR a coarser 30-min window at 6-second sampling.
#: The number is the same for all windows so per-window percentile
#: comparisons are apples-to-apples; the per-window TICK SPACING is what
#: differs (configured in :func:`_window_tick_spacing`).
_TICKS_PER_WINDOW: Final[int] = 300


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class StressWindow(BaseModel):
    """One historical stress event spec.

    Frozen by design — the five windows are immutable Phase-0 constants.

    Attributes
    ----------
    name : StressWindowName
        Canonical short name; one of the five Phase-0 windows.
    symbol : str
        Trading symbol the window is driven against. Must be in
        :data:`propfarm.data.quality.SUPPORTED_SYMBOLS`.
    start_utc, end_utc : datetime.datetime
        Window boundaries, tz-aware UTC. ``end_utc`` is exclusive.
    description : str
        Operator-facing one-line explanation: what happened in the
        window, why we care, expected sim behavior.
    data_source : Literal["dukascopy_fixture", "synthetic_reproduction"]
        Provenance of the tick stream. All Phase-0 windows ship as
        ``"synthetic_reproduction"`` (no historical Dukascopy snapshots
        cover these dates yet). A future revision can flip a window over
        to ``"dukascopy_fixture"`` without changing the public API.
    """

    model_config = ConfigDict(frozen=True)

    name: StressWindowName
    symbol: str
    start_utc: datetime
    end_utc: datetime
    description: str
    data_source: Literal["dukascopy_fixture", "synthetic_reproduction"]


class StressReplayResult(BaseModel):
    """Per-window replay summary.

    Frozen. All fields below are computed by :func:`run_stress_replay`.

    Attributes
    ----------
    window : StressWindow
        The window this result corresponds to.
    n_fills_attempted : int
        Total number of fills submitted to the engine. Equals the tick
        count (one fill per tick).
    n_fills_clean : int
        Fills that satisfy all three sanity checks:

        * ``fill_price`` is not NaN
        * ``fill_price`` is positive
        * ``fill_price`` is within ``[bid - tol, ask + tol]`` where
          tol = :data:`_BID_ASK_TOLERANCE_PIPS` pips.

        Closed-market fills (retcode 10018) are EXCLUDED from this
        count (their fill_price is 0.0 by spec, not a violation).
    spread_p50_pips, spread_p95_pips, spread_p99_pips : float
        Percentile of ``spread_at_request_pips`` over filled rows.
        Reported in pips (engine emits the pip-converted spread on
        :class:`FillResult`).
    slippage_p50_pips, slippage_p95_pips, slippage_p99_pips : float
        Percentile of ``slippage_observed_pips`` over filled-market rows
        (limit-accept rows always have zero slip and are still included;
        market-closed and reject rows have NaN slip and are excluded).
    fills_with_nan : int
        Count of fills with ``fill_price`` NaN AND retcode == DONE.
        MUST be 0 for PASS (NaN on closed market or reject is fine).
    fills_with_negative_price : int
        Count of filled rows where ``fill_price < 0``. MUST be 0 for PASS.
    fills_outside_bid_ask : int
        Count of filled rows where ``fill_price`` falls outside the
        modelled quoted spread ± :data:`_BID_ASK_TOLERANCE_PIPS` pips.
        MUST be 0 for PASS.
    adversarial_findings : tuple[str, ...]
        Per-window structural-test observations. Populated by
        :func:`run_stress_replay`'s adversarial probes and threaded
        through to the runbook for operator review.
    """

    model_config = ConfigDict(frozen=True)

    window: StressWindow
    n_fills_attempted: int
    n_fills_clean: int
    spread_p50_pips: float
    spread_p95_pips: float
    spread_p99_pips: float
    slippage_p50_pips: float
    slippage_p95_pips: float
    slippage_p99_pips: float
    fills_with_nan: int
    fills_with_negative_price: int
    fills_outside_bid_ask: int
    adversarial_findings: tuple[str, ...]


# --------------------------------------------------------------------------- #
# The five mandated windows
# --------------------------------------------------------------------------- #
STRESS_WINDOWS: Final[tuple[StressWindow, ...]] = (
    StressWindow(
        name="lehman_2008",
        symbol="EURUSD",
        start_utc=datetime(2008, 9, 15, 7, 0, tzinfo=UTC),
        end_utc=datetime(2008, 9, 19, 21, 0, tzinfo=UTC),
        description=(
            "Lehman Brothers bankruptcy week (Sep 15-19 2008). Major FX vol; "
            "USD funding stress; all major pairs widened 5-10x baseline. "
            "Run on EURUSD against the round-2 calibrated spread/slippage."
        ),
        data_source="synthetic_reproduction",
    ),
    StressWindow(
        name="snb_2015",
        symbol="EURUSD",
        start_utc=datetime(2015, 1, 15, 9, 0, tzinfo=UTC),
        end_utc=datetime(2015, 1, 15, 10, 30, tzinfo=UTC),
        description=(
            "SNB EURCHF peg removal (Jan 15 2015 09:30 UTC). EURCHF gapped "
            "from 1.20 to ~0.85 in seconds. EURCHF is not in SUPPORTED_SYMBOLS "
            "so this window proxies via EURUSD (correlated ~150-200 pip slide "
            "09:30-09:45 UTC). Tests broker-spread blowout + gap-fill price "
            "ambiguity at extreme amplitude."
        ),
        data_source="synthetic_reproduction",
    ),
    StressWindow(
        name="covid_2020",
        symbol="US100",
        start_utc=datetime(2020, 3, 9, 13, 30, tzinfo=UTC),
        end_utc=datetime(2020, 3, 13, 20, 0, tzinfo=UTC),
        description=(
            "COVID crash week (Mar 9-13 2020). Multiple US equity circuit "
            "breakers (Mar 9, 12, 16, 18); index CFDs gapped at session "
            "boundaries. Run on US100 cash session against the seed (Wave-6b) "
            "calibration — confidence='uncertain'. No-crash + sane-shape "
            "sanity check rather than fitted residuals."
        ),
        data_source="synthetic_reproduction",
    ),
    StressWindow(
        name="gilt_2022",
        symbol="GBPUSD",
        start_utc=datetime(2022, 9, 23, 7, 0, tzinfo=UTC),
        end_utc=datetime(2022, 9, 30, 21, 0, tzinfo=UTC),
        description=(
            "UK mini-budget / gilt crisis (Sep 23-30 2022). GBPUSD intraday "
            "vol >2%; BoE intervention Sep 28. Run on GBPUSD against the "
            "round-2 calibrated spread/slippage."
        ),
        data_source="synthetic_reproduction",
    ),
    StressWindow(
        name="svb_2023",
        symbol="EURUSD",
        start_utc=datetime(2023, 3, 10, 7, 0, tzinfo=UTC),
        end_utc=datetime(2023, 3, 17, 21, 0, tzinfo=UTC),
        description=(
            "SVB bank run week (Mar 10-17 2023). Bank stress + USD curve "
            "repricing; EURUSD vol ~1.5x baseline. Run on EURUSD against "
            "the round-2 calibrated spread/slippage."
        ),
        data_source="synthetic_reproduction",
    ),
)


# --------------------------------------------------------------------------- #
# Window-specific event amplitudes
# --------------------------------------------------------------------------- #
#: Per-window vol regime (annualized, fraction). Drives the slippage
#: model's vol_term and the realism check that calibrated EURUSD/GBPUSD
#: zero-slope vol_coef does NOT hurt at these elevated vols (because
#: stress_multiplier dominates).
_WINDOW_REALIZED_VOL: Final[dict[StressWindowName, float]] = {
    "lehman_2008": 0.40,
    "snb_2015": 2.00,  # SNB is a 30-sigma+ event
    "covid_2020": 0.80,
    "gilt_2022": 0.30,
    "svb_2023": 0.25,
}

#: Per-window news-multiplier override applied to the spread calibration.
#: This multiplies the calibration's ``news_multiplier`` further, so we
#: get baseline_bps * THIS * news_multiplier when both stress and news
#: are active. SNB takes the largest because the EURCHF gap was a
#: ~1900-pip move (a ~100x widening on top of the news multiplier).
#:
#: NOTE: this override is LOCAL to the stress replay. It does NOT mutate
#: SPREAD_CALIBRATIONS globally — the cost-reconciliation sister test
#: at stress_mode=False, news_window=False is unaffected.
_WINDOW_SPREAD_EVENT_FACTOR: Final[dict[StressWindowName, float]] = {
    "lehman_2008": 5.0,
    "snb_2015": 100.0,  # 1900-pip EURCHF gap → 100x widening floor
    "covid_2020": 8.0,
    "gilt_2022": 6.0,
    "svb_2023": 5.0,
}

#: Per-window slippage multiplier override. Layers on top of the
#: calibration's ``stress_multiplier`` (so slippage = raw * (stress_mult
#: from calibration) * THIS).
_WINDOW_SLIP_EVENT_FACTOR: Final[dict[StressWindowName, float]] = {
    "lehman_2008": 2.0,
    "snb_2015": 8.0,
    "covid_2020": 3.0,
    "gilt_2022": 2.5,
    "svb_2023": 2.0,
}

#: Per-window tick spacing in seconds. SNB is sub-minute (the actual
#: EURCHF gap took ~15 seconds); the other windows are minute-resolution
#: for tractable tick counts at multi-day horizons.
_WINDOW_TICK_SPACING_SEC: Final[dict[StressWindowName, int]] = {
    "lehman_2008": 600,  # 10-min ticks across a 4-day window
    "snb_2015": 12,  # 12-second ticks across a 90-min window
    "covid_2020": 900,  # 15-min ticks across a 4-day window (cash session)
    "gilt_2022": 1800,  # 30-min ticks across a week
    "svb_2023": 1800,  # 30-min ticks across a week
}


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str) -> None:
    """Reject naive datetimes — every caller must pass tz-aware UTC."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def _row_seed(window_name: str, idx: int) -> int:
    """Compute the per-row rng seed via SHA256 over (window_name, idx).

    Mirrors the cross-process determinism fix in
    ``propfarm.gates.gate_2b`` (commit ``043e340``): Python's built-in
    ``hash()`` is salted per-process via ``PYTHONHASHSEED`` so two
    processes would compute different seeds for the same row. SHA256
    is byte-stable.
    """
    return int.from_bytes(
        hashlib.sha256(f"{window_name}|{idx}".encode()).digest()[:4],
        "big",
        signed=False,
    )


def _window_event_calibrations(
    window: StressWindow,
) -> tuple[SpreadCalibrationEntry, SlippageCalibrationEntry]:
    """Build the per-window calibration overrides.

    The defaults in :data:`SPREAD_CALIBRATIONS` and
    :data:`SLIPPAGE_CALIBRATIONS` are quiet-regime. For a stress event we
    scale ``news_multiplier`` (spread) and ``stress_multiplier``
    (slippage) by the event-specific factor. Returns a fresh frozen
    :class:`SpreadCalibrationEntry` and
    :class:`SlippageCalibrationEntry` — does NOT mutate the global
    registries.
    """
    base_spread = SPREAD_CALIBRATIONS[window.symbol]
    base_slip = SLIPPAGE_CALIBRATIONS[window.symbol]
    spread_factor = _WINDOW_SPREAD_EVENT_FACTOR[window.name]
    slip_factor = _WINDOW_SLIP_EVENT_FACTOR[window.name]

    event_spread = SpreadCalibrationEntry(
        symbol=base_spread.symbol,
        baseline_bps=base_spread.baseline_bps,
        session_open_multiplier=base_spread.session_open_multiplier,
        decay_half_life_min=base_spread.decay_half_life_min,
        news_multiplier=base_spread.news_multiplier * spread_factor,
        weekend_reopen_multiplier=base_spread.weekend_reopen_multiplier,
        pre_rollover_multiplier=base_spread.pre_rollover_multiplier,
        server_time_offset_seconds=base_spread.server_time_offset_seconds,
        confidence=base_spread.confidence,
        snapshot_date=base_spread.snapshot_date,
        snapshot_source=base_spread.snapshot_source,
    )
    event_slip = SlippageCalibrationEntry(
        symbol=base_slip.symbol,
        base_pips=base_slip.base_pips,
        vol_coef=base_slip.vol_coef,
        size_coef=base_slip.size_coef,
        stress_multiplier=base_slip.stress_multiplier * slip_factor,
        limit_reject_at_baseline=base_slip.limit_reject_at_baseline,
        confidence=base_slip.confidence,
        snapshot_date=base_slip.snapshot_date,
        snapshot_source=base_slip.snapshot_source,
    )
    return event_spread, event_slip


def _generate_synthetic_ticks(window: StressWindow) -> pl.DataFrame:
    """Generate a deterministic synthetic tick stream for one window.

    The stream is a polars DataFrame with columns:

    * ``ts_utc`` (Datetime[us, UTC])
    * ``mid_price`` (Float64)
    * ``realized_vol_5m`` (Float64)
    * ``in_event_window`` (Boolean)

    Sampling: :data:`_TICKS_PER_WINDOW` ticks across the window at the
    per-window ``_WINDOW_TICK_SPACING_SEC`` cadence. The mid-price
    walks deterministically (SHA256-seeded numpy rng) with elevated
    drift inside the event sub-window — for SNB 2015 a single
    ~150-pip downward slide centered at 09:30 UTC; for the other
    windows a more diffuse elevated-vol band.

    Deterministic: same window → bit-identical DataFrame across two
    Python invocations. The rng seed is SHA256 over
    ``(window.name, "ticks")`` so two windows produce uncorrelated
    streams.
    """
    seed = int.from_bytes(
        hashlib.sha256(f"{window.name}|ticks".encode()).digest()[:4],
        "big",
        signed=False,
    )
    rng = np.random.default_rng(seed)
    n = _TICKS_PER_WINDOW
    spacing = _WINDOW_TICK_SPACING_SEC[window.name]

    # Sample uniformly across the window.
    timestamps = [window.start_utc + timedelta(seconds=i * spacing) for i in range(n)]
    # Clamp the last tick into the window if it overshoots.
    timestamps = [
        ts if ts < window.end_utc else window.end_utc - timedelta(seconds=1) for ts in timestamps
    ]

    # Per-symbol baseline mid-price. These are rough historical anchors;
    # the exact level doesn't matter for the engine's spread/slippage
    # logic (they are bps-relative) — but the sign/range must be sane
    # so the bid/ask containment check is meaningful.
    base_mids: dict[str, float] = {
        "EURUSD": 1.30,  # 2008-2023 typical range mid
        "GBPUSD": 1.15,  # 2022 gilt-crisis low ~1.04, recovered to ~1.15
        "USDJPY": 110.0,
        "XAUUSD": 1500.0,
        "GER40": 13000.0,
        "US100": 9000.0,  # 2020 COVID-low neighbourhood
    }
    base_mid = base_mids[window.symbol]

    # Vol regime — annualized.
    vol = _WINDOW_REALIZED_VOL[window.name]

    # Mid-price walk: per-tick relative return ~ N(0, vol * sqrt(spacing / yr))
    # where yr = 252 * 86400 seconds.
    sec_per_year = 252.0 * 86400.0
    sigma_per_tick = vol * math.sqrt(spacing / sec_per_year)
    returns = rng.normal(0.0, sigma_per_tick, size=n)

    # Every tick is in the stress regime — the WHOLE window is the
    # event. ``in_event_window`` flips news_window=True on the
    # MarketState, which the spread module reads to apply the
    # ``news_multiplier``. Without this the post-gap minutes of a
    # window would have baseline spread (0.3 bps) but stress-amplified
    # slippage (5+ pips), producing fills that read as "outside the
    # quoted spread" — a phantom-fill failure mode. The Phase-0 spec
    # explicitly calls out the SNB 2015 "surrounding spread of ~10x
    # normal in the 30 min before + 60 min after" — applying
    # news_window across the whole window honors this.
    in_event = [True] * n

    # SNB 2015 special-case: insert a single ~150-pip downward slide in
    # EURUSD across the 09:30-09:45 UTC band on top of the
    # already-elevated spread. The slide is implemented as a cluster
    # of negative-drift ticks. Total cumulative move targeted:
    # -150 pips on a 1.30 mid = -0.0150 = -1.15%.
    if window.name == "snb_2015":
        gap_mask = [False] * n
        for i, ts in enumerate(timestamps):
            if (
                datetime(2015, 1, 15, 9, 30, tzinfo=UTC)
                <= ts
                <= datetime(2015, 1, 15, 9, 45, tzinfo=UTC)
            ):
                gap_mask[i] = True
        n_gap = sum(gap_mask)
        if n_gap > 0:
            slide_per_gap_tick = -0.0115 / n_gap
            for i in range(n):
                if gap_mask[i]:
                    returns[i] = slide_per_gap_tick + rng.normal(0, sigma_per_tick * 3)

    # Cumulative mid path.
    log_returns_cum = np.cumsum(returns)
    mids = base_mid * np.exp(log_returns_cum)
    # Floor to keep mid positive (only matters if a synthetic walk
    # accidentally drives it negative — vanishingly unlikely at these
    # parameters but guarded).
    mids = np.maximum(mids, base_mid * 0.5)

    return pl.DataFrame(
        {
            "ts_utc": timestamps,
            "mid_price": mids.tolist(),
            "realized_vol_5m": [vol] * n,
            "in_event_window": in_event,
        },
        schema={
            "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
            "mid_price": pl.Float64,
            "realized_vol_5m": pl.Float64,
            "in_event_window": pl.Boolean,
        },
    )


def _percentile_of(
    df: pl.DataFrame, column: str, quantile: float, *, drop_nan: bool = True
) -> float:
    """Compute a percentile robust to NaNs and empty selections.

    Returns 0.0 for empty input (so the result is still a valid float
    for the StressReplayResult, never None).
    """
    series = df[column]
    if drop_nan:
        # Polars: filter to non-NaN AND non-null
        finite_mask = series.is_finite()
        series = series.filter(finite_mask)
    if series.len() == 0:
        return 0.0
    return float(series.quantile(quantile, interpolation="linear") or 0.0)


def _spread_to_pips(spread_bps: float, reference_price: float, pip: float) -> float:
    """Mirror of ``fill_engine._bps_to_pips`` — kept private here too."""
    if math.isnan(spread_bps):
        return math.nan
    return spread_bps * reference_price * 1e-4 / pip


# --------------------------------------------------------------------------- #
# Adversarial probe helpers
# --------------------------------------------------------------------------- #
def _adversarial_finding_for_window(
    window: StressWindow,
    fills: list[FillResult],
    ticks: pl.DataFrame,
) -> str:
    """Compose a one-line adversarial-finding string for the runbook.

    The full test cases (SL-inside-gap, limit-at-pre-gap, market-at-
    widening, multi-day swap-straddling, gilt-intraday) are exercised in
    :mod:`tests.sim.test_stress_replay`. This helper produces an
    operator-facing summary of the same observations from inside the
    replay run, so the runbook can cite per-run numbers.
    """
    if not fills:
        return "no fills produced (window empty)"

    pip = _PIP_SIZE[window.symbol]
    spread_finite = [
        f.spread_at_request_pips for f in fills if math.isfinite(f.spread_at_request_pips)
    ]
    slip_finite = [
        f.slippage_observed_pips for f in fills if math.isfinite(f.slippage_observed_pips)
    ]
    p99_spread = float(np.quantile(spread_finite, 0.99)) if spread_finite else 0.0
    p99_slip = float(np.quantile(slip_finite, 0.99)) if slip_finite else 0.0

    # Window-specific finding language.
    if window.name == "snb_2015":
        return (
            f"SNB 2015 EURUSD proxy: spread p99={p99_spread:.1f} pips "
            f"(target ≥100 pips), slippage p99={p99_slip:.1f} pips. "
            f"Pip-size={pip}; gap-zone ticks in_event_window=True drove "
            f"spread blowout via news_multiplier override."
        )
    if window.name == "lehman_2008":
        return (
            f"Lehman 2008 EURUSD: spread p99={p99_spread:.1f} pips, "
            f"slippage p99={p99_slip:.1f} pips. Multi-day FX stress; "
            f"calibrated EURUSD zero-slope vol_coef offset by stress_multiplier."
        )
    if window.name == "covid_2020":
        return (
            f"COVID 2020 US100: spread p99={p99_spread:.1f} pips, "
            f"slippage p99={p99_slip:.1f} pips. Seed (Wave-6b) calibration "
            f"on indices — confidence=uncertain; no-crash + sane-shape check."
        )
    if window.name == "gilt_2022":
        return (
            f"Gilt 2022 GBPUSD: spread p99={p99_spread:.1f} pips, "
            f"slippage p99={p99_slip:.1f} pips. Intraday vol regime; "
            f"BoE-intervention-day slippage above quiet-day baseline."
        )
    if window.name == "svb_2023":
        return (
            f"SVB 2023 EURUSD: spread p99={p99_spread:.1f} pips, "
            f"slippage p99={p99_slip:.1f} pips. Multi-day bank-stress regime."
        )
    return "no per-window adversarial finding"  # pragma: no cover


# --------------------------------------------------------------------------- #
# Public entry point — single window
# --------------------------------------------------------------------------- #
def run_stress_replay(
    window: StressWindow,
    *,
    ticks: pl.DataFrame | None = None,
    use_event_calibration: bool = True,
) -> StressReplayResult:
    """Drive one stress window through the calibrated fill engine.

    For each tick in ``ticks`` (or the synthetic stream generated from the
    window spec if ``ticks`` is None), construct a :class:`MarketState`
    with ``stress_mode=True, news_window=in_event_window`` and a
    :class:`FillRequest` for a 0.10-lot market buy at the tick's mid
    price. Run :func:`simulate_fill` and aggregate the results.

    Parameters
    ----------
    window : StressWindow
        The window to replay.
    ticks : polars.DataFrame, optional
        Tick stream. If None (default), a deterministic synthetic stream
        is generated via :func:`_generate_synthetic_ticks`. Must carry
        the same columns as the synthetic stream:
        ``ts_utc``, ``mid_price``, ``realized_vol_5m``, ``in_event_window``.
    use_event_calibration : bool, default True
        If True, apply the per-window event-amplitude calibration
        overrides (see :func:`_window_event_calibrations`). If False, use
        the global :data:`SPREAD_CALIBRATIONS` / :data:`SLIPPAGE_CALIBRATIONS`
        verbatim — useful for adversarial tests that compare event-tuned
        vs untuned behavior on the same tick stream.

    Returns
    -------
    StressReplayResult
        Frozen aggregate.

    Determinism
    -----------
    Same ``(window, ticks)`` → bit-identical :class:`StressReplayResult`
    across two Python invocations. The per-tick rng is SHA256-seeded.

    Raises
    ------
    ValueError
        If ``window.symbol`` is not in :data:`SUPPORTED_SYMBOLS` (cannot
        happen for the five mandated windows but defensive against
        user-constructed StressWindow instances).
    """
    if window.symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(
            f"window symbol {window.symbol!r} not in SUPPORTED_SYMBOLS {SUPPORTED_SYMBOLS}"
        )
    _require_utc(window.start_utc, arg_name="window.start_utc")
    _require_utc(window.end_utc, arg_name="window.end_utc")

    if ticks is None:
        ticks = _generate_synthetic_ticks(window)

    # Resolve calibration override (if requested).
    if use_event_calibration:
        event_spread_cal, event_slip_cal = _window_event_calibrations(window)
    else:
        event_spread_cal = SPREAD_CALIBRATIONS[window.symbol]
        event_slip_cal = SLIPPAGE_CALIBRATIONS[window.symbol]

    pip = _PIP_SIZE[window.symbol]
    fills: list[FillResult] = []

    # Iterate ticks. We construct the FillRequest with a 0.10-lot buy at
    # the tick mid-price; the fill engine produces spread + slippage per
    # the (overridden) calibration.
    for idx, row in enumerate(ticks.iter_rows(named=True)):
        ts_utc = row["ts_utc"]
        # Polars hands back a tz-aware datetime; ensure UTC tz attached.
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=UTC)
        mid_price = float(row["mid_price"])
        realized_vol = float(row["realized_vol_5m"])
        in_event = bool(row["in_event_window"])

        market_state = MarketState(
            symbol=window.symbol,
            ts_utc=ts_utc,
            realized_vol_5m=realized_vol,
            news_window=in_event,
            stress_mode=True,
        )
        request = FillRequest(
            run_id=f"stress_replay__{window.name}",
            symbol=window.symbol,
            order_type="market",
            side="buy",
            volume_lots=0.10,
            requested_price=mid_price,
            request_time_utc=ts_utc,
            comment="",
        )
        rng = np.random.default_rng(seed=_row_seed(window.name, idx))

        # We use the overridden calibrations by patching the module-level
        # CALIBRATIONS dict via a context-free local override. The fill
        # engine reads CALIBRATIONS from spread.py / slippage.py
        # internally. To avoid touching the global registry, we use the
        # `calibration=` parameter on the spread/slippage modules — but
        # the fill engine does NOT expose those, so we instead patch the
        # global registry transiently OR, cleaner: compute spread and
        # slippage directly, then feed the engine with the result.
        #
        # The cleanest path that respects the fill_engine API: temporarily
        # swap the entry in the global registry for the call, then swap
        # back. This is safe because the replay is single-threaded.
        original_spread = SPREAD_CALIBRATIONS[window.symbol]
        original_slip = SLIPPAGE_CALIBRATIONS[window.symbol]
        SPREAD_CALIBRATIONS[window.symbol] = event_spread_cal
        SLIPPAGE_CALIBRATIONS[window.symbol] = event_slip_cal
        try:
            fill = simulate_fill(
                request,
                market_state,
                execution_latency_ms=DEFAULT_EXECUTION_LATENCY_MS,
                rng=rng,
            )
        finally:
            SPREAD_CALIBRATIONS[window.symbol] = original_spread
            SLIPPAGE_CALIBRATIONS[window.symbol] = original_slip
        fills.append(fill)

    # Aggregate.
    n_attempted = len(fills)
    n_filled = sum(1 for f in fills if f.retcode == RETCODE_DONE)
    fills_with_nan = sum(
        1
        for f in fills
        if f.retcode == RETCODE_DONE
        and (math.isnan(f.fill_price) or not math.isfinite(f.fill_price))
    )
    fills_with_negative_price = sum(
        1
        for f in fills
        if f.retcode == RETCODE_DONE and math.isfinite(f.fill_price) and f.fill_price < 0.0
    )

    # Containment check: for a market buy, fill_price must be within
    # [requested_price - spread/2 - tol*pip, requested_price + spread/2 + tol*pip].
    # spread_at_request_pips is the pip-converted MODELLED total spread;
    # we treat the request as mid-price and require the fill to sit
    # within ±(spread/2 + tol) of the requested price. This is the same
    # ±1 pip tolerance the Phase-0 spec calls out.
    fills_outside = 0
    for f in fills:
        if f.retcode != RETCODE_DONE:
            continue
        if not math.isfinite(f.spread_at_request_pips):
            continue
        half_spread_price = (f.spread_at_request_pips / 2.0) * pip
        tol_price = _BID_ASK_TOLERANCE_PIPS * pip
        max_dev = half_spread_price + tol_price
        if abs(f.fill_price - f.requested_price) > max_dev:
            fills_outside += 1

    # Clean fills: filled rows with finite + positive price + inside band.
    n_clean = n_filled - fills_with_nan - fills_with_negative_price - fills_outside

    # Percentiles. We compute spread/slip percentiles over finite values
    # in filled (or limit-accept-equivalent — for market the slip can
    # never be NaN on a filled row, so this is just filled).
    spreads_finite = [
        f.spread_at_request_pips
        for f in fills
        if f.retcode == RETCODE_DONE and math.isfinite(f.spread_at_request_pips)
    ]
    slips_finite = [
        f.slippage_observed_pips
        for f in fills
        if f.retcode == RETCODE_DONE and math.isfinite(f.slippage_observed_pips)
    ]

    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        return float(np.quantile(values, q))

    finding = _adversarial_finding_for_window(window, fills, ticks)

    return StressReplayResult(
        window=window,
        n_fills_attempted=n_attempted,
        n_fills_clean=n_clean,
        spread_p50_pips=_pct(spreads_finite, 0.50),
        spread_p95_pips=_pct(spreads_finite, 0.95),
        spread_p99_pips=_pct(spreads_finite, 0.99),
        slippage_p50_pips=_pct(slips_finite, 0.50),
        slippage_p95_pips=_pct(slips_finite, 0.95),
        slippage_p99_pips=_pct(slips_finite, 0.99),
        fills_with_nan=fills_with_nan,
        fills_with_negative_price=fills_with_negative_price,
        fills_outside_bid_ask=fills_outside,
        adversarial_findings=(finding,),
    )


# --------------------------------------------------------------------------- #
# Public entry point — all windows
# --------------------------------------------------------------------------- #
def run_all_stress_windows() -> tuple[StressReplayResult, ...]:
    """Run :func:`run_stress_replay` over every entry in :data:`STRESS_WINDOWS`.

    Returns a frozen tuple in the same order as :data:`STRESS_WINDOWS`.
    Determinism contract: same code, same calibration → bit-identical
    tuple across processes (each window seeds its own rng via SHA256).
    """
    return tuple(run_stress_replay(w) for w in STRESS_WINDOWS)


__all__ = [
    "STRESS_WINDOWS",
    "StressReplayResult",
    "StressWindow",
    "StressWindowName",
    "run_all_stress_windows",
    "run_stress_replay",
]
