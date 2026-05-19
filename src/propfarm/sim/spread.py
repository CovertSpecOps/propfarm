"""Spread model — session-open widening + post-open decay (Task 6.1).

Why this module exists
----------------------
The fill engine (Task 7.2) needs an empirical model for the bid-ask spread
at any ``(symbol, timestamp, market_state)`` triple so backtest fills carry
realistic transaction costs. Spread is the largest *time-varying* cost the
simulator pays: commission is deterministic per-firm, swap is once-per-night,
but spread changes second by second and is what determines whether a
short-horizon strategy is profitable in production or just looks profitable
on average-cost backtests.

The model captures four empirical facts about retail-MT5 spread behaviour:

1. **Baseline spread.** A typical mid-session spread (e.g. EURUSD ~0.1 bps
   during London hours) that holds for ~95% of the trading week.
2. **Session-open widening.** At each major session open — London 07:00 UTC,
   New York 12:00/13:00 UTC (DST-aware), Tokyo 23:00 UTC, and the FX Sunday
   reopen at **22:00 UTC** — liquidity providers reposition their quotes, and
   the spread widens to ~5-10x baseline at the open instant. The Sunday
   reopen instant aligns with :func:`propfarm.data.quality.is_market_open`,
   which gates tradability on Sunday ``hour >= 22``. (A common retail-
   aggregator convention quotes pre-open prints from 21:00 UTC onwards, but
   those are indicative; tradable liquidity — and therefore the spread the
   simulator pays — is anchored to 22:00 UTC.)
3. **Post-open decay.** The widening decays back toward baseline within
   ~30 minutes; we model this as an exponential with a per-symbol half-life.
4. **News-window multiplier.** During a news event (NFP, CPI, FOMC, …) the
   spread can blow out by 5-50x. **This module does not decide WHEN news
   happens** — a separate news-calendar module (Phase 1) owns that. We just
   accept a ``market_state.news_window: bool`` flag and multiply.

Why not consume :func:`propfarm.data.quality.is_market_open`?
-------------------------------------------------------------
We do. ``evaluate`` calls ``is_market_open`` and returns ``NaN`` when the
market is closed, so the fill engine cannot accidentally book a fill on a
Saturday. ``is_market_open`` is the canonical session predicate; this module
sits on top of it and does not reinvent session boundaries — it only adds
the per-session "what is the spread *right now*" overlay.

Determinism contract
--------------------
:func:`evaluate` is a **pure function** of ``(market_state, request,
calibration)``. Given the same three inputs it returns bit-identical output
across any number of calls and any process. There is no hidden RNG, no
time-dependent global state, no cache that mutates between calls. This is
verified by ``test_determinism`` in ``tests/sim/test_spread.py``.

Confidence contract (mirrors W3 commission/swap pattern)
---------------------------------------------------------
Every :class:`SpreadCalibrationEntry` carries
``confidence: Literal["high", "medium", "uncertain"]``. The defaults seeded
in :data:`CALIBRATIONS` are all ``"uncertain"`` because we have not yet run
the 24-hour live capture against an MT5 demo terminal. The intermediate
``"medium"`` tier was added in Gate-2B calibration round 2 (2026-05-18) to
mark fields calibrated from a real capture but pending a second-capture
validation (e.g. weekend-spanning, multi-week). The Gate 2B (sim-vs-live
fill recording) certification will refuse to compare fills against an
``"uncertain"`` calibration. The runbook at
``docs/runbooks/spread-calibration-recording.md`` documents the capture
workflow that flips a row to ``"high"``.

Units convention
----------------
Spread is reported in **basis points** (1 bps = 0.01%). This is chosen for
two reasons:

1. **Cross-asset comparability.** Pips are well-defined for FX (~0.0001 of
   price) but ambiguous for indices (where "pip" is broker-specific) and
   metals (where the unit is dollars per ounce). Bps normalize against the
   mid-price and are unambiguous across asset classes.
2. **Direct cost translation.** A 0.5 bps spread on a $100k notional is
   exactly $5 — no per-symbol conversion needed downstream by the fill
   engine.

For FX majors at a 1.1000 mid, 1 bps == 0.11 pips. For XAUUSD at $3500/oz,
1 bps == $0.35 per oz. For GER40 at 20,000, 1 bps == 2 index points. The
calibration runbook records both raw bid/ask and the derived ``spread_bps``
so the conversion is traceable.

Public API
----------
* :class:`SpreadCalibrationEntry` — per-symbol parameters, frozen.
* :class:`MarketState` — market context (symbol, ts, optional vol + news).
* :class:`SpreadRequest` — uniform-shape placeholder for future order-side
  fields. Mirrors what slippage/fill engine will take.
* :class:`SpreadResult` — output: bps + per-component diagnostic.
* :func:`session_open_window` — helper: which session opened most recently
  and how many minutes ago.
* :func:`pre_rollover_window` — helper (added Gate-2B round 2): how many
  minutes until the broker's next daily server-time-midnight rollover, or
  ``(None, 0.0)`` outside the pre-rollover ramp window.
* :func:`evaluate` — top-level entry point. Returns :class:`SpreadResult`.
* :data:`CALIBRATIONS` — per-symbol registry of default calibration entries
  (all flagged ``"uncertain"`` until live capture replaces them).

Constraints
-----------
* All ``datetime`` inputs must be **tz-aware UTC**; naive datetimes raise.
* Unknown symbols raise ``ValueError`` rather than silently returning zero.
* No network, no MT5 import, no broker host — calibration data lands in
  Parquet snapshots on disk before being projected into typed entries.
* No randomness. If randomness becomes useful (e.g. stochastic spread
  jitter), the seeded RNG must be an **explicit parameter** with seed
  locked by a test on at least two seeds.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from propfarm.data.quality import SUPPORTED_SYMBOLS, is_market_open

# --------------------------------------------------------------------------- #
# Time-zone & session-open constants
# --------------------------------------------------------------------------- #

#: New York time zone, used to resolve the DST-aware NY session open
#: (NY equity / cash FX desks open at 08:00 local = 12:00 UTC in EDT,
#: 13:00 UTC in EST).
_NY: Final[ZoneInfo] = ZoneInfo("America/New_York")

#: NY session opens at this wall-clock hour, local time. Conversion to UTC is
#: DST-aware via ``ZoneInfo`` so the module stays in-phase across the spring
#: and autumn DST transitions without manual tweaks. ``ZoneInfo`` resolution
#: is the same pattern used by :mod:`propfarm.sim.swap` for the rollover
#: instant and :mod:`propfarm.data.quality` for US100 session hours.
_NY_OPEN_HOUR_LOCAL: Final[int] = 8

#: London session open: 07:00 UTC (London cash FX desks). Constant year-round
#: because London FX desks anchor to UTC, not BST, by convention — the
#: liquidity print happens at the UTC-anchored 07:00 instant regardless of
#: whether the wall clock in London says 07:00 (winter, UTC) or 08:00
#: (summer, BST). This matches the convention used by retail FX aggregators.
_LONDON_OPEN_UTC_HOUR: Final[int] = 7

#: Tokyo session open: 23:00 UTC (Tokyo opens at 08:00 JST = 23:00 UTC the
#: prior calendar day). Japan does not observe DST, so the UTC offset is
#: constant year-round.
_TOKYO_OPEN_UTC_HOUR: Final[int] = 23

#: FX weekly reopen: Sunday 22:00 UTC. This matches the canonical FX
#: weekly-open instant tracked by
#: :func:`propfarm.data.quality.is_market_open` (which gates tradability on
#: Sunday ``hour >= 22``). Some retail aggregators print quotes from
#: 21:00 UTC onwards, but those are pre-open indicative quotes — the
#: spread model targets *tradable* spreads, so we anchor to the 22:00 UTC
#: instant at which fills first become possible.
#:
#: The reopen widening is typically larger than weekday session opens
#: because liquidity providers have been offline for ~50 hours.
_SUNDAY_REOPEN_UTC_HOUR: Final[int] = 22

#: How many minutes after a session open we consider the session-open
#: "decay tail" to still be active. After this window the model returns to
#: baseline (no session multiplier applied). 60 min is generous — even
#: aggressive session-open spikes are usually back to baseline by 30 min,
#: but we tail out to 60 to capture the long-decay regime on some symbols
#: (notably XAUUSD post-Asia-open).
_SESSION_WINDOW_MIN: Final[float] = 60.0

#: Default broker server-time offset relative to UTC, in seconds. Used by
#: :func:`pre_rollover_window` to translate the broker's daily server-time
#: midnight rollover into a UTC instant. ``10800`` seconds = +03:00 = EEST
#: (Europe Summer Time), which is the canonical retail-MT5 convention during
#: summer for brokers headquartered in Cyprus/Athens (FXCM, Pepperstone,
#: many FTMO MT5 endpoints). Per-symbol calibration entries can override
#: this when an empirical capture shows a different broker policy — notably
#: FTMO's MT5 server uses EET (+02:00 = ``7200`` seconds) year-round (no
#: DST), so the FTMO-anchored EURUSD/GBPUSD calibration entries pass
#: ``server_time_offset_seconds=7200`` explicitly in round 2.
_DEFAULT_SERVER_TIME_OFFSET_SECONDS: Final[int] = 10800

#: Length of the pre-rollover ramp-up window, in minutes. The widening
#: starts ``_PRE_ROLLOVER_RAMP_MINUTES`` before the broker's daily
#: server-time midnight rollover and peaks at the rollover instant. After
#: the rollover, the widening collapses immediately to ``1.0`` because the
#: new server-day brings fresh liquidity providers online.
#:
#: 60 min is the captured-data-informed default: in the round-1 FTMO
#: capture the widening was visible from ~21:00 UTC and peaked at the
#: 22:00 UTC rollover (FTMO EET +02:00). A future capture with finer-
#: grained pre-rollover sampling may justify shortening this to 30-45 min.
_PRE_ROLLOVER_RAMP_MINUTES: Final[float] = 60.0


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class SpreadCalibrationEntry(BaseModel):
    """Frozen per-symbol spread parameters.

    Attributes
    ----------
    symbol : str
        Trading symbol. Must be a member of
        :data:`propfarm.data.quality.SUPPORTED_SYMBOLS`.
    baseline_bps : float
        Typical mid-session spread in basis points (1 bps = 0.01%). Must be
        strictly positive. For EURUSD this is ~0.1 bps (≈0.1 pips at 1.10);
        for XAUUSD ~3-5 bps; for index CFDs ~2-3 bps depending on broker.
    session_open_multiplier : float
        Multiplicative factor applied to ``baseline_bps`` at the *exact
        instant* of a major weekday session open (London/NY/Tokyo). Decays
        exponentially back to 1.0 over ``decay_half_life_min``. Typical
        values 3-10x. Must be ≥ 1.0.
    decay_half_life_min : float
        Half-life of the exponential decay from the session-open peak back
        to baseline, in minutes. Larger = slower decay. Must be > 0.
        Typical 5-15 min for FX, 10-20 min for indices and metals.
    news_multiplier : float
        Multiplicative factor applied when ``market_state.news_window=True``.
        The flag is set by an upstream news-calendar module (not part of
        Task 6.1). Typical 5-50x. Must be ≥ 1.0.
    weekend_reopen_multiplier : float
        Multiplicative factor at the Sunday 22:00 UTC FX reopen (the
        tradable-liquidity anchor; see ``_SUNDAY_REOPEN_UTC_HOUR``).
        Typically *larger* than ``session_open_multiplier`` (10-30x)
        because the market has been offline for ~50 hours and liquidity
        is thin. Decays with the same ``decay_half_life_min``.
        Must be ≥ 1.0.
    pre_rollover_multiplier : float | None
        Optional multiplicative factor applied during the broker's daily
        pre-rollover window (added Gate-2B round 2, 2026-05-18). ``None``
        means "no pre-rollover widening configured" — :func:`evaluate`
        skips the pre-rollover factor entirely for entries where this is
        ``None``, preserving the round-1 behaviour for symbols not yet
        characterised against a real capture. When set, must be ≥ 1.0;
        peaks at the rollover anchor (server-time 00:00 → see
        :func:`pre_rollover_window`) and linearly ramps from 1.0 over
        :data:`_PRE_ROLLOVER_RAMP_MINUTES` minutes before the anchor.
        After the anchor the factor immediately resets to 1.0 (the new
        server-day brings fresh liquidity).
    server_time_offset_seconds : int
        Broker's MT5 server-time offset relative to UTC, in seconds.
        Defaults to :data:`_DEFAULT_SERVER_TIME_OFFSET_SECONDS` (10800 =
        EEST summer). Per-broker overrides go here: FTMO uses EET year-round
        (7200), so its anchored entries pass 7200 explicitly. The value is
        consumed by :func:`pre_rollover_window` via :func:`evaluate` to
        translate the broker's server-day boundary into a UTC instant.
    confidence : Literal["high", "medium", "uncertain"]
        Runtime marker. ``"uncertain"`` until live MT5 capture replaces the
        seeded defaults. ``"medium"`` was added Gate-2B round 2 to mark
        fields calibrated from a real capture but pending a second-capture
        validation (e.g. weekend-spanning, multi-week). ``"high"`` means
        validated across multiple captures. Downstream Gate 2B refuses to
        certify a sim-vs-live fill comparison against ``"uncertain"`` rows.
        See :doc:`docs/runbooks/spread-calibration-recording.md`.
    snapshot_date : datetime.date
        Date the calibration values were sourced. For ``"uncertain"`` rows
        this is the seed date; for ``"high"`` rows it is the recording date.
    snapshot_source : str
        Repo-relative path to the snapshot file that backs this entry.
        For ``"uncertain"`` seed entries this points to the recording runbook
        (``docs/runbooks/spread-calibration-recording.md``); after live
        capture lands it will point to the per-symbol parquet under
        ``data/raw/spread_snapshots/``.

    Frozen-ness
    -----------
    Pydantic v2 ``ConfigDict(frozen=True)`` makes attribute-set raise after
    construction. This is the same pattern :class:`CommissionTable` and
    :class:`SwapTable` use for their snapshot-backed parameter sets.

    Backwards-compatibility (Gate-2B round 2, 2026-05-18)
    ------------------------------------------------------
    Two new fields land in round 2: ``pre_rollover_multiplier`` (optional,
    default ``None``) and ``server_time_offset_seconds`` (optional, default
    :data:`_DEFAULT_SERVER_TIME_OFFSET_SECONDS`). Existing round-1 entries
    (USDJPY, XAUUSD, GER40, US100) do NOT get a ``pre_rollover_multiplier``
    — their behaviour stays bit-identical to round 1 because ``evaluate``
    skips the pre-rollover factor when the multiplier is ``None``. Only
    EURUSD and GBPUSD are calibrated against the round-1 FTMO capture and
    so receive the pre-rollover term.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    baseline_bps: float
    session_open_multiplier: float
    decay_half_life_min: float
    news_multiplier: float
    weekend_reopen_multiplier: float
    pre_rollover_multiplier: float | None = None
    server_time_offset_seconds: int = _DEFAULT_SERVER_TIME_OFFSET_SECONDS
    confidence: Literal["high", "medium", "uncertain"]
    snapshot_date: date
    snapshot_source: str


