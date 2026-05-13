"""Random-entry, vol-targeted placebo strategy (Task 13.1).

Why this module exists
----------------------
The Gate 1 placebo gate runs random entries through the **full** fill engine
+ cost-model pipeline and asserts that the mean P&L is statistically
indistinguishable from the negative of the modelled cost floor. A positive
expectancy means the simulator is leaking alpha (look-ahead, missing cost
component, wrong sign on swap/financing); a too-negative expectancy means
costs are being double-counted. Either way, Gate 1 fails and forward work
stops until the leak is fixed.

The strategy itself must be *demonstrably zero-edge*:

* **Random direction.** 50/50 buy/sell, drawn from a seeded RNG.
* **Vol-targeted size.** Size = ``target_dollar_vol_per_trade /
  (price * rolling_vol * point_value)``. This keeps per-trade dollar risk
  roughly constant across the regime so a few high-vol periods do not
  dominate the bootstrap noise floor.
* **Fixed hold.** Each trade is held for a fixed number of bars, closed at
  the market. No stops, no targets, no signals — pure noise resampling.
* **No look-ahead.** Sizing at index ``i`` uses returns up to and including
  index ``i - 1`` (a rolling window strictly in the past). Entry price at
  index ``i`` is the price at the *end* of bar ``i - 1`` (the prevailing
  mid before the entry decision). Exit price is at index ``i + hold_bars``,
  i.e. ``hold_bars`` whole bars after entry. The placebo gate test
  ``test_full_pipeline_consumes_canonical_modules`` and the lookahead
  linter would catch any drift.

Substrate convention (load-bearing for Gate 1)
----------------------------------------------
The canonical fixture ``fixtures/synthetic_returns.parquet`` carries
**daily** log returns indexed by business-day timestamps at 00:00 UTC.
Every "bar" in this module is one row of the fixture. The user-facing
parameter ``hold_bars`` therefore means "hold across this many daily bars".
The default of 5 (one trading week) is chosen so that each trade crosses
~4 NY rollovers and ~1 Wednesday triple, exercising the swap leg of the
cost model in the gate aggregation. A larger hold would over-weight swap
relative to spread/commission; a smaller hold would never exercise swap.

Price walk
----------
The fixture stores **log returns**. We reconstruct a synthetic mid-price
series by ``price[i] = starting_mid_price * exp(cumsum(log_returns[:i]))``.
The starting mid is configurable (default 1.10000, matching the EURUSD
mid we calibrate the spread/slip models against). The series is used both
as the "open price" for each trade and as the "close price" ``hold_bars``
later. Because the fixture is a single shared series across the choppy
regime — not 2000 independent simulations — the trade-entry indices are
*sampled without replacement* (each trade gets a distinct entry index) so
that overlapping trades do not artificially inflate correlation in the
gate's residual.

Point-value convention
----------------------
EURUSD: 1 lot = 100,000 EUR notional; 1 pip = $10 per lot (point_value=10).
USDJPY: 1 pip = ~$10 per lot at typical mids.
XAUUSD: 1 lot = 100 oz; 1 pip ($0.01 move) = $1 per lot (point_value=1).
GER40 / US100: 1 lot ≈ $1 per point (point_value=1).

The placebo gate ships with **EURUSD only** because the canonical fixture
is a single 1-D return series; cross-symbol asymmetry would require a
separate return path per symbol, which is out of scope for Gate 1 — the
gate's job is to certify the cost pipeline, not the cross-symbol surface.

Vendor-convention caveat
------------------------
``point_value_per_lot_usd`` is the placebo's local mirror of the per-pip
dollar value, used for vol-targeted sizing and PnL conversion. It must
match the ``point_value_usd`` map inside the swap table for any symbol
we trade — otherwise the swap leg and the directional leg would compute
USD on different scales and the gate residual would be a unit error, not
a real leak. The default for EURUSD (point_value_per_lot_usd=10.0,
swap point_value_usd=1.0) is correct because the swap table's
``swap_long_points`` is already expressed in *USD-per-lot-per-night*
(i.e. the broker "points" are dollars, not pips); see the
``swap.point_value_usd`` docstring which seeds ``1.0`` for every symbol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------- #
# Per-symbol "USD per pip per lot" — the placebo's local mirror.
# --------------------------------------------------------------------------- #
# This is *not* the swap table's point_value_usd (which has its own broker-
# points convention). It is the dollar value of a one-pip price move on
# one lot, used both for vol-targeted sizing and for converting fill-price
# deltas + spread/slippage into USD.
#
# EURUSD: 1 lot = 100,000 EUR. 1 pip = 0.0001 price move = $10 per lot.
# USDJPY: 1 pip = 0.01 price move = ~$10 per lot at ~150 mid (slightly varies).
# XAUUSD: 1 lot = 100 oz. pip = 0.01 ($0.01 move) → $1 per lot.
# GER40, US100: 1 point = $1 per lot (index CFDs at $1/point).
_POINT_VALUE_PER_LOT_USD: Final[dict[str, float]] = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDJPY": 10.0,
    "XAUUSD": 1.0,
    "GER40": 1.0,
    "US100": 1.0,
}

#: Per-symbol pip size (price units). Mirrors the fill engine's _SYMBOL_DIGITS
#: formula ``pip = 10 ** -(digits - 1)``. Re-declared here so this module
#: does not reach into the fill engine's private map.
_PIP_SIZE: Final[dict[str, float]] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.01,
    "GER40": 1.0,
    "US100": 1.0,
}


def _point_value_usd(symbol: str) -> float:
    """Return USD per one-pip move per one lot for ``symbol``.

    Raises
    ------
    ValueError
        If ``symbol`` is not in :data:`_POINT_VALUE_PER_LOT_USD`.
    """
    try:
        return _POINT_VALUE_PER_LOT_USD[symbol]
    except KeyError as exc:
        raise ValueError(
            f"unknown symbol {symbol!r}; supported: {sorted(_POINT_VALUE_PER_LOT_USD)}"
        ) from exc


def _pip_size(symbol: str) -> float:
    """Return the pip size in price units for ``symbol``."""
    try:
        return _PIP_SIZE[symbol]
    except KeyError as exc:
        raise ValueError(f"unknown symbol {symbol!r}; supported: {sorted(_PIP_SIZE)}") from exc


# --------------------------------------------------------------------------- #
# PlaceboTrade model
# --------------------------------------------------------------------------- #
class PlaceboTrade(BaseModel):
    """One synthetic round-trip the placebo strategy produces.

    Frozen by design — once a trade is generated and priced through the
    simulator pipeline, no downstream code can mutate its cost attribution
    out from under the gate's aggregation.

    Convention
    ----------
    * ``spread_cost_usd``, ``slippage_cost_usd``, ``commission_usd``: positive
      values = USD charged to the trader (cost magnitudes).
    * ``swap_usd``: signed in *trader convention* — negative = trader was
      charged, positive = trader was credited, zero = no overnight crossings.
      (This is the **opposite** sign of the simulator's
      ``swap_for_position`` return, which uses "positive = cost". The
      conversion happens inside :func:`generate_random_trades` so consumers
      of :class:`PlaceboTrade` never need to worry about the sim/trader
      sign flip.)
    * ``realized_pnl_usd``: full round-trip P&L in USD including every cost
      component. A profitable trade has positive ``realized_pnl_usd``; a
      losing trade has negative.

    Attributes
    ----------
    symbol : str
        Trading symbol (Gate 1 ships EURUSD only).
    open_ts_utc, close_ts_utc : datetime.datetime
        Tz-aware UTC timestamps of the open and close legs.
    side : Literal["buy", "sell"]
        Trade direction.
    volume_lots : float
        Order size in lots, > 0.
    open_price, close_price : float
        Mid prices at open and close (the *requested* prices passed into
        the fill engine).
    open_fill_price, close_fill_price : float
        Realized fill prices from the simulator (with slippage applied).
    spread_cost_usd : float
        Total spread cost across the round trip (sum of half-spread on
        each leg), in USD. Always >= 0.
    slippage_cost_usd : float
        Total slippage cost across the round trip, in USD. Always >= 0.
    commission_usd : float
        Round-trip commission, in USD. Always >= 0.
    swap_usd : float
        Trader-convention swap. Negative = charged, positive = credited.
    realized_pnl_usd : float
        Full round-trip realized P&L including all costs.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    open_ts_utc: datetime
    close_ts_utc: datetime
    side: Literal["buy", "sell"]
    volume_lots: float
    open_price: float
    close_price: float
    open_fill_price: float
    close_fill_price: float
    spread_cost_usd: float
    slippage_cost_usd: float
    commission_usd: float
    swap_usd: float
    realized_pnl_usd: float


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def _rolling_vol_estimate(log_returns: np.ndarray, *, index: int, window: int) -> float:
    """Return a rolling-vol estimate at ``index``, using returns strictly before it.

    Uses the trailing ``window`` log-returns ending at ``index - 1`` (no
    look-ahead). For ``index < 2`` the estimate falls back to the
    population std of the available returns; for ``index < window`` the
    estimate uses whatever returns are available.

    Returns the *per-bar* sample std of log returns. The caller multiplies
    by ``sqrt(annualization_factor)`` if it wants annualized vol, but for
    vol-targeted sizing the per-bar number is the right one (we are
    estimating the per-bar price-move scale).
    """
    start = max(0, index - window)
    end = index  # exclusive — strictly past
    if end - start < 2:
        # Not enough data to estimate; fall back to a typical-regime default
        # so sizing never explodes on the first few bars.
        return 0.01
    window_arr = log_returns[start:end]
    sigma = float(np.std(window_arr, ddof=1))
    return sigma if sigma > 0.0 else 0.01


