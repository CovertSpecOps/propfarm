"""Record live FTMO MT5 demo fills to parquet for Gate 2B (Phase 0).

Gate 2B compares the simulator's predicted fill prices against real broker
fills. Rather than holding a live MT5 session open during the gate, we
pre-record a corpus of 100+ fills here so the comparison runs purely on
parquet later. This decouples gate execution from broker connectivity and
lets the user collect samples across 24-48h of session diversity.

The recorder runs on the same Windows VPS the Day-1 spike used. It:

* Reads FTMO demo credentials from ``~/.propfarm-secrets.json``.
* Asserts the connected server name starts with ``FTMO-Demo`` before
  *any* order is placed — a hardcoded safety belt against
  the script being pointed at a funded or non-demo account.
* Builds a deterministic sampling schedule covering London / NY / Tokyo
  session opens and mid-session quiet zones, mixing market / limit / stop
  orders across the configured symbols.
* For each sample: snapshots the bid/ask, sends the order, immediately
  closes the resulting position (if any), and writes a single row to a
  parquet file under ``data/raw/fill_recordings/{run_id}.parquet``.
* Writes a small JSON manifest alongside the parquet on exit, summarizing
  attempted vs. filled vs. rejected counts.

Safety guardrails (encoded as hard asserts; bail on violation):

* 0.01 lot only.
* Never more than 5 simultaneous open positions; if reached, the script
  forces a sweep-close of everything and aborts the session.
* Server name must match ``FTMO-Demo*`` (case-sensitive).
* 48h wall-clock hard cap regardless of ``--duration-hours``.

The ``MetaTrader5`` Python pkg is imported *inside* ``main()`` (not at
module top level) so this module loads on macOS/Linux for unit testing of
the pure helpers. The same pattern as ``scripts/spike_mt5.py``.

The pure helpers are:

* :func:`build_default_schedule` — deterministic schedule construction.
* :func:`build_order_request` — translate (order_type, side, tick) into
  the ``mt5.order_send`` request dict.
* :func:`parse_fill_into_record` — translate raw MT5 result + timing
  metadata into the canonical FillRecord row.

See ``docs/runbooks/gate-2b-fill-recording.md`` for the operator runbook.

OrderSendResult vs. deal record (MT5 vendor convention — CRITICAL)
------------------------------------------------------------------

For **market orders** in MT5, ``mt5.order_send`` returns an
``OrderSendResult`` whose ``.price`` field is **0.0 in most cases** —
not the executed fill price. The actual fill price lives in the
subsequent deal record, retrieved via ``mt5.history_deals_get(...)``
after ``order_send`` returns. The deal record also carries the
authoritative broker-side fill time as ``.time`` (epoch seconds).

Wave-6b's original implementation read ``result.price`` directly,
which produced a 110-row capture
(``data/raw/fill_recordings/24e00278d0024a98beb009b75762adb6.parquet``,
2026-05-13 18:00 UTC -> 2026-05-14 09:06 UTC) where every
``retcode == 10009`` row had ``fill_price = 0.0`` and absurd
``slippage_observed_pips`` like +-11,700 to +-13,500 (= ``(0 -
requested_price) / pip``). The capture is preserved with a
``UNUSABLE.md`` sidecar and ``status: fill_price-unusable`` in its
manifest; it is salvageable for ``retcode``, ``requested_price``,
``spread_at_request_pips``, and ``broker_latency_ms`` calibration but
must NOT be fed to Gate 2B.

The fix uses **Option B** dependency injection (see Gate 2B fix-up
dispatch brief, 2026-05-14): ``parse_fill_into_record`` accepts new
keyword-only parameters ``actual_fill_price`` and
``actual_fill_time_utc`` for the deal-resolved values. ``main()`` does
the deal lookup against the real ``mt5.history_deals_get`` and passes
the resolved scalars. The pure helper stays purely a data-shaping
function, mock-friendly without ``mt5`` import. Option A (callable
injection) was considered but B is cleaner because the helper never
needs to know about MT5 ticket types.

Per-field broker-response audit (2026-05-14)
--------------------------------------------

* ``retcode`` — from ``result.retcode`` directly. Documented MT5
  field; reliable against real broker. No change.
* ``comment`` — from ``result.comment`` directly. Reliable. May carry
  the ``"retcode_or_deal_failure: …"`` annotation in the soft-failure
  path described below.
* ``requested_price`` — from ``open_req["price"]`` (internal). Never
  reads ``OrderSendResult``. Reliable.
* ``volume_lots`` — from ``open_req["volume"]`` (internal). Reliable.
* ``spread_at_request_pips`` — from ``tick.bid`` / ``tick.ask``
  captured BEFORE the send. Independent of ``OrderSendResult``.
  Reliable.
* ``broker_latency_ms`` — from Python-side wallclock
  (``after_send_utc - request_time_utc``). This is round-trip-time
  (RTT) including bridge + broker, **not** broker-internal latency.
  Documented for downstream consumers.
* ``fill_price`` — for retcode=10009 market orders, **MUST come from
  the deal record**, not ``result.price``. This is the bug locus.
* ``broker_fill_time_utc`` — for retcode=10009 market orders, **MUST
  come from ``deal.time``** (broker authoritative). For non-success
  retcodes and the soft-failure case (deal lookup returned None /
  empty), falls back to ``after_send_utc`` (Python wallclock) with a
  documented caveat.
* ``slippage_observed_pips`` — derived from ``fill_price`` and
  ``requested_price`` with the adverse-positive convention. Inherits
  the fill_price fix automatically.

For **limit/stop pending orders**, ``main()`` cancels the pending
order immediately after ``order_send`` (see ``_cancel_pending_order``).
A pending order with no fill produces no deal record, so the deal
lookup returns ``None`` and the helper sets ``fill_price = NaN`` /
``slippage_observed_pips = NaN`` via the soft-failure path. This is
correct: a cancelled pending order has no fill price to record. The
fix does not regress the limit/stop branch.

Soft-failure: when the deal lookup returns ``None`` or empty (transient
broker / history-get glitch on an otherwise-successful market order),
``main()`` calls ``parse_fill_into_record`` with
``actual_fill_price=None`` and ``actual_fill_time_utc=None``. The helper
records ``fill_price = NaN`` and prefixes the comment with
``"retcode_or_deal_failure: "`` so downstream analysis can distinguish
"rejected by broker" (raw broker comment) from "filled but deal lookup
failed" (annotated comment).

2026-05-14 CRITICAL fix v2 — history-cache precondition + loud market lookup failures
-------------------------------------------------------------------------------------

The fix-v1 (commit ``9dd9af6``) introduced ``_resolve_fill_from_deal``
calling ``mt5.history_deals_get(ticket=...)`` and
``mt5.history_deals_get(position=...)``. Short-test capture on the VPS
(run_id ``a68b59a65e384f4d859d3bf257253d75``, 2026-05-14 16:11 UTC,
Ctrl-C'd at idx=006 before flush so no parquet landed) revealed that
those ticket / position keyed lookups returned **empty** for every
market order — even though the MT5 History tab confirmed real deals
existed broker-side (~17 round-trip deals, real prices, real tickets,
balance change of $100,000 -> $99,990.89 matching individual costs).
Every market fill therefore landed with ``fill_price = NaN``.

Documented fix per MetaQuotes Python integration docs
(``mt5historydealsget_py``): the function has three call overloads —
``(date_from, date_to, group=...)``, ``(ticket=...)``, and
``(position=...)``. The date-range overload is documented as receiving
"all history deals within a specified period in a single call similar
to the HistoryDealsTotal and HistoryDealSelect tandem" — i.e. the
date-range form internally drives the MQL5 ``HistorySelect`` step that
populates the history cache. So fix v2 uses the **date-range overload
as the most-robust fallback** when ticket / position lookups return
empty: query a small window around ``request_time`` and filter by
symbol + volume + side + ``DEAL_ENTRY_IN``.

Additionally, fix v2 calls ``mt5.history_select(date_from, date_to)``
defensively as a precondition WHEN the function exists on the loaded
MT5 build (``hasattr(mt5, "history_select")``). The Python MetaTrader5
package does not officially document a ``history_select`` function (only
the MQL5 server-side ``HistorySelect`` is documented), but some
community-built MT5 client versions expose it; calling it via
``hasattr`` is safe on all versions and may engage the history cache on
builds where it is the documented precondition.

The three-path lookup order in ``_resolve_fill_from_deal``:

1. ``mt5.history_deals_get(ticket=result.deal)`` — fast path; works on
   most MT5 builds when ``deal`` is populated.
2. ``mt5.history_deals_get(position=result.order)`` filtered to
   ``DEAL_ENTRY_IN`` — fallback when ``deal`` is 0 but ``order`` is
   populated.
3. ``mt5.history_deals_get(date_from=request_time-1s,
   date_to=now+5s)`` filtered by symbol + volume + side +
   ``DEAL_ENTRY_IN`` — most-robust documented fallback that drives the
   history cache via the date-range overload. Picks the deal with
   ``time`` closest to ``request_time`` on multi-match (logs
   ``[record_fills:ambiguous_deal_match]`` to stderr). A session-scoped
   claim set prevents double-attribution when the script fires two
   same-symbol same-side orders within the time window.

Market-vs-pending lookup-failure distinction
--------------------------------------------

For ``order_type == "market"`` with ``retcode == 10009``, an empty
lookup after all three paths is an **error condition** — the deal MUST
exist (the broker confirmed the fill). Fix v2 emits a stderr log
``[record_fills:market_lookup_failure] idx=N symbol=S order=M side=D
request_time=T window=[F,T]`` and increments a session-scoped
``n_market_lookup_failures`` counter that is propagated into the
:class:`SessionManifest` as a top-level integer field. Gate 2B's
harness refuses to consume a manifest whose ratio of market lookup
failures to filled market rows exceeds 5%.

For ``order_type in ("limit", "stop")`` with ``retcode == 10009``, an
empty lookup is silent and expected — a pending order returns 10009 to
acknowledge the placement, not a fill, so no deal exists yet. The
helper sets ``fill_price = NaN`` (the correct behavior) without
incrementing the failure counter or emitting a log.

The :class:`SessionManifest` schema bumps to ``"1.1"`` with the new
``n_market_lookup_failures`` field. The :class:`FillRecord` (parquet
column) schema is **unchanged** — the parquet column set stays locked
at v1.0 column names; only the sidecar manifest carries the new field.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import sys
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

OrderType = Literal["market", "limit", "stop"]
Side = Literal["buy", "sell"]

#: Manifest schema version. Bumped to "1.1" on 2026-05-14 fix v2 to add
#: ``n_market_lookup_failures``. FillRecord (parquet column) schema is
#: unchanged — only the sidecar JSON manifest gains the new field.
SCHEMA_VERSION: Final[str] = "1.2"
LOT_SIZE: Final[float] = 0.01
MAX_SIMULTANEOUS_POSITIONS: Final[int] = 5
HARD_TIME_LIMIT_HOURS: Final[float] = 48.0
ALLOWED_SERVER_PREFIX: Final[str] = "FTMO-Demo"

#: Time window for the date-range deal lookup fallback. ``request_time -
#: HISTORY_LOOKUP_WINDOW_PAD_BEFORE`` to ``now + HISTORY_LOOKUP_WINDOW_PAD_AFTER``.
#: 1s before / 5s after is wide enough to absorb broker-clock skew and
#: late deal commits, narrow enough that ambiguous matches across distinct
#: orders are rare. Matches the example windows in MetaQuotes' Python
#: integration docs (``mt5historydealsget_py``) which use datetime ranges
#: of seconds around ``order_send`` calls.
HISTORY_LOOKUP_WINDOW_PAD_BEFORE: Final[timedelta] = timedelta(seconds=1)
HISTORY_LOOKUP_WINDOW_PAD_AFTER: Final[timedelta] = timedelta(seconds=5)

# Session anchor minutes-of-day in UTC. London 07:00, NY am 12:00 (DST safety
# captured in distribution; we keep a single canonical UTC anchor since FX
# trades 24/5 and the session-open spread spike is what we care about), NY pm
# 13:00, Tokyo 23:00. Mid-session quiet zones at 10:00 and 15:00 UTC.
_SESSION_OPENS_UTC_MINUTES: Final[tuple[int, ...]] = (
    7 * 60,  # London open
    12 * 60,  # NY am
    13 * 60,  # NY pm
    23 * 60,  # Tokyo open
)
_QUIET_ZONES_UTC_MINUTES: Final[tuple[int, ...]] = (
    10 * 60,  # London quiet
    15 * 60,  # NY quiet
)

# Order-type mix target. The total must sum to 1.0; tests pin the ±10%
# tolerance against these targets.
_ORDER_TYPE_TARGET_MIX: Final[dict[OrderType, float]] = {
    "market": 0.60,
    "limit": 0.25,
    "stop": 0.15,
}

# Pip-distance for limit/stop placement relative to mid. 5 pips chosen as a
# round-number compromise: tight enough that "inside spread" limits fill within
# a few minutes during liquid hours, wide enough that "outside spread" limits
# usually reject (which we *want* to record — broker reject behaviour is part
# of Gate 2B's diagnostic surface).
_PIP_DISTANCE: Final[float] = 5.0


# --------------------------------------------------------------------------- #
# Pydantic models — the on-disk schema.
# --------------------------------------------------------------------------- #
class FillRecord(BaseModel):
    """One row in the fill-recording parquet.

    Schema version 1.0. Every field in the user-mandated schema table is
    captured here; ``schema_version`` lives in the manifest, not on each row,
    because all rows in a single parquet share it.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    request_time_utc: datetime
    broker_fill_time_utc: datetime
    symbol: str
    order_type: OrderType
    side: Side
    volume_lots: float
    requested_price: float
    fill_price: float
    spread_at_request_pips: float
    slippage_observed_pips: float
    broker_latency_ms: float
    retcode: int
    comment: str


