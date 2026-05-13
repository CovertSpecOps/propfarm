"""Walk-forward optimizer with IS/OOS Sharpe gate (Task 8.2).

Why this module exists
----------------------
**Walk-forward analysis** is the time-series-respecting alternative to
k-fold cross-validation: each fold's train window precedes its test
window in calendar time, so the optimizer never peeks at the future
when scoring a parameter set. The optimizer iterates rolling or
expanding ``train -> test`` windows, evaluates a parameter grid in each
train window, and reports the train/test Sharpe pairs.

Downstream consumers in this package use those pairs differently:

* :mod:`propfarm.validation.pbo` aggregates IS/OOS rankings to estimate
  the *probability of backtest overfitting* (Bailey & Borwein 2017).
* The Phase-3 strategy gate enforced here rejects parameter sets whose
  OOS Sharpe collapses relative to IS — the plan's formula is
  ``mean(OOS) >= gate_ratio * mean(IS) AND mean(OOS) > gate_oos_min``,
  with the spec defaults ``gate_ratio=0.5`` and ``gate_oos_min=0.8``
  taken from the Phase 0 plan.

This module is the **wrapper, not the strategy**: it accepts an
optional 1-D parameter grid that is broadcast against the returns to
form per-parameter strategy curves (``returns * weight[p]``). Hard-coded
strategy logic (signals, position sizing, regime filters) lives in
``propfarm.strategy`` — and reviewers will flag any leakage of that
logic into this file.

Output convention
-----------------
* :func:`walk_forward_split` is a *pure index generator*. It yields
  :class:`WalkForwardWindow` instances containing ``int64`` index
  arrays — never touching the underlying returns. This makes it trivial
  to compose with other backtest harnesses (e.g. CPCV's purge/embargo
  step is a layer on top of the same index discipline).
* :func:`evaluate` is the **uniform entry point** mandated by the
  validation-package contract (see ``src/propfarm/validation/__init__.py``):
  ``evaluate(returns: np.ndarray, **kwargs) -> WalkForwardResult``.
* :class:`SharpePair` carries the per-(fold, param) IS/OOS Sharpe pair.
  The full ``pairs`` tuple is what downstream tools (PBO, DSR) consume.

Sharpe definition
-----------------
We use the **sample-std Sharpe**::

    Sharpe = mean(r) / std(r, ddof=1) * sqrt(annualization)

with ``annualization=252`` (daily returns) as the default. When
``std == 0`` the function returns NaN — this is the load-bearing
behavior the *degenerate returns* test exercises. NaN propagates
cleanly through ``np.nanmean`` / ``np.nanstd`` consumers downstream.

Public API
----------
* :class:`WalkForwardWindow`
* :class:`SharpePair`
* :class:`WalkForwardResult`
* :func:`walk_forward_split`
* :func:`evaluate`
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Plan-default gate ratio: OOS Sharpe must be at least half of IS Sharpe.
DEFAULT_GATE_RATIO: Final[float] = 0.5

#: Plan-default OOS Sharpe floor: an additional absolute lower bound.
DEFAULT_GATE_OOS_MIN: Final[float] = 0.8

#: Plan-default annualization factor (trading days per year).
DEFAULT_ANNUALIZATION: Final[int] = 252


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
class WalkForwardWindow(BaseModel):
    """One ``(train, test)`` index pair from the walk-forward iterator.

    Parameters
    ----------
    train_idx, test_idx : np.ndarray
        ``int64`` index arrays into the original returns series.
        ``train_idx`` always precedes ``test_idx`` in time
        (``max(train_idx) < min(test_idx)``).
    fold_id : int
        Zero-indexed sequence number; the first window has ``fold_id=0``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    train_idx: np.ndarray
    test_idx: np.ndarray
    fold_id: int


class SharpePair(BaseModel):
    """In-sample and out-of-sample Sharpe for one parameter set in one window.

    The ``param_key`` is a stable identifier — typically a JSON-serialized
    parameter dict — so downstream tools (PBO matrix construction, etc.)
    can group pairs by parameter set without inspecting the raw values.
    """

    model_config = ConfigDict(frozen=True)

    fold_id: int
    param_key: str
    is_sharpe: float
    oos_sharpe: float


