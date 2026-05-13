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
"""

from __future__ import annotations

__all__: list[str] = []