class SessionManifest(BaseModel):
    """End-of-session manifest written alongside the parquet.

    Schema versioning
    -----------------

    * **1.0** (initial release) — fields: run_id, start_utc, end_utc,
      n_attempted, n_filled, n_rejected, schema_version, vps_host_redacted.
    * **1.2** (2026-05-14 fix v2 reviewer follow-up) — added
      ``n_filled_market`` so Gate 2B's market-lookup-failure ratio uses a
      market-only denominator, not the all-fills (market + pending)
      denominator that mathematically dilutes the signal. The 1.1 ratio
      n_market_lookup_failures / n_filled was lenient by ~2x for typical
      mixes (60% market / 40% pending); the 1.2 denominator gives the
      market-only failure rate the guard's threshold actually documents.
    * **1.1** (2026-05-14 fix v2) — added ``n_market_lookup_failures``.
      Counts the number of ``order_type == "market"`` rows where
      ``retcode == 10009`` (broker confirmed fill) but the helper's
      three-path deal lookup (ticket -> position -> time-range) returned
      empty. The expected value is ``0``; a non-zero value indicates the
      capture has unreliable ``fill_price`` data for some market rows.
      Gate 2B's harness refuses to consume a manifest whose ratio
      ``n_market_lookup_failures / max(n_filled_market, 1) > 0.05``.

    Notes
    -----

    ``schema_version`` defaults to the current :data:`SCHEMA_VERSION` so
    every newly-written manifest carries the latest version. Existing
    on-disk manifests written under v1.0 (e.g. the unusable
    ``24e00278…`` capture's manifest) are still readable as plain
    JSON; Gate 2B's loader treats a missing ``n_market_lookup_failures``
    key as ``0`` (forward-compat).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    start_utc: datetime
    end_utc: datetime
    n_attempted: int
    n_filled: int
    n_filled_market: int = 0
    n_rejected: int
    n_market_lookup_failures: int = 0
    schema_version: str = SCHEMA_VERSION
    vps_host_redacted: bool = True


@dataclass(frozen=True)
class SamplingSchedule:
    """A deterministic sequence of (target_time, symbol, order_type, side).

    Each entry pins exactly one fill attempt. ``main()`` iterates the entries
    in chronological order and blocks (``time.sleep``) until the target time
    before sending the order. Side is deterministic too (alternates buy/sell)
    so the recording captures balanced direction without seasonality bias.
    """

    targets: tuple[datetime, ...]
    symbols: tuple[str, ...]
    order_types: tuple[OrderType, ...]
    sides: tuple[Side, ...]

    def __post_init__(self) -> None:
        if not (len(self.targets) == len(self.symbols) == len(self.order_types) == len(self.sides)):
            raise ValueError(
                "SamplingSchedule arrays must be equal length; "
                f"got targets={len(self.targets)}, symbols={len(self.symbols)}, "
                f"order_types={len(self.order_types)}, sides={len(self.sides)}"
            )

    def __len__(self) -> int:
        return len(self.targets)


# --------------------------------------------------------------------------- #
# Pure helpers — testable without MetaTrader5 installed.
# --------------------------------------------------------------------------- #
def _pip_size(symbol: str) -> float:
    """Return the pip size for a symbol.

    JPY pairs quote with 3 digits past the decimal (pip = 0.01). All other FX
    majors covered here quote with 5 digits past the decimal (pip = 0.0001).
    Indices / metals would need a different rule; out of scope for now.
    """
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def _allocate_order_types(n: int, rng: random.Random) -> list[OrderType]:
    """Allocate ``n`` order types according to ``_ORDER_TYPE_TARGET_MIX``.

    Uses floor counts for each bucket, then distributes the rounding remainder
    deterministically by index order so the output is a stable function of
    ``n`` (and the rng-shuffle that follows). This avoids the "stochastic mix
    drifts off target by 30% on small n" failure mode of pure-rejection
    sampling.
    """
    base = {ot: math.floor(n * frac) for ot, frac in _ORDER_TYPE_TARGET_MIX.items()}
    assigned = sum(base.values())
    remainder = n - assigned
    # Hand out the leftover slots in the canonical order (market > limit > stop)
    # so the largest bucket absorbs rounding noise.
    leftover_order: list[OrderType] = ["market", "limit", "stop"]
    i = 0
    while remainder > 0:
        base[leftover_order[i % len(leftover_order)]] += 1
        remainder -= 1
        i += 1
    bag: list[OrderType] = []
    for ot in ("market", "limit", "stop"):
        bag.extend([ot] * base[ot])
    rng.shuffle(bag)
    return bag


def _allocate_symbols(n: int, symbols: tuple[str, ...], rng: random.Random) -> list[str]:
    """Round-robin allocate symbols, then shuffle.

    Round-robin first ensures perfect balance for small n; shuffle then breaks
    any unintended time-correlation between symbol and target time.
    """
    if not symbols:
        raise ValueError("symbols must be non-empty")
    bag: list[str] = [symbols[i % len(symbols)] for i in range(n)]
    rng.shuffle(bag)
    return bag


def _allocate_sides(n: int, rng: random.Random) -> list[Side]:
    """Half buys, half sells (rounding to majority buy on odd n), then shuffle."""
    n_buy = (n + 1) // 2
    buys: list[Side] = ["buy"] * n_buy
    sells: list[Side] = ["sell"] * (n - n_buy)
    bag: list[Side] = buys + sells
    rng.shuffle(bag)
    return bag


def _allocate_target_times(
    start_utc: datetime,
    duration_hours: float,
    n: int,
    rng: random.Random,
) -> list[datetime]:
    """Pick ``n`` UTC target times within ``[start, start + duration_hours)``.

    Distribution: ~30% snapped to within 30 min of a session-open or
    quiet-zone anchor (rounded into each day of the window), the remaining
    ~70% sampled uniformly across the window. Sorted ascending so the caller
    can iterate-and-sleep.
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    if duration_hours <= 0:
        raise ValueError(f"duration_hours must be positive, got {duration_hours}")
    end_utc = start_utc + timedelta(hours=duration_hours)
    n_anchored = max(1, round(n * 0.30))
    n_uniform = n - n_anchored

    anchors: list[datetime] = []
    # Build the candidate anchor pool: every (day, minute-of-day) pair inside
    # the window.
    cursor = start_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end_utc:
        for moy in _SESSION_OPENS_UTC_MINUTES + _QUIET_ZONES_UTC_MINUTES:
            candidate = cursor + timedelta(minutes=moy)
            if start_utc <= candidate < end_utc:
                anchors.append(candidate)
        cursor += timedelta(days=1)

    if not anchors:
        # Window too short to contain any anchor — fall back to all-uniform.
        n_anchored, n_uniform = 0, n

    anchored_picks: list[datetime] = []
    for _ in range(n_anchored):
        base = rng.choice(anchors)
        # Jitter ±30 min around the anchor so we sample the *neighborhood*, not
        # the same minute on every loop.
        jitter_min = rng.uniform(-30.0, 30.0)
        picked = base + timedelta(minutes=jitter_min)
        # Clip into window.
        if picked < start_utc:
            picked = start_utc
        elif picked >= end_utc:
            picked = end_utc - timedelta(seconds=1)
        anchored_picks.append(picked)

    window_secs = (end_utc - start_utc).total_seconds()
    uniform_picks: list[datetime] = [
        start_utc + timedelta(seconds=rng.uniform(0, window_secs)) for _ in range(n_uniform)
    ]

    return sorted(anchored_picks + uniform_picks)