class WalkForwardResult(BaseModel):
    """Output of :func:`evaluate`: train/test Sharpe pairs + gate verdict.

    Parameters
    ----------
    n_folds : int
        Number of walk-forward windows that produced pairs.
    n_params : int
        Size of the parameter grid (``1`` when ``param_grid is None`` —
        the raw returns are evaluated as a single "no-parameter" strategy).
    pairs : tuple[SharpePair, ...]
        All per-(fold, param) Sharpe pairs in deterministic order:
        sorted by ``fold_id`` then by the iteration order of ``param_grid``.
    gate_passed : bool
        Result of the gate formula
        ``mean(OOS) >= gate_ratio * mean(IS) AND mean(OOS) > gate_oos_min``
        applied across all pairs. NaN pairs cause the mean to be NaN, and
        any NaN ``mean(OOS)`` short-circuits to ``gate_passed=False``.
    gate_threshold : float
        The ``gate_ratio`` actually used (echoed for downstream auditing).
    gate_oos_min : float
        The OOS-Sharpe floor actually used.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    n_folds: int
    n_params: int
    pairs: tuple[SharpePair, ...]
    gate_passed: bool
    gate_threshold: float
    gate_oos_min: float

    @model_validator(mode="after")
    def _check_pair_count(self) -> WalkForwardResult:
        expected = self.n_folds * self.n_params
        if len(self.pairs) != expected:
            raise ValueError(
                f"pair-count mismatch: n_folds={self.n_folds} * n_params={self.n_params} "
                f"= {expected}, got {len(self.pairs)} pairs"
            )
        # Reviewer-required: the (fold_id, param_key) set must cover the full
        # Cartesian product. The bare length check is insufficient — a buggy
        # `evaluate()` could double-count fold 0 and skip fold 2 while keeping
        # the pair count correct, producing wrong PBO/DSR inputs downstream.
        distinct = {(p.fold_id, p.param_key) for p in self.pairs}
        if len(distinct) != expected:
            raise ValueError(
                f"pair-set mismatch: expected {expected} distinct "
                f"(fold_id, param_key) tuples covering the Cartesian product, "
                f"got {len(distinct)} distinct out of {len(self.pairs)} pairs "
                f"(duplicate or missing combinations)"
            )
        return self


# --------------------------------------------------------------------------- #
# Public functions
# --------------------------------------------------------------------------- #
def walk_forward_split(
    n_obs: int,
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    mode: Literal["rolling", "expanding"] = "rolling",
) -> Iterator[WalkForwardWindow]:
    """Yield walk-forward ``(train, test)`` index pairs.

    Parameters
    ----------
    n_obs : int
        Length of the observation sequence to split.
    train_size : int
        * ``mode="rolling"``: fixed train window size, kept constant
          across folds.
        * ``mode="expanding"``: the *minimum* train size; the first fold's
          train window is ``train_size`` long and grows by ``step``
          each subsequent fold.
    test_size : int
        Length of every fold's test window. The test window is always
        non-overlapping with its train window and immediately follows it.
    step : int, optional
        Step size in observations between consecutive folds. Defaults to
        ``test_size`` (i.e. non-overlapping test windows — the standard
        walk-forward configuration). Must be strictly positive.
    mode : {"rolling", "expanding"}, default "rolling"
        Window discipline (see ``train_size`` for the semantics).

    Yields
    ------
    WalkForwardWindow
        One window per fold, with ``fold_id`` starting at 0.

    Raises
    ------
    ValueError
        * If any of ``n_obs``, ``train_size``, ``test_size`` is non-positive,
          or if ``step`` is non-positive when explicitly supplied.
        * If ``n_obs < train_size + test_size`` (cannot fit even one fold).
        * If ``mode`` is not one of ``"rolling"`` / ``"expanding"``.

    Notes
    -----
    The iterator terminates once advancing by ``step`` would push the
    test window past ``n_obs``. The final fold always satisfies
    ``train_end + test_size <= n_obs`` exactly.
    """
    if n_obs <= 0:
        raise ValueError(f"n_obs must be positive, got {n_obs}")
    if train_size <= 0:
        raise ValueError(f"train_size must be positive, got {train_size}")
    if test_size <= 0:
        raise ValueError(f"test_size must be positive, got {test_size}")
    if step is not None and step <= 0:
        raise ValueError(f"step must be positive when supplied, got {step}")
    if mode not in ("rolling", "expanding"):
        raise ValueError(f"mode must be 'rolling' or 'expanding', got {mode!r}")
    if n_obs < train_size + test_size:
        raise ValueError(
            f"n_obs={n_obs} too small for train_size={train_size} + test_size={test_size}"
        )

    effective_step = step if step is not None else test_size
    fold_id = 0
    train_start = 0
    train_end = train_size  # exclusive
    while train_end + test_size <= n_obs:
        if mode == "rolling":
            train_idx = np.arange(train_start, train_end, dtype=np.int64)
        else:  # expanding
            train_idx = np.arange(0, train_end, dtype=np.int64)
        test_idx = np.arange(train_end, train_end + test_size, dtype=np.int64)
        yield WalkForwardWindow(train_idx=train_idx, test_idx=test_idx, fold_id=fold_id)
        fold_id += 1
        train_start += effective_step
        train_end += effective_step


def _sharpe(returns: np.ndarray, annualization: int) -> float:
    """Annualized Sharpe with sample std (ddof=1). NaN when std == 0 or n < 2."""
    if returns.size < 2:
        return float("nan")
    mean = float(np.mean(returns))
    std = float(np.std(returns, ddof=1))
    if std == 0.0 or not np.isfinite(std):
        return float("nan")
    return mean / std * float(np.sqrt(annualization))


def _iter_grid(param_grid: dict[str, np.ndarray]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(param_key, param_dict)`` for the Cartesian product of the grid.

    The ``param_key`` is the JSON serialization of the sorted-key parameter
    dict — stable across runs and OS-independent. For a 1-D grid (a single
    parameter axis) this is just ``{"name": value}``; the implementation
    supports N-D grids without baking in 1-D assumptions.
    """
    if not param_grid:
        yield "{}", {}
        return
    keys = sorted(param_grid.keys())
    arrays = [param_grid[k] for k in keys]
    # itertools.product would also work; we use np.ndindex to keep numpy semantics.
    shape = tuple(len(a) for a in arrays)
    for idx in np.ndindex(*shape):
        combo: dict[str, Any] = {
            k: arrays[i][j] for i, (k, j) in enumerate(zip(keys, idx, strict=True))
        }
        # JSON serialize with float coercion so np.float64 round-trips cleanly.
        coerced = {
            k: (float(v) if isinstance(v, np.floating | np.integer) else v)
            for k, v in combo.items()
        }
        key = json.dumps(coerced, sort_keys=True)
        yield key, combo