def _vol_targeted_size(
    *,
    target_dollar_vol_per_trade: float,
    price: float,
    per_bar_vol: float,
    point_value_per_lot_usd: float,
    pip_size: float,
) -> float:
    """Compute volume_lots such that one-bar dollar vol per trade ≈ target.

    The per-trade dollar move scale is::

        dollar_vol_per_lot = price * per_bar_vol * (point_value_per_lot_usd / pip_size)

    where ``price * per_bar_vol`` is the per-bar price move in price units,
    and ``point_value_per_lot_usd / pip_size`` converts price units to USD
    per lot (for EURUSD that ratio is 10/0.0001 = 100,000 — i.e. the lot
    notional in USD, as expected). So::

        volume_lots = target / dollar_vol_per_lot

    Vendor-convention catch: this is the standard retail FX vol-targeted
    sizing formula. For metals and indices the ``point_value_per_lot_usd /
    pip_size`` ratio is much smaller (1/0.01=100 for XAUUSD), reflecting
    the smaller notional per lot.

    Caps the result at the [0.01, 100] lot range so a degenerate
    near-zero vol estimate doesn't produce an absurd size that the fill
    engine's slippage model has no calibration for.
    """
    dollar_vol_per_lot = price * per_bar_vol * (point_value_per_lot_usd / pip_size)
    if dollar_vol_per_lot <= 0.0:
        return 0.01
    raw = target_dollar_vol_per_trade / dollar_vol_per_lot
    return float(max(0.01, min(100.0, raw)))


