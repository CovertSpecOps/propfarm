"""Phase-0 acceptance gates package.

This package houses the two end-of-Phase-0 acceptance harnesses:

* Gate 1 (placebo) — random-entry strategy collapses to negative expectancy
  equal to cost floor. Owned by ``propfarm.placebo``; re-exported from there
  in the parallel Wave 6 dispatch.
* Gate 2B (sim-vs-live fill comparison) — drive each row of the FTMO MT5
  demo capture parquet through ``propfarm.sim.fill_engine.simulate_fill``
  and compute per-field residuals. Owned by :mod:`propfarm.gates.gate_2b`.

Both gates write a deterministic markdown + parquet artifact pair the
operator inspects before declaring Phase 0 complete.
"""

from propfarm.gates.gate_2b import (
    SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS,
    SYMBOL_SPREAD_P95_THRESHOLD_PIPS,
    Gate2BReport,
    MarketStateReconstruction,
    ResidualDistribution,
    ResidualRow,
    reconstruct_fill_request,
    reconstruct_market_state,
    run_gate_2b,
)

__all__ = [
    "SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS",
    "SYMBOL_SPREAD_P95_THRESHOLD_PIPS",
    "Gate2BReport",
    "MarketStateReconstruction",
    "ResidualDistribution",
    "ResidualRow",
    "reconstruct_fill_request",
    "reconstruct_market_state",
    "run_gate_2b",
]
