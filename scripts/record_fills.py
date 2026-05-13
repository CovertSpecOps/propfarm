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
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

OrderType = Literal["market", "limit", "stop"]
Side = Literal["buy", "sell"]

SCHEMA_VERSION: Final[str] = "1.0"
LOT_SIZE: Final[float] = 0.01
MAX_SIMULTANEOUS_POSITIONS: Final[int] = 5
HARD_TIME_LIMIT_HOURS: Final[float] = 48.0
ALLOWED_SERVER_PREFIX: Final[str] = "FTMO-Demo"

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
    """End-of-session manifest written alongside the parquet."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    start_utc: datetime
    end_utc: datetime
    n_attempted: int
    n_filled: int
    n_rejected: int
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
) -> dict[str, Any]:
    """Convert one ``mt5.OrderSendResult`` into a FillRecord dict.

    Pure helper — accepts mock-friendly inputs. ``main()`` calls it with the
    real ``mt5.OrderSendResult``.

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
    comment = str(getattr(order_send_result, "comment", "") or "")

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

    if retcode != success_retcode:
        fill_price = float("nan")
        slippage_pips = float("nan")
        broker_fill_time_utc = after_send_utc
    else:
        fill_price = float(order_send_result.price)
        # Adverse-positive slippage.
        if side == "buy":
            slippage_pips = (fill_price - requested_price) / pip
        else:
            slippage_pips = (requested_price - fill_price) / pip
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
) -> tuple[pathlib.Path, pathlib.Path]:
    """Persist ``rows`` to parquet and write the session manifest.

    Append-mode: if the parquet already exists for this ``run_id``, the new
    rows are concatenated to the existing dataframe. This makes a crashed-
    mid-session script resumable — the operator re-runs with the same
    ``run_id`` and the new rows append cleanly.
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
    n_filled = int(df.filter(pl.col("retcode") == success_retcode).height)
    n_rejected = n_attempted - n_filled
    manifest = SessionManifest(
        run_id=run_id,
        start_utc=start_utc,
        end_utc=end_utc,
        n_attempted=n_attempted,
        n_filled=n_filled,
        n_rejected=n_rejected,
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

        for idx, (target, symbol, order_type, side) in enumerate(
            zip(
                schedule.targets,
                schedule.symbols,
                schedule.order_types,
                schedule.sides,
                strict=True,
            )
        ):
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
            )
            rows.append(row)
            print(
                f"[record_fills] idx={idx:03d} {symbol} {order_type} {side} "
                f"retcode={row['retcode']} fill={row['fill_price']} "
                f"slip_pips={row['slippage_observed_pips']:.2f} "
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
                )
                rows = []  # already on disk; avoid double-append next flush

        end_utc = datetime.now(tz=UTC)
        write_recording(
            rows,
            run_id=run_id,
            start_utc=start_utc,
            end_utc=end_utc,
            root=repo_root,
            success_retcode=success_retcode,
        )
        print(f"[record_fills] session complete: run_id={run_id}")
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