def generate_random_trades(
    *,
    returns: np.ndarray,
    timestamps: np.ndarray,
    symbol: str,
    n_trades: int,
    hold_bars: int = 5,
    target_dollar_vol_per_trade: float = 100.0,
    starting_mid_price: float = 1.10000,
    rng_seed: int = 20260513,
    vol_window: int = 20,
) -> tuple[float, ...]:
    """Return a deterministic ndarray of (open_idx, side, size) trade specs.

    Walk the return series. At each randomly-chosen entry index (sampled
    *without replacement* from the valid index range), pick a random
    direction (50/50 buy/sell), compute a vol-targeted size from the
    rolling-vol estimate strictly *before* that index, and hold for
    ``hold_bars`` rows.

    This function returns a flat tuple of floats so it can serve as the
    deterministic spec input to :func:`run_placebo_gate`. The gate then
    routes each spec through the fill engine + cost model and converts to
    a :class:`PlaceboTrade`. This split exists so the strategy is
    deterministic in (seed, returns, timestamps, symbol, params) without
    needing to pre-compute every cost.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of per-bar log returns. The substrate the strategy walks.
    timestamps : np.ndarray
        1-D array of ``datetime64`` (any UTC unit). Same length as
        ``returns``. The simulator converts each to tz-aware UTC.
    symbol : str
        Trading symbol. Must be a key in :data:`_POINT_VALUE_PER_LOT_USD`.
    n_trades : int
        Number of trades to generate. Capped at the count of valid entry
        indices (those with ``hold_bars`` of room before the array end).
    hold_bars : int, default 5
        Number of bars to hold each trade. The default of 5 — one
        trading week on the daily-bar canonical fixture — is chosen so
        that each trade crosses ~4 NY rollovers and ~1 Wednesday triple,
        exercising the swap leg of the cost model in the gate aggregation.
    target_dollar_vol_per_trade : float, default 100.0
        Target per-trade dollar vol. Default 100 USD = 1% of a $10,000
        account, a realistic vol budget per trade.
    starting_mid_price : float, default 1.10000
        Mid price at index 0. The rest of the price series is reconstructed
        as ``starting_mid * exp(cumsum(returns))``.
    rng_seed : int, default 20260513
        RNG seed. Same seed → byte-identical spec sequence.
    vol_window : int, default 20
        Look-back window for the rolling-vol estimate (in bars).

    Returns
    -------
    tuple[float, ...]
        Flat sequence of ``(open_idx, side_code, volume_lots,
        open_price, close_idx, close_price)`` 6-tuples, packed flat. Use
        :func:`unpack_trade_spec` (defined alongside) to walk.

        ``side_code``: 0.0 = buy, 1.0 = sell.

    Raises
    ------
    ValueError
        If ``returns`` and ``timestamps`` lengths differ, if ``n_trades``
        is non-positive, or if there is not enough room in the substrate
        for even one trade of length ``hold_bars``.
    """
    if returns.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {returns.shape}")
    if timestamps.ndim != 1:
        raise ValueError(f"timestamps must be 1-D, got shape {timestamps.shape}")
    if returns.size != timestamps.size:
        raise ValueError(f"returns/timestamps length mismatch: {returns.size} vs {timestamps.size}")
    if n_trades < 1:
        raise ValueError(f"n_trades must be >= 1, got {n_trades}")
    if hold_bars < 1:
        raise ValueError(f"hold_bars must be >= 1, got {hold_bars}")

    n = returns.size
    max_open_idx = n - hold_bars - 1
    if max_open_idx < 1:
        raise ValueError(
            f"substrate too short: n={n}, hold_bars={hold_bars}, max_open_idx={max_open_idx}"
        )

    # Reconstruct the synthetic mid-price series. price[i] = starting *
    # exp(cumsum(returns)[i]) where cumsum is the sum of returns at-or-before i.
    # We use returns[0..i-1] to compute price[i], i.e. price[0] = starting.
    log_cum = np.concatenate(([0.0], np.cumsum(returns)))
    prices = starting_mid_price * np.exp(log_cum[: n + 1])
    # prices[i] is the price at the start of bar i. Entry at index i uses
    # prices[i] as the open price; exit at index i+hold uses prices[i+hold].

    rng = np.random.default_rng(rng_seed)

    # Valid entry indices: [1 .. max_open_idx]. We require index >= 1 so the
    # rolling-vol window has at least one return to look at.
    valid_indices = np.arange(1, max_open_idx + 1, dtype=np.int64)
    n_valid = valid_indices.size
    n_actual = min(n_trades, n_valid)

    # Sample without replacement to keep trades non-overlapping at the
    # entry index. (They may still overlap *during* their hold windows,
    # which is fine — the gate aggregates across trades, and the bootstrap
    # noise floor is computed on the per-trade cost distribution, not on
    # the equity curve.)
    chosen = rng.choice(valid_indices, size=n_actual, replace=False)
    chosen.sort()  # walk forward in time for readability

    # Side: 50/50 binomial draws via uniform.
    side_uniform = rng.random(size=n_actual)

    out: list[float] = []
    for k in range(n_actual):
        open_idx = int(chosen[k])
        close_idx = open_idx + hold_bars
        per_bar_vol = _rolling_vol_estimate(returns, index=open_idx, window=vol_window)
        open_price = float(prices[open_idx])
        close_price = float(prices[close_idx])
        size_lots = _vol_targeted_size(
            target_dollar_vol_per_trade=target_dollar_vol_per_trade,
            price=open_price,
            per_bar_vol=per_bar_vol,
            point_value_per_lot_usd=_point_value_usd(symbol),
            pip_size=_pip_size(symbol),
        )
        side_code = 0.0 if side_uniform[k] < 0.5 else 1.0
        out.extend(
            (
                float(open_idx),
                side_code,
                size_lots,
                open_price,
                float(close_idx),
                close_price,
            )
        )
    return tuple(out)


