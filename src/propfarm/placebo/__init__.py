"""Placebo strategy + Gate 1 acceptance gate (Task 13.1).

Gate 1 of Phase 0: random entries run through the full
``simulate_fill`` + commission + swap pipeline produce a mean P&L that is
within ``3 * empirical_noise_floor`` of the deterministic cost floor
``-(spread + slippage + commission + swap)``. Positive expectancy is a
simulator alpha leak (look-ahead, missing costs, sign bug); negative
expectancy beyond the noise floor is a double-counted or over-aggressive
cost. Either failure trips the gate.
"""

from propfarm.placebo.gate import PlaceboGateResult, run_placebo_gate
from propfarm.placebo.random_strategy import PlaceboTrade, generate_random_trades

__all__ = [
    "PlaceboGateResult",
    "PlaceboTrade",
    "generate_random_trades",
    "run_placebo_gate",
]