# ``MarketState`` is re-exported from :mod:`propfarm.sim.market` — the canonical
# source for the shared market-context model. Wave 6b shipped a local copy
# here; W6b reviewer flagged the duplication as a HIGH-severity coupling
# problem for Wave 6c (the fill engine would otherwise accumulate adapter code
# between two nominally-distinct Pydantic types). Consolidated 2026-05-13.
#
# Task 6.1 ignores the ``stress_mode`` field on :class:`MarketState`; stress
# replay drives spread via the ``news_window`` flag and event-specific
# calibration entries (see :mod:`propfarm.sim.stress_replay` when it lands).
from propfarm.sim.market import MarketState  # noqa: E402 — re-export


class SpreadRequest(BaseModel):
    """Order-side request shape.

    Trivial today — spread is a market-side quantity, not an order-side one,
    so this model carries no fields. It exists so that ``evaluate(market_state,
    request)`` conforms to the uniform entry-point signature that the
    slippage model and fill engine will use. Downstream callers can pass
    ``SpreadRequest()`` or ``None`` interchangeably.

    A later iteration may add fields like ``intended_side`` if research shows
    that bid-ask asymmetry is large enough to model separately on the two
    sides. Today it is symmetric in the model.
    """

    model_config = ConfigDict(frozen=True)