def build_default_schedule(
    start_utc: datetime,
    duration_hours: float = 24.0,
    n_samples: int = 200,
    *,
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD"),
    seed: int = 20260513,
) -> SamplingSchedule:
    """Produce a deterministic sampling schedule.

    Parameters
    ----------
    start_utc
        UTC instant the schedule begins. Must be timezone-aware.
    duration_hours
        Width of the sampling window. Capped at ``HARD_TIME_LIMIT_HOURS``
        (48h) by ``main()`` — pure-helper accepts any positive value so tests
        can construct short windows.
    n_samples
        Number of attempts to schedule. The user-mandated target is 200 to
        absorb expected rejections and still land 100+ filled samples.
    symbols
        At least one symbol; default covers EURUSD and GBPUSD per the user's
        coverage requirement.
    seed
        Deterministic seed. Same seed + same args → identical schedule. The
        default value is the 2026-05-13 wave-6b dispatch date.

    Returns
    -------
    SamplingSchedule
        Sorted ascending by target time. Lengths of all four arrays match
        ``n_samples``.
    """
    if start_utc.tzinfo is None:
        raise ValueError("start_utc must be timezone-aware (UTC)")
    if n_samples <= 0:
        raise ValueError(f"n_samples must be positive, got {n_samples}")

    rng = random.Random(seed)
    # Generate the three independent bags first, then sort the targets so the
    # final schedule is chronological. Symbol/type/side are *not* re-sorted —
    # they retain their shuffled (deterministic) order, which is what we want:
    # the (time, symbol, type, side) tuple at index i is independent at the
    # schedule level even though each component is deterministic on its own.
    targets = _allocate_target_times(start_utc, duration_hours, n_samples, rng)
    order_types = _allocate_order_types(n_samples, rng)
    symbols_bag = _allocate_symbols(n_samples, symbols, rng)
    sides = _allocate_sides(n_samples, rng)
    return SamplingSchedule(
        targets=tuple(targets),
        symbols=tuple(symbols_bag),
        order_types=tuple(order_types),
        sides=tuple(sides),
    )


def _round_price(price: float, digits: int) -> float:
    """Round a price to the symbol's quote digits (5 for FX majors, 3 for JPY)."""
    return float(round(price, digits))


