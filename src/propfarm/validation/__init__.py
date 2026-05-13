"""Validation sub-package: CPCV, walk-forward, DSR, PBO, Monte Carlo, stress.

Modules in this package implement López de Prado / Bailey-style backtest
validation tools. Each module exposes a uniform ``evaluate(returns, **kwargs)``
entry point and a frozen result type so a generic Phase-3 deploy bar can
iterate them without per-tool special-casing.
"""