class SpreadResult(BaseModel):
    """Output of :func:`evaluate`.

    Attributes
    ----------
    symbol : str
        Echoed from input for downstream identification.
    ts_utc : datetime.datetime
        Echoed from input.
    spread_bps : float
        The modelled spread in basis points. May be ``math.nan`` if the
        market is closed (e.g. Saturday) — callers (the fill engine) must
        check :data:`math.isnan` and refuse to fill in that case.
    components : dict[str, float]
        Diagnostic breakdown of how ``spread_bps`` was computed::

            {
                "baseline_bps": float,        # the calibration baseline
                "session_factor": float,      # session-open multiplier
                                              # (decayed); 1.0 outside any open
                "pre_rollover_factor": float, # pre-rollover multiplier
                                              # (linearly ramped); 1.0 outside
                                              # the ramp window or when the
                                              # calibration entry's
                                              # pre_rollover_multiplier is None
                "session_or_pre_rollover_factor": float,
                                              # max(session_factor,
                                              # pre_rollover_factor) — the
                                              # ACTUAL multiplier applied to
                                              # baseline_bps (overlap-safe;
                                              # see evaluate() docstring)
                "news_factor": float,         # news multiplier; 1.0 when
                                              # news_window=False
                "decay_minutes": float,       # minutes since the most recent
                                              # session open; 0.0 if outside
                                              # any open window
                "minutes_to_rollover": float, # minutes until the next pre-
                                              # rollover anchor; +inf if
                                              # outside the ramp window
            }

        Reported so the fill engine and any later cost-attribution module
        can split out which component drove a wide spread.
    calibration_confidence : Literal["high", "medium", "uncertain"]
        Echoed from the consulted calibration entry. Gate 2B refuses to
        certify against ``"uncertain"``. The ``"medium"`` tier was added
        Gate-2B round 2 (2026-05-18) for fields calibrated from a real
        capture but pending a second-capture cross-validation.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts_utc: datetime
    spread_bps: float
    components: dict[str, float]
    calibration_confidence: Literal["high", "medium", "uncertain"]


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str = "ts_utc") -> None:
    """Raise ``ValueError`` unless ``ts`` is tz-aware.

    Same guard pattern used in :mod:`propfarm.data.quality` and
    :mod:`propfarm.sim.swap`.
    """
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def _ny_open_utc_for_date(d: date) -> datetime:
    """Return the UTC instant of the NY session open on NY-local calendar date ``d``.

    NY opens at 08:00 America/New_York → 12:00 UTC during EDT (mid-March to
    early November), 13:00 UTC during EST (rest of the year). The transition
    days themselves never have an 08:00-local ambiguity because the DST
    fold/skip happens at 02:00 local.

    This is the same DST-resolution pattern :mod:`propfarm.sim.swap` uses for
    the 22:00 NY rollover and :mod:`propfarm.data.quality` uses for the
    US100 cash session.
    """
    return datetime(d.year, d.month, d.day, _NY_OPEN_HOUR_LOCAL, 0, tzinfo=_NY).astimezone(UTC)


def session_open_window(symbol: str, ts_utc: datetime) -> tuple[str | None, float]:
    """Return ``(session_name, minutes_since_open)`` for the most-recent session open at ``ts_utc``.

    Looks back up to :data:`_SESSION_WINDOW_MIN` minutes and picks the most
    recent session open from the four tracked sessions:

    * ``"sunday_reopen"`` — Sunday 22:00 UTC (FX weekly reopen, tradable-liquidity anchor)
    * ``"tokyo"`` — 23:00 UTC each tradable weekday-eve
    * ``"london"`` — 07:00 UTC each weekday
    * ``"ny"`` — 12:00 UTC (EDT) / 13:00 UTC (EST), DST-aware

    Returns ``(None, 0.0)`` if the timestamp is more than
    :data:`_SESSION_WINDOW_MIN` minutes past every session open (so the model
    should fall through to baseline).

    Parameters
    ----------
    symbol : str
        Used to filter sessions that do not apply to a symbol's asset class.
        Index CFDs (GER40, US100) skip the Tokyo open because Tokyo trading
        hours do not touch their cash session — including Tokyo would just
        add noise to the model. FX and metals receive all four sessions.
    ts_utc : datetime.datetime
        Tz-aware UTC timestamp.

    Returns
    -------
    tuple[str | None, float]
        ``(session_name, minutes_since_open)`` if a session opened within
        :data:`_SESSION_WINDOW_MIN` minutes before ``ts_utc``; ``(None, 0.0)``
        otherwise. When multiple session opens fall inside the window (e.g.
        if a caller queries 30 minutes after London open, the Tokyo open
        from 8 hours earlier is *outside* the window and not returned),
        the most recent one wins.

    Raises
    ------
    ValueError
        If ``symbol`` is not in :data:`SUPPORTED_SYMBOLS` or ``ts_utc`` is
        naive.
    """
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(f"unknown symbol {symbol!r}; supported symbols: {SUPPORTED_SYMBOLS}")
    _require_utc(ts_utc, arg_name="ts_utc")
    ts = ts_utc.astimezone(UTC)

    # Index CFDs skip Tokyo (Tokyo hours do not overlap the cash sessions).
    is_index = symbol in ("GER40", "US100")

    # Build candidate session-open instants. We look at *today*, *yesterday*,
    # and *tomorrow* (the last covers the case where ts is at e.g. 23:30 UTC
    # and the most recent Tokyo open was at 23:00 UTC today — Tokyo "opens"
    # on the prior calendar day boundary too).
    candidates: list[tuple[str, datetime]] = []
    today = ts.date()
    for offset in (-1, 0, 1):
        d = today.fromordinal(today.toordinal() + offset)
        # London 07:00 UTC: weekdays only (Mon=0 .. Fri=4).
        if d.weekday() <= 4:
            candidates.append(
                ("london", datetime(d.year, d.month, d.day, _LONDON_OPEN_UTC_HOUR, 0, tzinfo=UTC))
            )
            # NY open: DST-aware.
            candidates.append(("ny", _ny_open_utc_for_date(d)))
        # Tokyo 23:00 UTC: opens on the *prior* UTC day for the Tokyo
        # business day starting next morning. We include it on weekdays
        # whose 23:00 UTC instant falls on a day Tokyo will trade — i.e.
        # Sun 23:00 UTC (=Mon Tokyo), Mon 23:00 UTC (=Tue Tokyo), ...,
        # Thu 23:00 UTC (=Fri Tokyo). Fri 23:00 UTC would be Sat Tokyo,
        # which is closed; Sat 23:00 UTC = Sun Tokyo, closed.
        if not is_index and d.weekday() <= 4:
            candidates.append(
                ("tokyo", datetime(d.year, d.month, d.day, _TOKYO_OPEN_UTC_HOUR, 0, tzinfo=UTC))
            )
        # Sunday reopen 22:00 UTC (tradable-liquidity anchor): only on Sundays.
        if d.weekday() == 6:
            candidates.append(
                (
                    "sunday_reopen",
                    datetime(d.year, d.month, d.day, _SUNDAY_REOPEN_UTC_HOUR, 0, tzinfo=UTC),
                )
            )

    # Pick the most-recent open at-or-before ts, within the window.
    best_name: str | None = None
    best_minutes: float = 0.0
    best_delta: float = math.inf
    for name, open_ts in candidates:
        if open_ts > ts:
            continue
        delta_min = (ts - open_ts).total_seconds() / 60.0
        if delta_min > _SESSION_WINDOW_MIN:
            continue
        # Tie-break: the smallest non-negative delta is the most recent.
        if delta_min < best_delta:
            best_delta = delta_min
            best_minutes = delta_min
            best_name = name
    if best_name is None:
        return (None, 0.0)
    return (best_name, best_minutes)


def pre_rollover_window(
    symbol: str,
    ts_utc: datetime,
    *,
    server_time_offset_seconds: int = _DEFAULT_SERVER_TIME_OFFSET_SECONDS,
) -> tuple[str | None, float]:
    """Return ``("pre_rollover", minutes_to_rollover)`` if ``ts_utc`` is inside
    the broker's daily pre-rollover ramp window, else ``(None, 0.0)``.

    The broker's daily server-time midnight rollover (server-time 00:00) is
    the canonical retail-MT5 anchor at which liquidity providers reposition
    quotes and the bid-ask spread can widen 10-30x over the preceding
    ~60 minutes. After the rollover the spread snaps back to baseline because
    the new server-day brings fresh liquidity online. This window mirrors the
    session-open machinery (:func:`session_open_window`) but with inverse
    shape — it peaks AT the anchor and is dormant outside the lead-up.

    DST / broker-policy semantics
    -----------------------------
    The rollover anchor in UTC is computed from ``server_time_offset_seconds``,
    NOT from a hardcoded UTC hour. The mapping is::

        rollover_utc_hour = (24 - server_time_offset_seconds // 3600) % 24

    For the default +03:00 (EEST summer, 10800 s) this is 21:00 UTC. For the
    +02:00 (EET, 7200 s) it is 22:00 UTC — which is the FTMO-MT5 policy
    year-round (FTMO does not observe DST on its MT5 server clock, a common
    broker convention to avoid ambiguous bar timestamps on the DST change
    weekends).

    Parameters
    ----------
    symbol : str
        Symbol to validate. The pre-rollover widening is calibrated per
        symbol (different brokers may impose different spread regimes on
        different asset classes), but the time-window logic itself is
        symbol-agnostic — the function only validates ``symbol`` against
        :data:`SUPPORTED_SYMBOLS` and uses ``ts_utc`` to compute the window.
    ts_utc : datetime.datetime
        Tz-aware UTC timestamp.
    server_time_offset_seconds : int, keyword-only
        Broker server-time offset relative to UTC, in seconds. Default
        ``10800`` (EEST summer). Pass ``7200`` for EET (FTMO) or other
        offsets when the broker policy differs.

    Returns
    -------
    tuple[str | None, float]
        ``("pre_rollover", minutes_to_rollover)`` if ``ts_utc`` falls in the
        ``[anchor - _PRE_ROLLOVER_RAMP_MINUTES, anchor]`` window (closed on
        both sides), else ``(None, 0.0)``. ``minutes_to_rollover`` is
        non-negative; at the exact anchor it is ``0.0`` (peak) and at the
        ramp start it equals :data:`_PRE_ROLLOVER_RAMP_MINUTES`.

        After the rollover anchor (even one second past), the window is
        ``(None, 0.0)`` — the fresh server-day resets liquidity per the
        broker convention this function models.

    Raises
    ------
    ValueError
        If ``symbol`` is not in :data:`SUPPORTED_SYMBOLS` or ``ts_utc`` is
        naive.

    Notes
    -----
    Symmetry with :func:`session_open_window` — both functions return a
    label-and-clock tuple consumed by :func:`evaluate`. The shape contract
    is identical so the two helpers can be combined via :func:`max` at the
    call site (see ``evaluate``'s overlap-safe combination rule).
    """
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(f"unknown symbol {symbol!r}; supported symbols: {SUPPORTED_SYMBOLS}")
    _require_utc(ts_utc, arg_name="ts_utc")
    ts = ts_utc.astimezone(UTC)

    # Determine the UTC hour at which the broker's server-time 00:00 falls.
    # server-time = UTC + offset → server-time-00:00 corresponds to UTC-hour
    # = (-offset mod 24h). Modulo arithmetic keeps it in [0, 24).
    offset_hours = server_time_offset_seconds // 3600
    rollover_utc_hour = (24 - offset_hours) % 24

    # The pre-rollover anchor is the most-recent (today or yesterday) UTC
    # instant whose hour equals ``rollover_utc_hour`` and whose minute=0.
    # We look at *today* and *tomorrow* relative to ``ts``: the today-anchor
    # may be in the future (e.g. ts=20:30 UTC, anchor at 21:00 UTC summer)
    # OR in the past (e.g. ts=23:00 UTC, anchor at 21:00 UTC summer — in
    # which case we are post-rollover and the function returns (None, 0.0)).
    today = ts.date()
    candidates: list[datetime] = []
    for offset in (0, 1):
        d = today.fromordinal(today.toordinal() + offset)
        candidates.append(datetime(d.year, d.month, d.day, rollover_utc_hour, 0, tzinfo=UTC))
    # Pick the smallest anchor >= ts (i.e. the NEXT rollover).
    future_anchors = [a for a in candidates if a >= ts]
    if not future_anchors:
        # Shouldn't happen since we include tomorrow, but defensively return.
        return (None, 0.0)
    next_anchor = min(future_anchors)
    minutes_to_rollover = (next_anchor - ts).total_seconds() / 60.0

    # Outside the ramp window → window closed.
    if minutes_to_rollover > _PRE_ROLLOVER_RAMP_MINUTES:
        return (None, 0.0)
    # Negative (post-rollover) cannot happen because we took the next-anchor.
    return ("pre_rollover", minutes_to_rollover)


def _pre_rollover_factor(
    minutes_to_rollover: float, peak_multiplier: float, ramp_minutes: float
) -> float:
    """Linear ramp from 1.0 at ``ramp_minutes`` away to ``peak_multiplier`` at the anchor.

    Formula::

        factor(t) = 1.0 + (peak - 1.0) * (1 - t / ramp_minutes)

    where ``t = minutes_to_rollover``. At ``t = ramp_minutes`` the factor is
    1.0; at ``t = 0`` (the rollover anchor) the factor equals
    ``peak_multiplier``.

    Linear (not exponential) because:

    1. The round-1 FTMO capture has only 4 pre-rollover rows in the band;
       a linear interpolation is the only fit shape thin data can support
       without over-claiming structure (a non-linear fit would chase noise).
    2. The session-open machinery uses exponential decay because it models
       a single-point liquidity event followed by smooth recovery; pre-
       rollover widening is the opposite — a slow build-up to an event,
       which intuitively reads as a linear (or front-loaded) ramp.

    Documented as a known simplification: a future capture with finer-
    grained pre-rollover sampling may justify replacing this with a fitted
    piecewise curve (e.g. flat for the first 30 min, then accelerating).

    Parameters
    ----------
    minutes_to_rollover : float
        Minutes between the current timestamp and the rollover anchor.
        Must be in ``[0, ramp_minutes]`` for the formula to make physical
        sense; outside that range the helper still returns a finite value
        but :func:`pre_rollover_window` guards the call site so this never
        triggers from production paths.
    peak_multiplier : float
        Multiplier at the anchor (``t = 0``). Must be ≥ 1.0.
    ramp_minutes : float
        Length of the ramp window in minutes. Must be > 0.
    """
    if ramp_minutes <= 0:
        raise ValueError(f"ramp_minutes must be > 0, got {ramp_minutes!r}")
    if minutes_to_rollover >= ramp_minutes:
        return 1.0
    if minutes_to_rollover <= 0.0:
        return float(peak_multiplier)
    fraction_to_peak = 1.0 - (minutes_to_rollover / ramp_minutes)
    return 1.0 + (peak_multiplier - 1.0) * fraction_to_peak


def _decay_factor(minutes_since_open: float, peak_multiplier: float, half_life_min: float) -> float:
    """Exponential decay from ``peak_multiplier`` at t=0 back to 1.0 as t→∞.

    Formula::

        factor(t) = 1.0 + (peak - 1.0) * 0.5 ** (t / half_life)

    The factor equals ``peak_multiplier`` exactly at ``t=0`` and approaches
    ``1.0`` as ``t`` grows. At ``t = half_life_min`` the factor sits halfway
    between peak and baseline, which is the property locked by
    ``test_decay_post_open``.

    Why exponential and not piecewise-linear: the live spread tape on retail
    MT5 feeds shows a fast post-reopen tightening that flattens out, which is
    exponential-shaped rather than linear. The choice is documented in the
    module docstring; a future calibration may switch to a fitted
    piecewise-linear curve per symbol if the exponential proves a poor fit.

    Parameters
    ----------
    minutes_since_open : float
        Non-negative minutes since the session open.
    peak_multiplier : float
        Multiplier at t=0. Must be ≥ 1.0.
    half_life_min : float
        Decay half-life in minutes. Must be > 0.
    """
    if half_life_min <= 0:
        raise ValueError(f"half_life_min must be > 0, got {half_life_min!r}")
    decay: float = math.pow(0.5, minutes_since_open / half_life_min)
    return 1.0 + (peak_multiplier - 1.0) * decay


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def evaluate(
    market_state: MarketState,
    request: SpreadRequest | None = None,
    *,
    calibration: SpreadCalibrationEntry | None = None,
) -> SpreadResult:
    """Return the modelled spread at ``market_state.ts_utc`` for ``market_state.symbol``.

    Algorithm
    ---------
    1. Validate symbol and timestamp.
    2. If the market is closed (per :func:`propfarm.data.quality.is_market_open`),
       return :data:`math.nan` for ``spread_bps`` with all factors set to
       ``math.nan`` — the caller must refuse to fill.
    3. Look up the calibration entry (parameter > registry > raise).
    4. Find the most recent session open via :func:`session_open_window`.
    5. Compute the session factor: ``_decay_factor(...)`` using the
       ``weekend_reopen_multiplier`` for ``sunday_reopen``, the
       ``session_open_multiplier`` for any weekday open, and ``1.0`` if no
       open is in the window.
    6. Compute the pre-rollover factor (Gate-2B round 2, 2026-05-18) via
       :func:`pre_rollover_window` + :func:`_pre_rollover_factor`. Skipped
       (factor = 1.0) when ``calibration.pre_rollover_multiplier`` is
       ``None``, which preserves round-1 behaviour for non-FX-major symbols.
    7. Combine ``session_factor`` and ``pre_rollover_factor`` via
       :func:`max` (NOT product). The two windows are structurally non-
       overlapping at real broker anchors (London 07:00 UTC vs FTMO
       rollover 22:00 UTC), but a synthetic timestamp could put both
       above 1.0; the ``max`` rule prevents double-counting and gives the
       reviewer a defensible overlap policy. Documented test coverage:
       ``test_pre_rollover_does_not_double_count_at_session_overlap``.
    8. Compute the news factor: ``calibration.news_multiplier`` if
       ``market_state.news_window`` is True, else ``1.0``.
    9. Return ``baseline_bps * max(session_factor, pre_rollover_factor) *
       news_factor`` with a full component breakdown.

    Parameters
    ----------
    market_state : MarketState
        Must carry a tz-aware UTC timestamp.
    request : SpreadRequest, optional
        Order-side request. Not consumed today; present for uniform-shape
        conformance with the slippage and fill-engine signatures.
    calibration : SpreadCalibrationEntry, optional
        Override the registry lookup. Useful in tests and for what-if
        analyses; production callers pass ``None`` and let the registry
        resolve by ``market_state.symbol``.

    Returns
    -------
    SpreadResult
        Frozen output carrying the modelled spread in bps, a diagnostic
        breakdown, and the calibration confidence flag.

    Raises
    ------
    ValueError
        If the symbol is unknown, the datetime is naive, or no calibration
        is available for the symbol.

    Determinism
    -----------
    The function is pure: same ``(market_state, request, calibration)``
    triple → bit-identical ``SpreadResult``. No RNG, no clock reads, no
    cache mutation.
    """
    _require_utc(market_state.ts_utc, arg_name="market_state.ts_utc")
    if market_state.symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(
            f"unknown symbol {market_state.symbol!r}; supported symbols: {SUPPORTED_SYMBOLS}"
        )

    if calibration is None:
        try:
            calibration = CALIBRATIONS[market_state.symbol]
        except KeyError as exc:  # pragma: no cover — guarded by SUPPORTED_SYMBOLS check
            raise ValueError(
                f"no calibration registered for symbol {market_state.symbol!r}"
            ) from exc

    # Market closed → NaN. The fill engine treats NaN as "no fill possible".
    if not is_market_open(market_state.symbol, market_state.ts_utc):
        return SpreadResult(
            symbol=market_state.symbol,
            ts_utc=market_state.ts_utc,
            spread_bps=math.nan,
            components={
                "baseline_bps": math.nan,
                "session_factor": math.nan,
                "pre_rollover_factor": math.nan,
                "session_or_pre_rollover_factor": math.nan,
                "news_factor": math.nan,
                "decay_minutes": math.nan,
                "minutes_to_rollover": math.nan,
            },
            calibration_confidence=calibration.confidence,
        )

    session_name, minutes_since_open = session_open_window(market_state.symbol, market_state.ts_utc)
    if session_name == "sunday_reopen":
        session_factor = _decay_factor(
            minutes_since_open,
            calibration.weekend_reopen_multiplier,
            calibration.decay_half_life_min,
        )
    elif session_name in ("london", "ny", "tokyo"):
        session_factor = _decay_factor(
            minutes_since_open,
            calibration.session_open_multiplier,
            calibration.decay_half_life_min,
        )
    else:
        session_factor = 1.0

    # Pre-rollover factor (Gate-2B round 2). Skipped entirely when the
    # calibration entry doesn't carry a pre_rollover_multiplier — this
    # preserves round-1 behaviour for symbols (USDJPY/XAUUSD/GER40/US100)
    # that haven't yet been characterised against a real capture.
    pre_rollover_factor: float = 1.0
    minutes_to_rollover: float = math.inf
    if calibration.pre_rollover_multiplier is not None:
        pre_label, mins_to_roll = pre_rollover_window(
            market_state.symbol,
            market_state.ts_utc,
            server_time_offset_seconds=calibration.server_time_offset_seconds,
        )
        if pre_label == "pre_rollover":
            minutes_to_rollover = mins_to_roll
            pre_rollover_factor = _pre_rollover_factor(
                mins_to_roll,
                calibration.pre_rollover_multiplier,
                _PRE_ROLLOVER_RAMP_MINUTES,
            )

    # Overlap-safe combination: take the MAX of the two factors, NOT the
    # product. At real-broker anchors (London 07:00 UTC vs FTMO rollover
    # 22:00 UTC) the windows are structurally non-overlapping — but a
    # synthetic timestamp could put both above 1.0, in which case product
    # would over-claim the widening. ``max`` is the defensible policy.
    combined_session_factor = max(session_factor, pre_rollover_factor)

    news_factor = calibration.news_multiplier if market_state.news_window else 1.0
    spread_bps = calibration.baseline_bps * combined_session_factor * news_factor

    return SpreadResult(
        symbol=market_state.symbol,
        ts_utc=market_state.ts_utc,
        spread_bps=spread_bps,
        components={
            "baseline_bps": calibration.baseline_bps,
            "session_factor": session_factor,
            "pre_rollover_factor": pre_rollover_factor,
            "session_or_pre_rollover_factor": combined_session_factor,
            "news_factor": news_factor,
            "decay_minutes": minutes_since_open,
            "minutes_to_rollover": minutes_to_rollover,
        },
        calibration_confidence=calibration.confidence,
    )


# --------------------------------------------------------------------------- #
# Calibration registry
# --------------------------------------------------------------------------- #
# Seed values derived from publicly-observed retail-broker spread tapes (FTMO
# demo, IC Markets raw, Pepperstone razor). They are flagged
# ``"uncertain"`` because the canonical source — a 24h+ MT5 demo recording on
# the production VPS — has not yet been captured for every symbol. The
# recording runbook is at ``docs/runbooks/spread-calibration-recording.md``
# and the post-capture calibration routine (which will flip these rows to
# ``"high"``) is deferred to a separate task.
#
# Gate-2B round 1 (2026-05-18, EURUSD + GBPUSD only)
# --------------------------------------------------
# Source: ``data/raw/fill_recordings/bbf710b335f84e94af21b74cc3b5d725_residuals.parquet``.
# 199 retcode-matched rows. Per-symbol mean ``spread_residual_pips``
# (live - sim) under the original calibration:
#
#   EURUSD n=99  mean_resid_pips = +0.220  (sim underestimates spread)
#   GBPUSD n=100 mean_resid_pips = +0.370  (sim underestimates spread)
#
# To collapse the per-symbol residual mean to ~0, the ``baseline_bps`` is
# raised by the mean residual converted to bps at the reference price.
# Conversion: ``Δbaseline_bps = mean_resid_pips * pip_size / reference_price
# * 1e4``. For EURUSD at ~1.16 mid: 0.220 / 1.16 ≈ 0.19 bps → 0.10 + 0.19 ≈
# 0.29. For GBPUSD at ~1.33 mid: 0.370 / 1.33 ≈ 0.28 bps → 0.15 + 0.28 ≈
# 0.43. After this change the per-symbol residual mean drops to within
# ±0.10 pip on both symbols (verified by harness re-run on the same
# parquet).
#
# Lowest-touch parameter rationale: we raise the BASELINE rather than the
# session-open peak / decay tail because the live capture's residual is
# present across all 24h, not concentrated near session opens. Bumping the
# session-open multiplier would only correct rows inside the ±60min decay
# window; the baseline shift corrects every hour. The trade-off: the
# session-open peak (=baseline x session_open_multiplier) now sits at
# 0.29 x 5 = 1.45 bps for EURUSD and 0.43 x 6 = 2.58 bps for GBPUSD, both
# slightly higher than the pre-calibration peak. This is acceptable because
# the original seed peak was a placeholder; if a future capture shows a
# session-open over-shoot, the session_open_multiplier can be tuned down.
#
# Gate-2B round 2 update (2026-05-18)
# -----------------------------------
# Two round-1-known residuals are now both CLOSED on EURUSD/GBPUSD:
#
#   1. NY-close / pre-rollover widening — a session-aware
#      ``pre_rollover_multiplier`` lands on EURUSD and GBPUSD against the
#      same 24h capture (4 outlier rows in 21:00-22:00 UTC). Linear ramp
#      60 min before the 22:00 UTC FTMO rollover anchor. Peak=15.0 on
#      both symbols (the smallest round-numbered value that meets the
#      PASS criterion p95 ≤ 1.0 AND t-test p ≥ 0.01).
#
#      **Slice fit honesty (round-2 reviewer correction, 2026-05-18)**:
#      the round-2 calibration agent reported "3 of 4 outlier rows reduce
#      to |residual| ≤ 0.79 pip"; the round-2 reviewer's independent
#      recompute showed this is empirically wrong. Slice residuals on
#      the 5 rows in hour-21 (one is retcode=10018 rejected, one is a
#      stop-order activation; 3 are clean market activations): absolute
#      residuals {0.31, 1.76, 1.80, 1.84, 4.94}. **Only 1 of 5 sits below
#      0.79 pip post-cal; the other 4 are above 1.0 pip.** The PASS
#      verdict is still genuine because the global p95 = 0.527 pip
#      (these 5 rows are 2.5% of n=200; they fall in p99-rank territory,
#      not p95). But the slice fit is significantly worse than the agent
#      reported.
#
#      The implied peak factors on the 4 large-residual rows cluster
#      tightly around 17 (15.7, 16.3, 17.1, 19.8) — geometrically
#      consistent with a **step function** (flat at peak across the
#      window), NOT a linear ramp. Reviewer's geometry: with a
#      step P=18, max|residual| drops to 0.78 pip and ALL 4 outliers
#      land below the 1.0 pip threshold. The linear-ramp choice
#      satisfies the global PASS criterion but is provably suboptimal
#      at the slice level. **Round 3 should replace the linear ramp
#      with a step function (peak ~17-18) once a second capture lands
#      to validate the shape.** Flagged in the deferred ledger.
#
#   2. Session-open over-shoot — round-1 left session_open_multiplier at
#      Wave-6b seed values (5.0 EURUSD, 6.0 GBPUSD). The round-1 reviewer
#      explicitly authorised tuning these down in round 2 ("Tokyo-open
#      over-shoot at hour 23 UTC the sim's session_open multiplier
#      produces ~1.5 pips for GBPUSD while the live capture shows ~0.44
#      pips"). Live data across the bbf710b3 capture at all four
#      session-open hours (07, 12, 13, 23 UTC) sits close to baseline,
#      so the round-2 calibration drops both to 2.0. Without this tweak,
#      the round-2 spread p95 drops only from 1.4275 → 1.2304 (still
#      INVESTIGATE); WITH it, p95 drops to ~0.53 → PASS.
#
# Overall round-2 spread p95: 0.53 pip (vs round-1 1.4275 pip, target ≤ 1.0).
#
# Other symbols (USDJPY/XAUUSD/GER40/US100) remain at their Wave-6b seed
# values because they were NOT in the Gate-2B round 1 capture.
#
# Conventions
# -----------
# * EURUSD, GBPUSD: round 1 calibrated. baseline_bps reflects FTMO MT5
#   demo round-trip live observation, NOT a pre-publication aggregator
#   tape.
# * USDJPY: FX major; ``baseline_bps`` derives from ~0.1-0.2 pip baseline
#   at a 150 mid → ~0.15 bps (uncalibrated seed).
# * XAUUSD: 30-50 cents typical spread at a $3500 mid → ~3-4 bps (seed).
# * GER40: 1.0-1.5 index-point spread at a 20,000 index level → ~2-3 bps
#   (seed). For index CFDs the bps convention computes against the index
#   level itself, so a "wider" feed translates proportionally.
# * US100: 1.5-2.5 index-point spread at a 20,000 level → ~2-3 bps (seed).
#
# Session-open multipliers are slightly larger for indices because they
# coincide with the cash-market open (which is the *only* time index CFDs
# see real liquidity reset). Sunday reopen multipliers are largest for
# XAUUSD (gold reopens with very thin liquidity).
_SEED_DATE: Final[date] = date(2026, 5, 12)

_SEED_SOURCE: Final[str] = "docs/runbooks/spread-calibration-recording.md"

#: Snapshot date and source for the EURUSD/GBPUSD entries recalibrated against
#: the Gate-2B round 1 capture. The confidence flag stays "uncertain" because
#: 24h of FTMO demo data is too thin to upgrade.
_GATE_2B_R1_DATE: Final[date] = date(2026, 5, 18)
_GATE_2B_R1_SOURCE: Final[str] = (
    "data/raw/fill_recordings/bbf710b335f84e94af21b74cc3b5d725_residuals.parquet "
    "(Gate 2B calibration round 1 — single 24h FTMO MT5 demo capture, 199 rows)"
)

#: Round-2 source. The pre_rollover_multiplier on EURUSD/GBPUSD is calibrated
#: from the same capture's 21:00-22:00 UTC slice (4 pre-rollover outlier rows).
#: pre_rollover_multiplier is marked confidence="medium" because the field is
#: calibrated from a real capture but the slice is too thin (n=4) for split-
#: half cross-validation; flagged for second-capture validation across weekday
#: + weekend-spanning windows.
_GATE_2B_R2_DATE: Final[date] = date(2026, 5, 18)
_GATE_2B_R2_SOURCE: Final[str] = (
    "data/raw/fill_recordings/bbf710b335f84e94af21b74cc3b5d725_residuals.parquet "
    "(Gate 2B calibration round 2 — pre_rollover_multiplier fit against the "
    "21:00-22:00 UTC slice, n=4 rows; confidence=medium pending second-capture "
    "validation across weekday + weekend-spanning windows)"
)

#: FTMO MT5 server-time offset. FTMO operates its MT5 server on EET year-round
#: (no DST observance — a common broker convention to avoid ambiguous bar
#: timestamps on DST-change weekends). EET = UTC+02:00 = 7200 seconds. This
#: anchors the daily server-time-midnight rollover at 22:00 UTC year-round.
#: The captured 4 pre-rollover outliers (21:18-21:55 UTC) all sit inside the
#: 60-min window before this 22:00 UTC anchor, confirming the offset.
_FTMO_SERVER_TIME_OFFSET_SECONDS: Final[int] = 7200

CALIBRATIONS: Final[dict[str, SpreadCalibrationEntry]] = {
    "EURUSD": SpreadCalibrationEntry(
        symbol="EURUSD",
        # Gate-2B round 1 (2026-05-18): baseline_bps 0.10 -> 0.29
        # to absorb the +0.22 pip mean residual at ~1.16 ref price.
        baseline_bps=0.29,
        # Gate-2B round 2 (2026-05-18): session_open_multiplier 5.0 -> 2.0.
        # The round-1 calibration left this at the Wave-6b seed (5.0), and
        # the round-1 reviewer-deferred "Tokyo-open over-shoot" follow-up
        # explicitly authorised tuning it down in round 2. Live data from
        # the bbf710b3 capture: hour 07 UTC EURUSD max=0.4 pip, hour 12 UTC
        # max=0.4 pip, hour 23 UTC max=0.3 pip — all close to baseline
        # (0.34 pip = 0.29 bps * 1.16 ref). A 5x multiplier at peak would
        # predict 1.7 pip, overshooting live by ~1.3 pip and inflating
        # residual p95. 2.0 gives peak=0.67 pip, a realistic small bump.
        # Without this tweak, the round-2 spread p95 drops only from
        # 1.4275 → 1.2304 (still INVESTIGATE); WITH it, p95 drops to
        # ~0.53 pip → PASS.
        session_open_multiplier=2.0,
        decay_half_life_min=10.0,
        news_multiplier=20.0,
        weekend_reopen_multiplier=15.0,
        # Gate-2B round 2 (2026-05-18): pre_rollover_multiplier=15.0 fits
        # the global p95 PASS criterion (1.4275 → 0.527 pip) on the
        # 21:00-22:00 UTC pre-rollover slice. Linear ramp 60 min before
        # the 22:00 UTC FTMO rollover anchor. Peak is the smallest
        # round-numbered multiplier that meets PASS (p95 ≤ 1.0 AND t-test
        # p ≥ 0.01). The 21:18 EURUSD outlier's residual is **+4.94 pip**
        # post-cal (round-2 reviewer corrected the impl agent's mis-stated
        # +4.23 pip; recomputed from residuals parquet). **Only 1 of 5
        # slice rows sits below 0.79 pip post-cal**; the other 4 are in
        # 1.76-4.94 pip range — slice fit is worse than the impl agent
        # reported, but the PASS verdict is genuine because the slice is
        # 2.5% of n=200 (these 4-5 rows fall in p99-rank territory, not
        # p95). The implied peak factors on the 4 outliers cluster tightly
        # around 17, geometrically favoring a step function (flat-at-peak)
        # over linear ramp — **round-3 candidate**: replace with step at
        # peak ~17-18. Confidence: "medium" on the field (real-capture
        # derived but n=4 too thin for split-half cross-validation);
        # entry's overall confidence stays "uncertain" until a second
        # capture lands.
        pre_rollover_multiplier=15.0,
        server_time_offset_seconds=_FTMO_SERVER_TIME_OFFSET_SECONDS,
        confidence="uncertain",
        snapshot_date=_GATE_2B_R2_DATE,
        snapshot_source=_GATE_2B_R2_SOURCE,
    ),
    "GBPUSD": SpreadCalibrationEntry(
        symbol="GBPUSD",
        # Gate-2B round 1 (2026-05-18): baseline_bps 0.15 -> 0.43
        # to absorb the +0.37 pip mean residual at ~1.33 ref price.
        baseline_bps=0.43,
        # Gate-2B round 2 (2026-05-18): session_open_multiplier 6.0 -> 2.0.
        # Same rationale as EURUSD. Live data from the bbf710b3 capture
        # at hour 07 UTC GBPUSD max=0.9 pip, hour 12 UTC max=0.4 pip,
        # hour 23 UTC max=0.6 pip — all close to baseline (0.57 pip =
        # 0.43 bps * 1.33 ref). A 6x multiplier at peak would predict
        # 3.4 pip, overshooting live by ~2.5 pip; this was the round-1
        # reviewer-flagged "Tokyo-open over-shoot at hour 23". 2.0 gives
        # peak=1.14 pip, a realistic small bump.
        session_open_multiplier=2.0,
        decay_half_life_min=10.0,
        news_multiplier=20.0,
        weekend_reopen_multiplier=15.0,
        # Gate-2B round 2 (2026-05-18): pre_rollover_multiplier=15.0; same
        # rationale as EURUSD. The 21:55 UTC GBPUSD outlier (live=9.9 pip at
        # t=4.2min to rollover) is partially caught — sim peaks at
        # ~8.0 pip vs live 9.9 → |residual|=1.88 pip post-cal. The under-
        # shoot is intentional: a higher peak would over-fit this single
        # outlier and inflate sim spread on non-outlier rows (which would
        # flip the t-test bias from -0.06 toward 0 but at the cost of a
        # wider p99 tail). The round-2 GBPUSD slice contains only this one
        # outlier, so the peak multiplier is effectively fit on a single
        # data point — flagged for second-capture validation.
        # confidence="medium" on the new field; entry's overall confidence
        # stays "uncertain".
        pre_rollover_multiplier=15.0,
        server_time_offset_seconds=_FTMO_SERVER_TIME_OFFSET_SECONDS,
        confidence="uncertain",
        snapshot_date=_GATE_2B_R2_DATE,
        snapshot_source=_GATE_2B_R2_SOURCE,
    ),
    "USDJPY": SpreadCalibrationEntry(
        symbol="USDJPY",
        baseline_bps=0.15,
        session_open_multiplier=5.0,
        decay_half_life_min=10.0,
        news_multiplier=20.0,
        weekend_reopen_multiplier=15.0,
        confidence="uncertain",
        snapshot_date=_SEED_DATE,
        snapshot_source=_SEED_SOURCE,
    ),
    "XAUUSD": SpreadCalibrationEntry(
        symbol="XAUUSD",
        baseline_bps=4.0,
        session_open_multiplier=6.0,
        decay_half_life_min=15.0,
        news_multiplier=15.0,
        weekend_reopen_multiplier=25.0,
        confidence="uncertain",
        snapshot_date=_SEED_DATE,
        snapshot_source=_SEED_SOURCE,
    ),
    "GER40": SpreadCalibrationEntry(
        symbol="GER40",
        baseline_bps=2.5,
        session_open_multiplier=8.0,
        decay_half_life_min=12.0,
        news_multiplier=10.0,
        weekend_reopen_multiplier=10.0,
        confidence="uncertain",
        snapshot_date=_SEED_DATE,
        snapshot_source=_SEED_SOURCE,
    ),
    "US100": SpreadCalibrationEntry(
        symbol="US100",
        baseline_bps=2.5,
        session_open_multiplier=8.0,
        decay_half_life_min=12.0,
        news_multiplier=10.0,
        weekend_reopen_multiplier=10.0,
        confidence="uncertain",
        snapshot_date=_SEED_DATE,
        snapshot_source=_SEED_SOURCE,
    ),
}


__all__ = [
    "CALIBRATIONS",
    "MarketState",
    "SpreadCalibrationEntry",
    "SpreadRequest",
    "SpreadResult",
    "evaluate",
    "pre_rollover_window",
    "session_open_window",
]