def build_order_request(
    open_req_template: dict[str, Any],
    *,
    order_type: OrderType,
    symbol_info_tick: Any,
    side: Side,
    inside_spread: bool = True,
    mt5_constants: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build an ``mt5.order_send`` request for the given order type.

    Pure helper — accepts the MT5 constants as an explicit mapping so unit
    tests can supply mock integers without importing the (Windows-only)
    MetaTrader5 module.

    Parameters
    ----------
    open_req_template
        A template dict with at least: ``symbol``, ``volume``, ``deviation``,
        ``type_filling``. Used so the caller centralizes broker-specific
        knobs.
    order_type
        ``"market"``, ``"limit"``, or ``"stop"``.
    symbol_info_tick
        An object with ``.bid``, ``.ask``, and ``.time`` attributes (a real
        ``mt5.Tick`` in production; a ``types.SimpleNamespace`` in tests).
    side
        ``"buy"`` or ``"sell"``.
    inside_spread
        For limit orders: ``True`` → place 5 pips inside the spread (likely
        to fill); ``False`` → place 5 pips outside (likely to reject). Stops
        always place outside (a stop entry placed inside spread fires
        instantly and degenerates into a market order, which would skew the
        recorded slippage).
    mt5_constants
        Dict mapping the constant *name* to the integer the MT5 pkg exposes.
        Must include ``TRADE_ACTION_DEAL``, ``TRADE_ACTION_PENDING``,
        ``ORDER_TYPE_BUY``, ``ORDER_TYPE_SELL``, ``ORDER_TYPE_BUY_LIMIT``,
        ``ORDER_TYPE_SELL_LIMIT``, ``ORDER_TYPE_BUY_STOP``,
        ``ORDER_TYPE_SELL_STOP``. ``main()`` builds this from ``mt5``; tests
        pass synthetic values.
    """
    if mt5_constants is None:
        raise ValueError("mt5_constants is required (pass mock dict in tests)")

    required_keys = {
        "TRADE_ACTION_DEAL",
        "TRADE_ACTION_PENDING",
        "ORDER_TYPE_BUY",
        "ORDER_TYPE_SELL",
        "ORDER_TYPE_BUY_LIMIT",
        "ORDER_TYPE_SELL_LIMIT",
        "ORDER_TYPE_BUY_STOP",
        "ORDER_TYPE_SELL_STOP",
    }
    missing = required_keys - mt5_constants.keys()
    if missing:
        raise ValueError(f"mt5_constants missing keys: {sorted(missing)}")

    symbol = str(open_req_template["symbol"])
    pip = _pip_size(symbol)
    digits = 3 if "JPY" in symbol.upper() else 5
    bid = float(symbol_info_tick.bid)
    ask = float(symbol_info_tick.ask)
    mid = (bid + ask) / 2.0

    base: dict[str, Any] = {
        "symbol": symbol,
        "volume": float(open_req_template["volume"]),
        "deviation": int(open_req_template["deviation"]),
        "type_filling": int(open_req_template["type_filling"]),
        # No SL/TP on the recording orders — round-trip-and-close is the goal,
        # and inline stops complicate the slippage attribution.
        "sl": 0.0,
        "tp": 0.0,
    }

    if order_type == "market":
        # action = DEAL, type = BUY/SELL, price = ask (buy) / bid (sell).
        base["action"] = mt5_constants["TRADE_ACTION_DEAL"]
        if side == "buy":
            base["type"] = mt5_constants["ORDER_TYPE_BUY"]
            base["price"] = _round_price(ask, digits)
        else:
            base["type"] = mt5_constants["ORDER_TYPE_SELL"]
            base["price"] = _round_price(bid, digits)
        return base

    if order_type == "limit":
        # Limit buy: price *below* current ask. Inside-spread = ask - 5 pips
        # (close to mid; likely fill). Outside-spread = bid - 5 pips (well
        # below; likely reject because price never trades there over the short
        # observation window). Sell limits mirror.
        base["action"] = mt5_constants["TRADE_ACTION_PENDING"]
        if side == "buy":
            base["type"] = mt5_constants["ORDER_TYPE_BUY_LIMIT"]
            if inside_spread:
                base["price"] = _round_price(mid - _PIP_DISTANCE * pip / 2, digits)
            else:
                base["price"] = _round_price(bid - _PIP_DISTANCE * pip, digits)
        else:
            base["type"] = mt5_constants["ORDER_TYPE_SELL_LIMIT"]
            if inside_spread:
                base["price"] = _round_price(mid + _PIP_DISTANCE * pip / 2, digits)
            else:
                base["price"] = _round_price(ask + _PIP_DISTANCE * pip, digits)
        return base

    if order_type == "stop":
        # Buy stop fires when price rises through trigger; place *above* ask.
        # Sell stop fires when price falls through trigger; place *below* bid.
        # Always outside-of-market by 5 pips so the order rests rather than
        # firing instantly.
        base["action"] = mt5_constants["TRADE_ACTION_PENDING"]
        if side == "buy":
            base["type"] = mt5_constants["ORDER_TYPE_BUY_STOP"]
            base["price"] = _round_price(ask + _PIP_DISTANCE * pip, digits)
        else:
            base["type"] = mt5_constants["ORDER_TYPE_SELL_STOP"]
            base["price"] = _round_price(bid - _PIP_DISTANCE * pip, digits)
        return base

    raise ValueError(f"unknown order_type: {order_type!r}")


#: Comment prefix used to mark records where the broker reported success
#: (retcode=10009) but the subsequent deal lookup returned None / empty,
#: so the helper has no authoritative ``fill_price`` to record. Downstream
#: consumers (Gate 2B) can distinguish "broker rejected" (raw retcode +
#: raw comment) from "filled but deal lookup failed" (this prefix).
DEAL_LOOKUP_FAILURE_PREFIX: Final[str] = "retcode_or_deal_failure: "


def parse_fill_into_record(
    *,
    run_id: str,
    request_time_utc: datetime,
    after_send_utc: datetime,
    open_req: dict[str, Any],
    order_send_result: Any,
    tick_at_request: Any,
    symbol_digits: int,
    order_type: OrderType,
    side: Side,
    success_retcode: int = 10009,
    actual_fill_price: float | None = None,
    actual_fill_time_utc: datetime | None = None,
) -> dict[str, Any]:
    """Convert one ``mt5.OrderSendResult`` (+ deal lookup) into a FillRecord dict.

    Pure helper — accepts mock-friendly inputs. ``main()`` calls it with the
    real ``mt5.OrderSendResult`` and the resolved deal-record scalars.

    The MT5 ``OrderSendResult.price`` field is 0.0 in most cases for market
    orders — the executed fill price lives in the deal record, retrieved
    via ``mt5.history_deals_get`` after ``order_send`` returns. This helper
    takes the resolved deal scalars as keyword-only inputs
    (``actual_fill_price`` and ``actual_fill_time_utc``) so the helper stays
    a pure data-shaping function with no ``mt5`` import. See the module
    docstring's "OrderSendResult vs. deal record" section for the full
    convention.

    Parameters
    ----------
    actual_fill_price
        Resolved fill price from ``mt5.history_deals_get(...)``'s deal
        record. ``None`` signals one of two cases:

        1. The order was rejected (``retcode != success_retcode``). The
           helper records ``fill_price = NaN`` and
           ``slippage_observed_pips = NaN``. Comment is the raw broker
           comment.
        2. The order succeeded (``retcode == success_retcode``) but the
           subsequent deal lookup returned ``None`` / empty (transient
           failure). The helper STILL records ``fill_price = NaN`` and
           ``slippage_observed_pips = NaN``, but prefixes the comment
           with :data:`DEAL_LOOKUP_FAILURE_PREFIX` so downstream consumers
           can identify this case.

        For pending limit/stop orders that ``main()`` immediately cancels,
        there is no fill, so ``actual_fill_price`` is ``None`` and the
        record correctly carries ``NaN`` fill_price (the existing
        behavior).
    actual_fill_time_utc
        Resolved fill time from the deal record (``deal.time`` as a tz-aware
        UTC ``datetime``). ``None`` triggers the ``after_send_utc`` fallback
        which is the Python-side wallclock — useful only for the
        soft-failure case; documented as not broker-authoritative.

    Slippage convention: **positive = adverse to trader**.
    * Buy: filled higher than requested → adverse → slippage > 0.
    * Sell: filled lower than requested → adverse → slippage > 0.

    Rejected fills (retcode != ``success_retcode``) emit a record with
    ``fill_price = NaN`` and ``slippage_observed_pips = NaN``. The retcode
    is preserved verbatim so downstream analysis can break the corpus down by
    rejection reason (10004 requote, 10018 market closed, 10019 no money,
    10030 unsupported filling mode, etc.).
    """
    retcode = int(order_send_result.retcode)
    raw_comment = str(getattr(order_send_result, "comment", "") or "")

    symbol = str(open_req["symbol"])
    # JPY pairs have digits=3 → pip = 0.01; FX majors digits=5 → pip = 0.0001.
    # symbol_digits is the broker-reported digits and authoritative; we use it
    # instead of hardcoding so an unusual quote convention (e.g. 4-digit FX
    # majors on some demo servers) still works correctly.
    pip = 10.0 ** -(symbol_digits - 1)

    bid = float(tick_at_request.bid)
    ask = float(tick_at_request.ask)
    spread_at_request_pips = (ask - bid) / pip

    requested_price = float(open_req.get("price", 0.0) or 0.0)
    broker_latency_ms = (after_send_utc - request_time_utc).total_seconds() * 1000.0

    comment = raw_comment

    if retcode != success_retcode:
        # Broker-side rejection — no deal, no fill price. Leave the comment
        # as-is (raw broker text like "Market closed", "Requote", …).
        fill_price = float("nan")
        slippage_pips = float("nan")
        broker_fill_time_utc = after_send_utc
    elif actual_fill_price is None:
        # Soft-failure: broker accepted (retcode 10009) but the deal lookup
        # returned None / empty. Cannot record fill_price; mark the row so
        # downstream consumers can distinguish this from a clean reject.
        fill_price = float("nan")
        slippage_pips = float("nan")
        # Fall back to wallclock — documented as Python-side, not broker-auth.
        broker_fill_time_utc = actual_fill_time_utc or after_send_utc
        comment = DEAL_LOOKUP_FAILURE_PREFIX + raw_comment
    else:
        fill_price = float(actual_fill_price)
        # Adverse-positive slippage.
        if side == "buy":
            slippage_pips = (fill_price - requested_price) / pip
        else:
            slippage_pips = (requested_price - fill_price) / pip
        if actual_fill_time_utc is not None:
            # Authoritative broker-side fill time (from deal record).
            broker_fill_time_utc = actual_fill_time_utc
        else:
            # Should be unreachable when ``actual_fill_price`` is provided —
            # ``main()`` resolves both atomically — but the fallback chain
            # (``result.time`` → ``after_send_utc``) is preserved as a last
            # resort for any caller that supplies a price without a time.
            result_time = getattr(order_send_result, "time", None)
            if result_time is None or result_time == 0:
                broker_fill_time_utc = after_send_utc
            else:
                # MT5 OrderSendResult.time is epoch seconds (broker-side).
                broker_fill_time_utc = datetime.fromtimestamp(int(result_time), tz=UTC)

    return {
        "run_id": run_id,
        "request_time_utc": request_time_utc,
        "broker_fill_time_utc": broker_fill_time_utc,
        "symbol": symbol,
        "order_type": order_type,
        "side": side,
        "volume_lots": float(open_req["volume"]),
        "requested_price": requested_price,
        "fill_price": fill_price,
        "spread_at_request_pips": spread_at_request_pips,
        "slippage_observed_pips": slippage_pips,
        "broker_latency_ms": broker_latency_ms,
        "retcode": retcode,
        "comment": comment,
    }


#: Stderr-log prefixes for the helper's three failure / ambiguity surfaces.
#: Documented so the operator can ``findstr`` / ``grep`` them on the next
#: VPS capture run.
_LOG_PREFIX_HISTORY_SELECT_FAILED: Final[str] = "[record_fills:history_select_failed]"
_LOG_PREFIX_MARKET_LOOKUP_FAILURE: Final[str] = "[record_fills:market_lookup_failure]"
_LOG_PREFIX_AMBIGUOUS_DEAL_MATCH: Final[str] = "[record_fills:ambiguous_deal_match]"


def _resolve_fill_from_deal(  # pragma: no cover - integration path
    mt5: Any,
    order_send_result: Any,
    *,
    success_retcode: int,
    request_time_utc: datetime,
    order_type: OrderType,
    symbol: str,
    volume_lots: float,
    side: Side,
    idx: int | None = None,
    claimed_deal_tickets: set[int] | None = None,
) -> tuple[float | None, datetime | None]:
    """Resolve the deal-record fill price + time for a successful order.

    Three-path lookup ordering (2026-05-14 fix v2):

    1. ``mt5.history_deals_get(ticket=result.deal)`` — fast path when the
       broker populated ``OrderSendResult.deal``.
    2. ``mt5.history_deals_get(position=result.order)`` filtered to
       ``DEAL_ENTRY_IN`` — fallback when ``deal == 0`` but ``order != 0``.
    3. ``mt5.history_deals_get(date_from, date_to)`` filtered by
       ``symbol``, ``volume`` (within float tolerance), ``side``
       (DEAL_TYPE_BUY / DEAL_TYPE_SELL), and ``entry == DEAL_ENTRY_IN``
       — most-robust fallback. MetaQuotes docs describe this overload as
       "receiving all history deals within a specified period in a single
       call similar to the HistoryDealsTotal and HistoryDealSelect tandem"
       — i.e. it implicitly drives the MQL5 ``HistorySelect`` step that
       populates the deal-history cache.

    Before any lookup, the helper attempts a defensive
    ``mt5.history_select(date_from=..., date_to=...)`` call iff
    ``hasattr(mt5, "history_select")``. This function is NOT in the
    documented Python MetaTrader5 API (the docs list only the MQL5
    server-side ``HistorySelect``), but the call is safe on builds where
    it is present and a no-op on builds where it is absent. On builds
    where it returns ``False`` (documented failure signal in MQL5), the
    helper emits a ``[record_fills:history_select_failed]`` stderr log
    and returns ``(None, None)`` so the row records ``NaN``.

    Parameters
    ----------
    mt5
        The imported ``MetaTrader5`` module (or a mock in tests).
    order_send_result
        Result of ``mt5.order_send(req)``. Carries ``retcode``, ``deal``,
        ``order``, ``volume``, etc.
    success_retcode
        ``mt5.TRADE_RETCODE_DONE`` (10009 on FTMO Demo).
    request_time_utc
        Wallclock UTC datetime at the moment ``mt5.order_send`` was
        called. Used to compute the time-range lookup window:
        ``[request_time_utc - HISTORY_LOOKUP_WINDOW_PAD_BEFORE,
        datetime.now(UTC) + HISTORY_LOOKUP_WINDOW_PAD_AFTER]``.
    order_type
        ``"market"`` / ``"limit"`` / ``"stop"``. Drives the
        market-vs-pending lookup-failure distinction (see module docstring
        "Market-vs-pending lookup-failure distinction" section).
    symbol
        Symbol the order targeted; used in the time-range filter.
    volume_lots
        Requested volume; used in the time-range filter with a small
        absolute tolerance to handle broker-side rounding.
    side
        ``"buy"`` or ``"sell"``; used in the time-range filter
        (mapped to ``DEAL_TYPE_BUY`` / ``DEAL_TYPE_SELL``).
    idx
        Optional iteration index from ``main()``; included in stderr
        logs for grep-ability.
    claimed_deal_tickets
        Optional session-scoped set of deal tickets that have already
        been attributed to a row. The time-range fallback skips any
        candidate whose ticket is in this set, then adds the chosen
        deal's ticket. Prevents double-attribution when two same-symbol
        same-side orders fire within the lookup window. The caller
        owns the set; passing ``None`` disables claim tracking (tests).

    Returns
    -------
    tuple[float | None, datetime | None]
        ``(fill_price, fill_time_utc)`` on success;
        ``(None, None)`` on any soft-failure case. The caller distinguishes
        "market with empty lookup" (loud error, log + counter increment)
        from "pending with empty lookup" (silent, expected) based on
        ``order_type``.
    """
    if int(order_send_result.retcode) != success_retcode:
        return (None, None)

    # Build the time-range window once; used both by the optional
    # ``history_select`` precondition and the time-range fallback below.
    # ``date_to`` anchors off ``max(now_utc, request_time_utc)`` so the
    # window stays well-formed even when the caller passes a future
    # ``request_time_utc`` (test fixtures use future timestamps; in
    # production ``request_time_utc`` is always just before ``order_send``
    # and ``now_utc`` is the later anchor).
    now_utc = datetime.now(tz=UTC)
    date_from = request_time_utc - HISTORY_LOOKUP_WINDOW_PAD_BEFORE
    date_to = max(now_utc, request_time_utc) + HISTORY_LOOKUP_WINDOW_PAD_AFTER

    # Defensive history_select precondition. Not officially in the Python
    # MetaTrader5 API docs (only the MQL5 server-side HistorySelect is
    # documented), but some MT5 builds expose it as the precondition that
    # populates the deal-history cache. Calling it via ``hasattr`` is safe
    # on all versions: present -> engages; absent -> skipped.
    history_select = getattr(mt5, "history_select", None)
    if history_select is not None:
        ok = True
        select_exc_type: str | None = None
        try:
            ok = history_select(date_from=date_from, date_to=date_to)
        except TypeError:
            # Older builds may use positional-only; retry positionally.
            try:
                ok = history_select(date_from, date_to)
            except Exception as exc:  # pragma: no cover — defensive
                ok = False
                select_exc_type = type(exc).__name__
        if ok is False:
            # Disambiguate "returned False" from "raised <Exc>" so the
            # operator can grep stderr and tell whether the broker is
            # reporting a select-failure or whether some other exception
            # short-circuited the call.
            raised_suffix = f" raised={select_exc_type}" if select_exc_type else ""
            print(
                f"{_LOG_PREFIX_HISTORY_SELECT_FAILED} "
                f"idx={idx if idx is not None else '?'} symbol={symbol} "
                f"order_type={order_type} side={side} "
                f"window=[{date_from.isoformat()},{date_to.isoformat()}]"
                f"{raised_suffix}",
                file=sys.stderr,
            )
            return (None, None)

    # Claim-tracking applies to ALL three paths, not just the time-range
    # fallback. A path-1 (ticket=) or path-2 (position=) hit that returns
    # an already-claimed deal would silently double-attribute one broker
    # deal across two synthetic-order rows; force fall-through to the
    # next path in that case so the second row soft-fails honestly.
    def _is_claimed(ticket: int) -> bool:
        return claimed_deal_tickets is not None and ticket in claimed_deal_tickets

    deal_ticket = int(getattr(order_send_result, "deal", 0) or 0)
    deal: Any | None = None
    if deal_ticket and not _is_claimed(deal_ticket):
        deals = mt5.history_deals_get(ticket=deal_ticket)
        if deals:
            cand = deals[0]
            cand_ticket = int(getattr(cand, "ticket", deal_ticket) or deal_ticket)
            if not _is_claimed(cand_ticket):
                deal = cand
    if deal is None:
        # Path 2 — position-keyed lookup, filtered to entry-side.
        order_ticket = int(getattr(order_send_result, "order", 0) or 0)
        if order_ticket:
            deals = mt5.history_deals_get(position=order_ticket)
            if deals:
                entry_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
                for cand in deals:
                    if int(getattr(cand, "entry", -1)) != int(entry_in):
                        continue
                    cand_ticket = int(getattr(cand, "ticket", 0) or 0)
                    if _is_claimed(cand_ticket):
                        continue
                    deal = cand
                    break
    if deal is None:
        # Path 3 — time-range fallback. The documented robust path:
        # MetaQuotes Python docs describe history_deals_get(date_from,
        # date_to) as receiving "all history deals within a specified
        # period in a single call similar to the HistoryDealsTotal and
        # HistoryDealSelect tandem", i.e. it drives the history cache
        # internally. Filter strictly by (symbol, volume, side, entry).
        deal = _time_range_fallback_lookup(
            mt5,
            date_from=date_from,
            date_to=date_to,
            request_time_utc=request_time_utc,
            symbol=symbol,
            volume_lots=volume_lots,
            side=side,
            idx=idx,
            order_type=order_type,
            claimed_deal_tickets=claimed_deal_tickets,
        )

    if deal is None:
        return (None, None)

    # Successful resolution — claim the ticket if tracking is enabled
    # so a subsequent time-range fallback cannot re-attribute it.
    if claimed_deal_tickets is not None:
        chosen_ticket = int(getattr(deal, "ticket", 0) or 0)
        if chosen_ticket:
            claimed_deal_tickets.add(chosen_ticket)

    price = float(getattr(deal, "price", 0.0) or 0.0)
    if price == 0.0:
        # Deal exists but reports a zero price — treat as soft-failure
        # rather than recording a zero. The 2026-05-13 bug capture taught
        # us never to trust a zero from any broker-side response.
        return (None, None)
    deal_time = getattr(deal, "time", None)
    if deal_time is None or int(deal_time) == 0:
        return (price, None)
    return (price, datetime.fromtimestamp(int(deal_time), tz=UTC))


def _time_range_fallback_lookup(  # pragma: no cover - integration path
    mt5: Any,
    *,
    date_from: datetime,
    date_to: datetime,
    request_time_utc: datetime,
    symbol: str,
    volume_lots: float,
    side: Side,
    idx: int | None,
    order_type: OrderType,
    claimed_deal_tickets: set[int] | None,
) -> Any | None:
    """Path-3 fallback: query by date range, filter by request attributes.

    Filter predicate, applied in order:

    * ``deal.symbol == symbol``
    * ``abs(deal.volume - volume_lots) < 1e-6`` (broker rounds 0.01 lot)
    * ``deal.entry == DEAL_ENTRY_IN`` (only the entry leg counts; exit
      legs and balance deals are excluded)
    * ``deal.type == DEAL_TYPE_BUY if side == "buy" else DEAL_TYPE_SELL``
    * If ``claimed_deal_tickets`` is provided, ``deal.ticket`` is not
      already claimed.

    Multi-match: pick the deal whose ``time`` is closest to
    ``request_time_utc`` and log
    ``[record_fills:ambiguous_deal_match]`` to stderr. The closest-time
    heuristic is correct when two consecutive same-symbol same-side
    market orders fire within the lookup window — the second order's
    deal will be milliseconds-later than the first.

    Returns
    -------
    Any | None
        The chosen deal, or ``None`` if no candidate matched.
    """
    deals = mt5.history_deals_get(date_from=date_from, date_to=date_to)
    if not deals:
        return None

    entry_in = int(getattr(mt5, "DEAL_ENTRY_IN", 0))
    deal_type_buy = int(getattr(mt5, "DEAL_TYPE_BUY", 0))
    deal_type_sell = int(getattr(mt5, "DEAL_TYPE_SELL", 1))
    want_type = deal_type_buy if side == "buy" else deal_type_sell
    request_ts = request_time_utc.timestamp()

    candidates: list[Any] = []
    for cand in deals:
        if str(getattr(cand, "symbol", "")) != symbol:
            continue
        cand_volume = float(getattr(cand, "volume", 0.0) or 0.0)
        if abs(cand_volume - float(volume_lots)) > 1e-6:
            continue
        if int(getattr(cand, "entry", -1)) != entry_in:
            continue
        if int(getattr(cand, "type", -1)) != want_type:
            continue
        cand_ticket = int(getattr(cand, "ticket", 0) or 0)
        if claimed_deal_tickets is not None and cand_ticket in claimed_deal_tickets:
            continue
        candidates.append(cand)

    if not candidates:
        return None

    # Closest-time match. The deal's time is broker-side epoch seconds.
    def _delta(c: Any) -> float:
        t = getattr(c, "time", None)
        if t is None:
            return float("inf")
        return abs(float(int(t)) - request_ts)

    candidates.sort(key=_delta)
    chosen = candidates[0]
    if len(candidates) > 1:
        print(
            f"{_LOG_PREFIX_AMBIGUOUS_DEAL_MATCH} "
            f"idx={idx if idx is not None else '?'} symbol={symbol} "
            f"order_type={order_type} side={side} "
            f"n_candidates={len(candidates)} "
            f"chosen_delta_s={_delta(chosen):.3f}",
            file=sys.stderr,
        )
    return chosen


def emit_market_lookup_failure_log(
    *,
    idx: int | None,
    symbol: str,
    order_type: OrderType,
    side: Side,
    request_time_utc: datetime,
    date_from: datetime,
    date_to: datetime,
) -> None:
    """Emit the structured stderr log for a market-order lookup failure.

    Extracted so tests can verify the exact format without invoking the
    full integration path. The format is:

    ``[record_fills:market_lookup_failure] idx=N symbol=S order=M side=D
    request_time=T window=[F,T]``

    A non-zero count of these logs in a 24h capture indicates the deal
    history-cache mechanism is not engaging on the broker / build pair,
    and Gate 2B will refuse the capture if the ratio exceeds 5%.
    """
    print(
        f"{_LOG_PREFIX_MARKET_LOOKUP_FAILURE} "
        f"idx={idx if idx is not None else '?'} symbol={symbol} "
        f"order={order_type} side={side} "
        f"request_time={request_time_utc.isoformat()} "
        f"window=[{date_from.isoformat()},{date_to.isoformat()}]",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# Parquet / manifest IO. Importable from tests, no MT5 dependency.
# --------------------------------------------------------------------------- #
def _records_to_dataframe(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a polars DataFrame with stable column order from a list of rows."""
    if not rows:
        # Return an empty frame with the right schema so downstream readers
        # don't choke on zero-row files.
        return pl.DataFrame(
            schema={
                "run_id": pl.Utf8,
                "request_time_utc": pl.Datetime(time_zone="UTC"),
                "broker_fill_time_utc": pl.Datetime(time_zone="UTC"),
                "symbol": pl.Utf8,
                "order_type": pl.Utf8,
                "side": pl.Utf8,
                "volume_lots": pl.Float64,
                "requested_price": pl.Float64,
                "fill_price": pl.Float64,
                "spread_at_request_pips": pl.Float64,
                "slippage_observed_pips": pl.Float64,
                "broker_latency_ms": pl.Float64,
                "retcode": pl.Int64,
                "comment": pl.Utf8,
            }
        )
    return pl.DataFrame(rows)


def _output_paths(run_id: str, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve the parquet + manifest paths for one run."""
    out_dir = root / "data" / "raw" / "fill_recordings"
    return (out_dir / f"{run_id}.parquet", out_dir / f"{run_id}.json")


def write_recording(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    start_utc: datetime,
    end_utc: datetime,
    root: pathlib.Path,
    success_retcode: int = 10009,
    n_market_lookup_failures: int = 0,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Persist ``rows`` to parquet and write the session manifest.

    Append-mode: if the parquet already exists for this ``run_id``, the new
    rows are concatenated to the existing dataframe. This makes a crashed-
    mid-session script resumable — the operator re-runs with the same
    ``run_id`` and the new rows append cleanly.

    Manifest schema v1.1 (2026-05-14 fix v2): the manifest carries a
    ``n_market_lookup_failures`` integer counting ``order_type ==
    "market"`` rows where ``retcode == 10009`` but the deal-lookup
    helper returned ``(None, None)``. Gate 2B refuses to consume any
    capture whose ratio of market lookup failures to filled market rows
    exceeds 5%. Callers running the integration path should thread the
    session-scoped counter through every flush so the manifest reflects
    the cumulative count, not just the post-last-flush slice.
    """
    parquet_path, manifest_path = _output_paths(run_id, root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    new_df = _records_to_dataframe(rows)
    if parquet_path.exists():
        existing = pl.read_parquet(parquet_path)
        df = pl.concat([existing, new_df], how="vertical_relaxed")
    else:
        df = new_df
    df.write_parquet(parquet_path)

    n_attempted = df.height
    filled_mask = pl.col("retcode") == success_retcode
    n_filled = int(df.filter(filled_mask).height)
    n_filled_market = int(df.filter(filled_mask & (pl.col("order_type") == "market")).height)
    n_rejected = n_attempted - n_filled
    manifest = SessionManifest(
        run_id=run_id,
        start_utc=start_utc,
        end_utc=end_utc,
        n_attempted=n_attempted,
        n_filled=n_filled,
        n_filled_market=n_filled_market,
        n_rejected=n_rejected,
        n_market_lookup_failures=int(n_market_lookup_failures),
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return parquet_path, manifest_path


# --------------------------------------------------------------------------- #
# CLI argument parser. Importable, returns Namespace.
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record FTMO MT5 demo fills to parquet for Gate 2B."
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=24.0,
        help="Wall-clock duration of the recording session. Hard-capped at 48h.",
    )
    parser.add_argument(
        "--n-samples",
        type=int,
        default=200,
        help="Target number of fill attempts (some will reject; budget ~30%% over the target).",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="EURUSD,GBPUSD",
        help="Comma-separated FX symbols. Must include EURUSD plus at least one other major.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Reuse this run_id to resume a crashed session. Defaults to a fresh UUID.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260513,
        help="Deterministic schedule seed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the schedule and print it; do NOT connect or place orders.",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# main() — only place where MetaTrader5 is imported.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:  # pragma: no cover - integration path
    args = _parse_args(argv)

    duration_hours = min(float(args.duration_hours), HARD_TIME_LIMIT_HOURS)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if "EURUSD" not in symbols:
        raise SystemExit("EURUSD must be in --symbols (the user-mandated coverage rule).")

    run_id = args.run_id or uuid.uuid4().hex
    start_utc = datetime.now(tz=UTC)
    schedule = build_default_schedule(
        start_utc=start_utc,
        duration_hours=duration_hours,
        n_samples=int(args.n_samples),
        symbols=symbols,
        seed=int(args.seed),
    )

    print(
        f"[record_fills] run_id={run_id} start_utc={start_utc.isoformat()} "
        f"n_samples={len(schedule)} duration_hours={duration_hours} "
        f"symbols={list(symbols)}"
    )

    if args.dry_run:
        for i, (t, s, ot, sd) in enumerate(
            zip(
                schedule.targets,
                schedule.symbols,
                schedule.order_types,
                schedule.sides,
                strict=True,
            )
        ):
            print(f"  [{i:03d}] {t.isoformat()} {s} {ot} {sd}")
        return

    # MetaTrader5 has no macOS/Linux wheel for type resolution; the
    # `import-not-found` is silenced here so this module remains importable
    # for unit tests on macOS while still type-checking under mypy --strict.
    # The deferred-import pattern is the same as scripts/spike_mt5.py.
    import MetaTrader5 as mt5  # type: ignore[import-not-found]

    repo_root = pathlib.Path(__file__).resolve().parents[1]

    creds = json.loads(pathlib.Path.home().joinpath(".propfarm-secrets.json").read_text())[
        "ftmo_demo"
    ]
    if not mt5.initialize(login=creds["login"], password=creds["password"], server=creds["server"]):
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")

    try:
        # SAFETY ASSERT #1 — server must be FTMO-Demo*. Never run against a
        # funded or non-demo account.
        account = mt5.account_info()
        if account is None:
            raise SystemExit("mt5.account_info() returned None; cannot verify server")
        if not str(account.server).startswith(ALLOWED_SERVER_PREFIX):
            raise SystemExit(
                f"refusing to record on server={account.server!r} "
                f"(must start with {ALLOWED_SERVER_PREFIX!r})"
            )
        print(f"[record_fills] connected to server={account.server} login={account.login}")

        constants = {
            "TRADE_ACTION_DEAL": mt5.TRADE_ACTION_DEAL,
            "TRADE_ACTION_PENDING": mt5.TRADE_ACTION_PENDING,
            "ORDER_TYPE_BUY": mt5.ORDER_TYPE_BUY,
            "ORDER_TYPE_SELL": mt5.ORDER_TYPE_SELL,
            "ORDER_TYPE_BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
            "ORDER_TYPE_SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
            "ORDER_TYPE_BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
            "ORDER_TYPE_SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
        }
        success_retcode = int(mt5.TRADE_RETCODE_DONE)
        template = {
            "symbol": "EURUSD",  # overridden per-iteration
            "volume": LOT_SIZE,
            "deviation": 10,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        session_deadline = start_utc + timedelta(hours=HARD_TIME_LIMIT_HOURS)
        rows: list[dict[str, Any]] = []
        rng = random.Random(int(args.seed) ^ 0xA5A5)  # for inside_spread flips
        n_attempted = 0
        n_exceptions = 0
        # 2026-05-14 fix v2: session-scoped counter for market-order rows
        # whose deal lookup returned (None, None). Propagated into the
        # manifest as `n_market_lookup_failures`; Gate 2B refuses captures
        # where this exceeds 5% of filled market rows.
        n_market_lookup_failures = 0
        # 2026-05-14 fix v2: session-scoped set of deal tickets already
        # attributed to a row, so the time-range fallback in
        # `_resolve_fill_from_deal` cannot double-attribute one deal to
        # two consecutive same-symbol same-side rows fired within the
        # ~6-second lookup window.
        claimed_deal_tickets: set[int] = set()
        exception_type_counts: Counter[str] = Counter()

        for idx, (target, symbol, order_type, side) in enumerate(
            zip(
                schedule.targets,
                schedule.symbols,
                schedule.order_types,
                schedule.sides,
                strict=True,
            )
        ):
            try:
                now = datetime.now(tz=UTC)
                if now >= session_deadline:
                    print(f"[record_fills] hit 48h hard cap at idx={idx}; stopping")
                    break
                # W6b reviewer fix: wait UNTIL the scheduled target, even if the
                # gap is > 1h. The earlier version did a single `time.sleep(min(
                # sleep_s, 3600))` and then unconditionally fell through to the
                # `order_send` block, which fired the order up to (gap - 1h)
                # before its scheduled time. Functionally rare at n=200/24h (avg
                # gap ~7 min), but a real defect under low-n or quiet-zone-heavy
                # schedules. The 1h-cap-per-sleep stays as a watchdog (so a
                # corrupt entry can't block forever in a single syscall), but is
                # now wrapped in a loop that re-checks `now` and the deadline.
                while True:
                    now = datetime.now(tz=UTC)
                    if now >= session_deadline:
                        break
                    if now >= target:
                        break
                    sleep_s = (target - now).total_seconds()
                    time.sleep(min(sleep_s, 3600.0))
                if datetime.now(tz=UTC) >= session_deadline:
                    print(f"[record_fills] hit 48h hard cap at idx={idx}; stopping")
                    break

                # SAFETY ASSERT #2 — open position cap.
                open_positions = mt5.positions_get() or ()
                if len(open_positions) >= MAX_SIMULTANEOUS_POSITIONS:
                    print(
                        f"[record_fills] {len(open_positions)} open positions "
                        f">= cap {MAX_SIMULTANEOUS_POSITIONS}; force-closing all and aborting"
                    )
                    _force_close_all(mt5, open_positions, constants, template)
                    raise SystemExit("position cap reached; session aborted")

                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    print(f"[record_fills] idx={idx} tick None for {symbol}; skipping")
                    continue

                template_for_symbol = {**template, "symbol": symbol}
                inside = bool(order_type == "limit" and rng.random() < 0.6)
                req = build_order_request(
                    template_for_symbol,
                    order_type=order_type,
                    symbol_info_tick=tick,
                    side=side,
                    inside_spread=inside,
                    mt5_constants=constants,
                )

                request_time = datetime.now(tz=UTC)
                result = mt5.order_send(req)
                after_send = datetime.now(tz=UTC)
                n_attempted += 1

                # Resolve the authoritative fill price + time from the deal
                # record for ALL successful orders (2026-05-14 fix v2). For
                # market orders the deal MUST exist; for limit / stop pending
                # orders the deal does NOT exist (the order is queued, not
                # filled) and the helper's empty return is the correct
                # silent-NaN behavior. The order_type-aware
                # market_lookup_failure logging below distinguishes the two.
                actual_fill_price, actual_fill_time = _resolve_fill_from_deal(
                    mt5,
                    result,
                    success_retcode=success_retcode,
                    request_time_utc=request_time,
                    order_type=order_type,
                    symbol=symbol,
                    volume_lots=float(req["volume"]),
                    side=side,
                    idx=idx,
                    claimed_deal_tickets=claimed_deal_tickets,
                )

                # Market-vs-pending lookup-failure distinction. A market
                # order whose deal lookup returned None after all three
                # paths is a loud error: the broker confirmed the fill
                # but Python could not retrieve the deal record. A
                # limit / stop pending order whose lookup returned None
                # is expected — no fill yet.
                if (
                    order_type == "market"
                    and int(result.retcode) == success_retcode
                    and actual_fill_price is None
                ):
                    n_market_lookup_failures += 1
                    _window_from = request_time - HISTORY_LOOKUP_WINDOW_PAD_BEFORE
                    _window_to = (
                        max(datetime.now(tz=UTC), request_time) + HISTORY_LOOKUP_WINDOW_PAD_AFTER
                    )
                    emit_market_lookup_failure_log(
                        idx=idx,
                        symbol=symbol,
                        order_type=order_type,
                        side=side,
                        request_time_utc=request_time,
                        date_from=_window_from,
                        date_to=_window_to,
                    )

                symbol_info = mt5.symbol_info(symbol)
                digits = int(symbol_info.digits) if symbol_info is not None else 5
                row = parse_fill_into_record(
                    run_id=run_id,
                    request_time_utc=request_time,
                    after_send_utc=after_send,
                    open_req=req,
                    order_send_result=result,
                    tick_at_request=tick,
                    symbol_digits=digits,
                    order_type=order_type,
                    side=side,
                    success_retcode=success_retcode,
                    actual_fill_price=actual_fill_price,
                    actual_fill_time_utc=actual_fill_time,
                )
                rows.append(row)
                fill_repr = (
                    f"{row['fill_price']:.5f}"
                    if isinstance(row["fill_price"], float) and not math.isnan(row["fill_price"])
                    else "NaN"
                )
                slip_repr = (
                    f"{row['slippage_observed_pips']:.2f}"
                    if isinstance(row["slippage_observed_pips"], float)
                    and not math.isnan(row["slippage_observed_pips"])
                    else "NaN"
                )
                print(
                    f"[record_fills] idx={idx:03d} {symbol} {order_type} {side} "
                    f"retcode={row['retcode']} fill={fill_repr} "
                    f"slip_pips={slip_repr} "
                    f"latency_ms={row['broker_latency_ms']:.1f}"
                )

                # Round-trip-and-close — only for market fills that filled. Limit
                # and stop pending orders are cancelled instead so we don't leave
                # resting orders accumulating.
                if row["retcode"] == success_retcode and order_type == "market":
                    _close_market_position(mt5, symbol, side, constants, template_for_symbol)
                elif row["retcode"] == success_retcode and order_type in ("limit", "stop"):
                    _cancel_pending_order(mt5, result, constants)

                # Periodic flush — every 10 fills, write to disk so a crash loses
                # at most 10 records.
                if (idx + 1) % 10 == 0:
                    end_utc = datetime.now(tz=UTC)
                    write_recording(
                        rows,
                        run_id=run_id,
                        start_utc=start_utc,
                        end_utc=end_utc,
                        root=repo_root,
                        success_retcode=success_retcode,
                        n_market_lookup_failures=n_market_lookup_failures,
                    )
                    rows = []  # already on disk; avoid double-append next flush
            except SystemExit:
                # Hard-bail conditions (position cap, server prefix mismatch)
                # MUST propagate. Flush whatever we have first.
                try:
                    end_utc = datetime.now(tz=UTC)
                    write_recording(
                        rows,
                        run_id=run_id,
                        start_utc=start_utc,
                        end_utc=end_utc,
                        root=repo_root,
                        success_retcode=success_retcode,
                        n_market_lookup_failures=n_market_lookup_failures,
                    )
                except Exception:  # pragma: no cover — best-effort flush
                    pass
                raise
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # The 2026-05-13 Wave-6b capture exited with LastTaskResult=1
                # at iteration 110/200 — unknown cause, likely a transient
                # broker / network exception on one specific order_send. To
                # survive a future bad row, log to STDERR (Task Scheduler's
                # stdout is buffered/lost on hidden jobs) and continue.
                # The structured prefix lets the operator grep for these on
                # the next run: `findstr "[record_fills:exception]" stderr.log`.
                n_exceptions += 1
                exception_type_counts[type(exc).__name__] += 1
                print(
                    f"[record_fills:exception] idx={idx:03d} symbol={symbol} "
                    f"order_type={order_type} side={side} "
                    f"exc_type={type(exc).__name__} exc_msg={exc!r}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                continue

        end_utc = datetime.now(tz=UTC)
        write_recording(
            rows,
            run_id=run_id,
            start_utc=start_utc,
            end_utc=end_utc,
            root=repo_root,
            success_retcode=success_retcode,
            n_market_lookup_failures=n_market_lookup_failures,
        )
        # Summary to stderr too, so the operator can confirm completion even
        # if stdout was discarded by the scheduler.
        summary = (
            f"[record_fills] session complete: run_id={run_id} "
            f"scheduled={len(schedule)} attempted={n_attempted} "
            f"exceptions={n_exceptions} exc_types={dict(exception_type_counts)} "
            f"market_lookup_failures={n_market_lookup_failures}"
        )
        print(summary)
        print(summary, file=sys.stderr)
    except SystemExit:
        # Propagate after the finally block runs mt5.shutdown().
        raise
    except Exception as exc:
        # Any pre-loop / loop-runaway exception that escapes the per-iteration
        # try/except still gets visibility on stderr before mt5.shutdown().
        print(
            f"[record_fills:fatal] run_id={run_id} exc_type={type(exc).__name__} exc_msg={exc!r}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        mt5.shutdown()


def _force_close_all(  # pragma: no cover - integration path
    mt5: Any,
    positions: Any,
    constants: dict[str, int],
    template: dict[str, Any],
) -> None:
    """Sweep-close every open position. Called when the cap is breached."""
    for pos in positions:
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            continue
        opposite = (
            constants["ORDER_TYPE_SELL"]
            if int(pos.type) == constants["ORDER_TYPE_BUY"]
            else constants["ORDER_TYPE_BUY"]
        )
        close_req = {
            "action": constants["TRADE_ACTION_DEAL"],
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": opposite,
            "position": int(pos.ticket),
            "price": float(tick.bid if opposite == constants["ORDER_TYPE_SELL"] else tick.ask),
            "deviation": int(template["deviation"]),
            "type_filling": int(template["type_filling"]),
            "sl": 0.0,
            "tp": 0.0,
        }
        mt5.order_send(close_req)


def _close_market_position(  # pragma: no cover - integration path
    mt5: Any,
    symbol: str,
    side: Side,
    constants: dict[str, int],
    template: dict[str, Any],
) -> None:
    """Close the just-opened market position on ``symbol``."""
    positions = mt5.positions_get(symbol=symbol) or ()
    if not positions:
        return
    pos = positions[-1]  # most recent
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    opposite_type = constants["ORDER_TYPE_SELL"] if side == "buy" else constants["ORDER_TYPE_BUY"]
    close_price = float(tick.bid if side == "buy" else tick.ask)
    close_req = {
        "action": constants["TRADE_ACTION_DEAL"],
        "symbol": symbol,
        "volume": float(pos.volume),
        "type": opposite_type,
        "position": int(pos.ticket),
        "price": close_price,
        "deviation": int(template["deviation"]),
        "type_filling": int(template["type_filling"]),
        "sl": 0.0,
        "tp": 0.0,
    }
    mt5.order_send(close_req)


def _cancel_pending_order(  # pragma: no cover - integration path
    mt5: Any,
    place_result: Any,
    constants: dict[str, int],
) -> None:
    """Cancel a pending (limit/stop) order by its ticket."""
    order_ticket = getattr(place_result, "order", None)
    if not order_ticket:
        return
    cancel_req = {
        "action": getattr(mt5, "TRADE_ACTION_REMOVE", 8),  # TRADE_ACTION_REMOVE=8
        "order": int(order_ticket),
    }
    mt5.order_send(cancel_req)


if __name__ == "__main__":  # pragma: no cover
    main()