def _apply_param(returns: np.ndarray, param: dict[str, Any]) -> np.ndarray:
    """Broadcast a parameter dict to a per-bar weight on the returns series.

    The walk-forward optimizer is **strategy-agnostic**: it does not know
    what a parameter means. We treat each numeric parameter value as a
    scalar weight applied to the entire window — so a parameter sweep of
    ``{"w": [0.5, 1.0, 1.5]}`` produces three strategy curves
    (``returns * 0.5``, ``returns * 1.0``, ``returns * 1.5``). The
    Sharpe of a positive-scalar-weighted series equals the Sharpe of the
    unweighted series (the scalar cancels in ``mean/std``), so this acts
    as a sign / leverage sweep — a deliberately minimal contract that
    keeps the optimizer wrapper-only. Real strategies live in
    ``propfarm.strategy`` and pass *their own* per-bar weights in via
    the param dict's values (the values may be arrays of len(returns)).
    """
    if not param:
        return returns
    weight = np.asarray(1.0, dtype=np.float64)
    for v in param.values():
        weight = weight * np.asarray(v, dtype=np.float64)
    return returns * weight


def evaluate(
    returns: np.ndarray,
    *,
    param_grid: dict[str, np.ndarray] | None = None,
    train_size: int = 252,
    test_size: int = 63,
    mode: Literal["rolling", "expanding"] = "rolling",
    annualization: int = DEFAULT_ANNUALIZATION,
    gate_ratio: float = DEFAULT_GATE_RATIO,
    gate_oos_min: float = DEFAULT_GATE_OOS_MIN,
) -> WalkForwardResult:
    """Run a walk-forward optimization and apply the IS/OOS Sharpe gate.

    Parameters
    ----------
    returns : np.ndarray
        1-D array of per-bar returns (typically daily log or simple
        returns). Must contain at least ``train_size + test_size``
        observations or a :class:`ValueError` is raised.
    param_grid : dict[str, np.ndarray], optional
        Each key is a parameter name; each value is the 1-D array of
        candidate values along that axis. If ``None`` (the default),
        the raw returns are evaluated as a single "no-parameter"
        strategy and ``n_params == 1``.
    train_size, test_size : int
        Fold sizing (see :func:`walk_forward_split`). Defaults of
        ``252 / 63`` correspond to one year IS, one quarter OOS for daily
        returns.
    mode : {"rolling", "expanding"}, default "rolling"
        Window discipline.
    annualization : int, default 252
        Scaling factor inside ``mean / std * sqrt(annualization)``.
    gate_ratio : float, default 0.5
        OOS/IS Sharpe ratio threshold for the gate. The plan default
        encodes "OOS must be at least half of IS".
    gate_oos_min : float, default 0.8
        Absolute OOS Sharpe floor. The gate ANDs this with the ratio
        condition.

    Returns
    -------
    WalkForwardResult

    Raises
    ------
    ValueError
        If ``returns`` has fewer than ``train_size + test_size`` elements
        or is not 1-D.

    Notes
    -----
    The gate compares **mean across all pairs** of OOS Sharpe against
    ``gate_ratio * mean(IS Sharpe)`` and against ``gate_oos_min``. A NaN
    in ``mean(OOS)`` (e.g. all test windows degenerate to zero std) forces
    ``gate_passed=False``.
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    if arr.size < train_size + test_size:
        raise ValueError(
            f"returns has {arr.size} obs, need at least "
            f"train_size + test_size = {train_size + test_size}"
        )

    grid = param_grid if param_grid is not None else {}
    param_list = list(_iter_grid(grid))
    n_params = len(param_list)

    pairs_list: list[SharpePair] = []
    windows = list(
        walk_forward_split(
            arr.size,
            train_size=train_size,
            test_size=test_size,
            mode=mode,
        )
    )
    for window in windows:
        train_returns = arr[window.train_idx]
        test_returns = arr[window.test_idx]
        for param_key, param in param_list:
            is_curve = _apply_param(train_returns, param)
            oos_curve = _apply_param(test_returns, param)
            pairs_list.append(
                SharpePair(
                    fold_id=window.fold_id,
                    param_key=param_key,
                    is_sharpe=_sharpe(is_curve, annualization),
                    oos_sharpe=_sharpe(oos_curve, annualization),
                )
            )

    pairs = tuple(pairs_list)
    is_array = np.array([p.is_sharpe for p in pairs], dtype=np.float64)
    oos_array = np.array([p.oos_sharpe for p in pairs], dtype=np.float64)
    is_mean = float(np.mean(is_array)) if is_array.size > 0 else float("nan")
    oos_mean = float(np.mean(oos_array)) if oos_array.size > 0 else float("nan")
    if np.isnan(oos_mean) or np.isnan(is_mean):
        gate_passed = False
    else:
        gate_passed = (oos_mean >= gate_ratio * is_mean) and (oos_mean > gate_oos_min)

    return WalkForwardResult(
        n_folds=len(windows),
        n_params=n_params,
        pairs=pairs,
        gate_passed=gate_passed,
        gate_threshold=gate_ratio,
        gate_oos_min=gate_oos_min,
    )


__all__ = [
    "DEFAULT_ANNUALIZATION",
    "DEFAULT_GATE_OOS_MIN",
    "DEFAULT_GATE_RATIO",
    "SharpePair",
    "WalkForwardResult",
    "WalkForwardWindow",
    "evaluate",
    "walk_forward_split",
]
