"""Canonical :class:`MarketState` — the single market-context object that flows
through every sim-layer module (spread, slippage, fill engine, stress replay).

Origin (2026-05-13): Tasks 6.1 (spread) and 7.1 (slippage) each defined a local
``MarketState`` during parallel Wave-6b dispatch. The two definitions diverged
on the ``stress_mode`` field (slippage had it, spread did not), creating two
nominally-distinct Pydantic types with the same name. The Wave 6b reviewer
flagged this as a HIGH-severity blocker for Wave 6c (fill engine), which would
otherwise accumulate adapter code converting between two near-identical models.

This module is the consolidation: a single :class:`MarketState` with the
superset of fields. ``propfarm.sim.spread`` and ``propfarm.sim.slippage`` both
re-export it (so existing call sites keep working) and import from here as the
single source of truth. The fill engine (Task 7.2, Wave 6c) imports from here
directly.

Vendor-convention note (per the W6a-derived reviewer playbook): ``realized_vol_5m``
is **annualized**, not per-bar (e.g. 0.15 == 15% annualized vol). Every consumer
must read this convention from the field docstring rather than re-deriving from
context.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MarketState(BaseModel):
    """Snapshot of the prevailing market regime at one instant in time.

    Frozen by design so a downstream consumer (spread / slippage / fill engine /
    stress replay) cannot mutate the caller's state mid-evaluation.

    Attributes
    ----------
    symbol : str
        Trading symbol. Consumers may validate against
        :data:`propfarm.data.quality.SUPPORTED_SYMBOLS`.
    ts_utc : datetime.datetime
        Snapshot timestamp. **Must be tz-aware UTC.** Consumers raise
        ``ValueError`` at the public ``evaluate`` boundary on naive
        datetimes — predicates do not re-validate on the hot path.
    realized_vol_5m : float | None
        Annualized realized return volatility over the most-recent 5 minutes
        (fraction, not percent — e.g. ``0.15`` == 15% annualized vol).
        ``None`` falls back to module-specific defaults. Must be non-negative
        when provided.
    news_window : bool
        True if the snapshot falls inside a published economic-event window
        (NFP, CPI, central-bank decisions). The spread + slippage modules
        multiply by their respective ``news_multiplier`` when this is True.
        This module does NOT decide *when* news is — that's the news-calendar
        module's responsibility (a later module).
    stress_mode : bool
        True if the snapshot is being replayed under a Task 10.2 stress
        scenario (2008, SNB, GBP flash, COVID, UK gilts, SVB). When True,
        the slippage model applies its calibrated ``stress_multiplier``
        (10-20x for FX, ~5x for indices). The spread model currently
        ignores this flag — stress replay drives the spread via the
        ``news_window`` flag and event-specific calibration entries.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts_utc: datetime
    realized_vol_5m: float | None = None
    news_window: bool = False
    stress_mode: bool = False


__all__ = ["MarketState"]
