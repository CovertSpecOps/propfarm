"""Rules-as-code: per-firm prop-firm rule predicates (Task 11.x).

This package exposes the :class:`Predicate` ABC and one submodule per firm
(:mod:`propfarm.rules.ftmo`, plus :mod:`propfarm.rules.fundednext` and
:mod:`propfarm.rules.fundingpips` once W4b lands). Each firm submodule
declares a tuple of :class:`Predicate` instances covering the firm's daily
drawdown, max drawdown, profit-target, banned-techniques, and other rules.

The :class:`Predicate` ABC carries a ``confidence: Literal["high",
"uncertain"]`` flag that mirrors the W3 pattern on
:class:`propfarm.sim.commission.CommissionTable` and
:class:`propfarm.sim.swap.SwapTable`. High-confidence predicates trip the
kill switch on breach; uncertain predicates surface to the daily
auto-report. See ``predicates.py`` for the full semantics.

Predicate :meth:`Predicate.evaluate` returns an :class:`Event` — either a
:class:`Violation` (rule breach; severity follows confidence) or an
:class:`Achievement` (non-failure completion event; never trips the kill
switch). The two-event-type design separates "is the rule interpretive?"
from "is the event a failure?" — fixing the conflation a reviewer caught
in the original W4a draft where ``confidence="uncertain"`` was overloaded
to coerce ``severity="warn"`` on profit-target predicates.
"""

from __future__ import annotations

from propfarm.rules.predicates import (
    AccountState,
    Achievement,
    CandidateTrade,
    Event,
    OpenPosition,
    Predicate,
    Violation,
)
from propfarm.rules.registry import ALL_FIRM_PREDICATES, ALL_MODEL_PREDICATES

__all__ = [
    "ALL_FIRM_PREDICATES",
    "ALL_MODEL_PREDICATES",
    "AccountState",
    "Achievement",
    "CandidateTrade",
    "Event",
    "OpenPosition",
    "Predicate",
    "Violation",
]