def unpack_trade_spec(
    spec: tuple[float, ...],
) -> list[tuple[int, Literal["buy", "sell"], float, float, int, float]]:
    """Unpack a flat trade-spec tuple from :func:`generate_random_trades`.

    Returns a list of ``(open_idx, side, volume_lots, open_price,
    close_idx, close_price)`` tuples.

    Raises
    ------
    ValueError
        If the spec length is not a multiple of 6.
    """
    if len(spec) % 6 != 0:
        raise ValueError(f"spec length {len(spec)} is not a multiple of 6 — corrupt spec")
    out: list[tuple[int, Literal["buy", "sell"], float, float, int, float]] = []
    for k in range(0, len(spec), 6):
        open_idx = int(spec[k])
        side_code = spec[k + 1]
        side: Literal["buy", "sell"] = "buy" if side_code == 0.0 else "sell"
        size = spec[k + 2]
        open_price = spec[k + 3]
        close_idx = int(spec[k + 4])
        close_price = spec[k + 5]
        out.append((open_idx, side, size, open_price, close_idx, close_price))
    return out


def to_utc_datetime(ts: np.datetime64) -> datetime:
    """Convert a ``numpy.datetime64`` to a tz-aware UTC ``datetime``.

    The canonical fixture stores timestamps as naive ``datetime64[ns]`` at
    00:00 (business day index). We assume those naive instants are UTC.
    """
    py_dt = ts.astype("datetime64[us]").astype(object)
    if not isinstance(py_dt, datetime):
        raise TypeError(f"expected datetime conversion, got {type(py_dt).__name__}")
    if py_dt.tzinfo is None:
        return py_dt.replace(tzinfo=UTC)
    return py_dt.astimezone(UTC)


__all__ = [
    "PlaceboTrade",
    "generate_random_trades",
    "to_utc_datetime",
    "unpack_trade_spec",
]
