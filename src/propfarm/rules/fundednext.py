"""FundedNext rule predicates (Task 11.2 — W4b).

Concrete consumer of :class:`propfarm.rules.predicates.Predicate` covering
the four documented FundedNext challenge models (Stellar 2-Step, Stellar
1-Step, Stellar Lite). Stellar Instant is **not** encoded — FundedNext has
not published a dedicated help-center rule article for it as of
2026-05-12; the legacy Express model has been deprecated.

This module **does not modify the W4a ABC**. The W4a abstractions
(:class:`Predicate`, :class:`Violation`, :class:`Achievement`,
:class:`AccountState`, :class:`OpenPosition`, :class:`CandidateTrade`) are
inherited unchanged. Per-class fields (``model``, ``threshold_fraction``)
are added on FundedNext predicate subclasses for metadata only — they do
not extend the ABC.

Source of truth
---------------
Every predicate's ``tos_quote`` is verbatim from
``docs/firm-tos-snapshots/fundednext-rules-2026-05-12.md`` — that file is
the canonical interpretation of FundedNext's published rules as of the
retrieval date, and the integrity test
``test_fundednext_predicate_tos_quotes_appear_in_snapshot`` asserts each
quote still appears in the snapshot at test time, closing the silent-drift
window between code and source-of-truth.

Server-midnight handling
------------------------
FundedNext's MT5 platform server runs on **GMT+2 winter / GMT+3 summer** —
the same EET/EEST timezone family as FTMO (see the snapshot file).
``zoneinfo.ZoneInfo("Europe/Athens")`` is the canonical IANA zone. The
:func:`server_midnight_before` helper is **deliberately shared with FTMO**
via re-export — anything firm-agnostic stays in ftmo.py for W4b, per
reviewer's "do not duplicate" guidance, and is re-exported through this
module's public surface so callers do not need to import ftmo to compute a
FundedNext daily reset.

Multi-model selector
--------------------
FundedNext has four challenge models with **different numeric drawdown
thresholds**:

* Stellar 2-Step: 5% daily / 10% max / 8%+5% targets / 5 min days
* Stellar 1-Step: 3% daily / 6% max / 10% target / 2 min days
* Stellar Lite: 4% daily / 8% max / 8%+4% targets / 5 min days
* Stellar Instant: not encoded (no help-center article)

Because the rule **structure** is the same across models (static
drawdown vs starting balance, max(daily_start_equity, current_balance)
as the daily-loss reference base), per-model differences are encoded as
**separate instances of the same Predicate subclass** with different
``threshold_fraction`` values plus a ``model`` metadata field. This
follows the FTMO pattern (multiple :class:`FtmoProfitTarget` instances
share one class) and does **not** require any ABC change.

The :data:`FUNDEDNEXT_PREDICATES_BY_MODEL` dict exposes per-model tuples;
:data:`FUNDEDNEXT_PREDICATES` aliases the **Stellar 2-Step** entry — the
project's default model, matching the brief's $50k FTMO + $50k FundedNext
Phase-B parallel run.

Constraints
-----------
* No ``MetaTrader5`` import. No broker hostname. No VPS IP.
* All datetimes flowing through predicates MUST be tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from propfarm.rules.ftmo import server_midnight_before
from propfarm.rules.predicates import (
    AccountState,
    CandidateTrade,
    Event,
    Predicate,
    Violation,
)

__all__ = [
    "FIRM_SLUG",
    "FUNDEDNEXT_CONSISTENCY",
    "FUNDEDNEXT_COPY_TRADING",
    "FUNDEDNEXT_HFT",
    "FUNDEDNEXT_HYPERACTIVITY",
    "FUNDEDNEXT_LATENCY_ARB",
    "FUNDEDNEXT_MARTINGALE",
    "FUNDEDNEXT_NEWS_BLACKOUT",
    "FUNDEDNEXT_PREDICATES",
    "FUNDEDNEXT_PREDICATES_BY_MODEL",
    "FUNDEDNEXT_STELLAR_1STEP_DAILY_DD",
    "FUNDEDNEXT_STELLAR_1STEP_MAX_DD",
    "FUNDEDNEXT_STELLAR_1STEP_MIN_TRADING_DAYS",
    "FUNDEDNEXT_STELLAR_1STEP_PREDICATES",
    "FUNDEDNEXT_STELLAR_1STEP_PROFIT_TARGET",
    "FUNDEDNEXT_STELLAR_2STEP_DAILY_DD",
    "FUNDEDNEXT_STELLAR_2STEP_MAX_DD",
    "FUNDEDNEXT_STELLAR_2STEP_MIN_TRADING_DAYS",
    "FUNDEDNEXT_STELLAR_2STEP_PREDICATES",
    "FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1",
    "FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE2",
    "FUNDEDNEXT_STELLAR_LITE_DAILY_DD",
    "FUNDEDNEXT_STELLAR_LITE_MAX_DD",
    "FUNDEDNEXT_STELLAR_LITE_MIN_TRADING_DAYS",
    "FUNDEDNEXT_STELLAR_LITE_PREDICATES",
    "FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE1",
    "FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2",
    "FUNDEDNEXT_TIME_LIMIT",
    "FundedNextConsistencyCheck",
    "FundedNextCopyTradingCheck",
    "FundedNextDailyDrawdown",
    "FundedNextHftCheck",
    "FundedNextHyperactivityCheck",
    "FundedNextLatencyArbCheck",
    "FundedNextMartingaleCheck",
    "FundedNextMaxDrawdown",
    "FundedNextMinTradingDays",
    "FundedNextNewsBlackoutWindow",
    "FundedNextProfitTarget",
    "FundedNextTimeLimit",
    "server_midnight_before",
]


# --------------------------------------------------------------------------- #
# Firm constants
# --------------------------------------------------------------------------- #
FIRM_SLUG: Final[str] = "fundednext"

#: Per-model numeric thresholds. See snapshot file for sources.
_STELLAR_2STEP_DAILY_DD: Final[float] = 0.05
_STELLAR_2STEP_MAX_DD: Final[float] = 0.10
_STELLAR_2STEP_PROFIT_TARGET_PHASE1: Final[float] = 0.08
_STELLAR_2STEP_PROFIT_TARGET_PHASE2: Final[float] = 0.05
_STELLAR_2STEP_MIN_DAYS: Final[int] = 5

_STELLAR_1STEP_DAILY_DD: Final[float] = 0.03
_STELLAR_1STEP_MAX_DD: Final[float] = 0.06
_STELLAR_1STEP_PROFIT_TARGET: Final[float] = 0.10
_STELLAR_1STEP_MIN_DAYS: Final[int] = 2

_STELLAR_LITE_DAILY_DD: Final[float] = 0.04
_STELLAR_LITE_MAX_DD: Final[float] = 0.08
_STELLAR_LITE_PROFIT_TARGET_PHASE1: Final[float] = 0.08
_STELLAR_LITE_PROFIT_TARGET_PHASE2: Final[float] = 0.04
_STELLAR_LITE_MIN_DAYS: Final[int] = 5

#: Copy-trading combined-capital cap (same-owner accounts), USD.
_COPY_TRADING_CAPITAL_THRESHOLD_USD: Final[float] = 300_000.0

#: News blackout half-window seconds (5 minutes pre and post).
_NEWS_BLACKOUT_SECONDS: Final[int] = 300

#: Hyperactivity per-day threshold (trade count).
_HYPERACTIVITY_TRADES_PER_DAY: Final[int] = 200

#: HFT working heuristic — same as the FTMO interpretation since FundedNext
#: publishes no numeric.
_HFT_ORDERS_PER_MIN_THRESHOLD: Final[int] = 5

#: Latency-arb working heuristic.
_LATENCY_ARB_RTT_MS_THRESHOLD: Final[float] = 50.0

#: Consistency working heuristic (FundedNext publishes no numeric;
#: same single-day-share heuristic as FTMO).
_CONSISTENCY_SINGLE_DAY_FRACTION: Final[float] = 0.50


# --------------------------------------------------------------------------- #
# Verbatim ToS quotes — must match docs/firm-tos-snapshots/fundednext-rules-2026-05-12.md
# --------------------------------------------------------------------------- #
# Drawdown — Stellar 2-Step
_QUOTE_STELLAR_2STEP_DAILY_DD: Final[str] = (
    "Your account must not lose more than 5% of the initial balance in a single day."
)
_QUOTE_STELLAR_2STEP_MAX_DD: Final[str] = (
    "Your account must not drop below 90% of its initial balance — "
    "meaning the total loss cannot exceed 10% overall."
)
_QUOTE_STELLAR_2STEP_PROFIT_TARGET: Final[str] = (
    "Phase 1: You must achieve 8% growth on your starting balance. "
    "Phase 2: After completing Phase 1, you must achieve 5% growth in Phase 2."
)
_QUOTE_STELLAR_2STEP_TIME_LIMIT: Final[str] = (
    "There is no time limit for completing Phase 1 or Phase 2 of the Stellar 2-Step Challenge."
)

# Drawdown — Stellar 1-Step
_QUOTE_STELLAR_1STEP_DAILY_DD: Final[str] = (
    "Your account must not lose more than 3% of the initial balance in a single day."
)
_QUOTE_STELLAR_1STEP_MAX_DD: Final[str] = (
    "Your account must not drop below 94% of its initial balance, "
    "meaning the total loss cannot exceed 6% overall."
)
_QUOTE_STELLAR_1STEP_PROFIT_TARGET: Final[str] = (
    "The Stellar 1-Step Challenge requires traders to reach a 10% profit target "
    "to pass the Challenge Phase."
)
_QUOTE_STELLAR_1STEP_TIME_LIMIT: Final[str] = (
    "There is no time restriction to complete the Stellar 1-Step Challenge."
)

# Drawdown — Stellar Lite
_QUOTE_STELLAR_LITE_DAILY_DD: Final[str] = (
    "You may not lose more than 4% of your initial balance in a single day."
)
_QUOTE_STELLAR_LITE_MAX_DD: Final[str] = (
    "Your account balance or equity cannot fall below 92% of your initial balance."
)
_QUOTE_STELLAR_LITE_PROFIT_TARGET: Final[str] = (
    "Phase 1: 8% profit target. Phase 2: 4% profit target."
)

# Minimum trading days (cross-model article)
_QUOTE_MIN_TRADING_DAYS: Final[str] = (
    "Stellar 1-Step: a minimum of 2 trading days. "
    "Stellar 2-Step: a minimum of 5 trading days. "
    "Stellar Lite: a minimum of 5 trading days."
)

# Banned techniques
_QUOTE_HFT: Final[str] = "No, FundedNext does not allow High-Frequency Trading (HFT)."
_QUOTE_LATENCY_ARB: Final[str] = (
    "Latency Trading — exploiting delayed market data or delays in execution."
)
_QUOTE_COPY_TRADING: Final[str] = (
    "Copy trading between multiple FundedNext Challenge Accounts owned by the same "
    "individual is permitted, provided the combined capital does not exceed USD 300,000. "
    "One account must be designated as the Master Account with others functioning as "
    "Slave Accounts."
)
_QUOTE_HYPERACTIVITY: Final[str] = "200 trades or 2,000 server messages in a single day."
_QUOTE_MARTINGALE: Final[str] = (
    "FundedNext's banned-technique catalog does not list martingale by name; "
    "predicate carried for cross-firm symmetry and flagged uncertain."
)

# News
_QUOTE_NEWS: Final[str] = (
    "Trades executed 5 minutes before and 5 minutes after a listed high-impact news event "
    "(a total 10-minute window) are subject to the News Reward Share Rule. "
    "40% of the profit from these profitable trade(s) will be counted toward "
    "the trader's account balance."
)

# Consistency (uncertain — no published numeric)
_QUOTE_CONSISTENCY: Final[str] = (
    "FundedNext does NOT publish a numeric single-day-profit-share threshold "
    "for challenge-phase or funded-stage consistency. The Disciplined Trader "
    "Program references five (5) consecutive successful Performance Reward cycles"
)


# --------------------------------------------------------------------------- #
# Drawdown predicates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextDailyDrawdown(Predicate):
    """FundedNext daily drawdown — model-parametrized.

    FundedNext's daily-loss reference base is **the higher of (a)
    daily_start_equity, (b) intraday peak balance** — i.e.
    ``max(daily_start_equity, current_balance)``. This is documented in the
    help-center daily-loss article and corroborated across all three
    documented models. The predicate threshold is model-specific.

    Confidence ``"high"`` — numeric and explicit per the snapshot.
    """

    #: Model identifier metadata. NOT a field on the ABC.
    model: Literal["stellar_2step", "stellar_1step", "stellar_lite"] = "stellar_2step"
    threshold_fraction: float = _STELLAR_2STEP_DAILY_DD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Return a kill Violation if equity drop > threshold from daily-start reference.

        Reference base is ``max(daily_start_equity, current_balance)``.
        """
        reference = max(state.daily_start_equity, state.current_balance)
        loss_usd = reference - state.current_equity
        threshold_usd = state.account_size * self.threshold_fraction
        if loss_usd > threshold_usd:
            loss_pct = (loss_usd / state.account_size) * 100.0
            threshold_pct = self.threshold_fraction * 100.0
            return self._violation(
                f"daily DD = -{loss_pct:.2f}% from reference (max of "
                f"daily_start_equity {state.daily_start_equity:.2f} / "
                f"current_balance {state.current_balance:.2f}) "
                f"= {reference:.2f}; threshold -{threshold_pct:.2f}% on account_size "
                f"{state.account_size:.2f}"
            )
        return None


@dataclass(frozen=True)
class FundedNextMaxDrawdown(Predicate):
    """FundedNext static max drawdown — model-parametrized.

    Non-trailing: threshold is fixed at ``threshold_fraction`` * starting
    balance. Confidence ``"high"``.
    """

    model: Literal["stellar_2step", "stellar_1step", "stellar_lite"] = "stellar_2step"
    threshold_fraction: float = _STELLAR_2STEP_MAX_DD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Return a kill Violation if equity is down > threshold from starting balance."""
        loss_usd = state.account_size - state.current_equity
        threshold_usd = state.account_size * self.threshold_fraction
        if loss_usd > threshold_usd:
            loss_pct = (loss_usd / state.account_size) * 100.0
            threshold_pct = self.threshold_fraction * 100.0
            return self._violation(
                f"max DD = -{loss_pct:.2f}% from starting balance "
                f"{state.account_size:.2f}; threshold -{threshold_pct:.2f}%"
            )
        return None


# --------------------------------------------------------------------------- #
# Profit-target predicate (completion-gate; emits Achievement)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextProfitTarget(Predicate):
    """FundedNext profit-target predicate — model-parametrized.

    Hitting the target emits an :class:`Achievement` (not Violation), so the
    kill switch never trips on it and the state machine treats it as a phase
    transition. The rule is high-confidence: FundedNext publishes the
    numeric thresholds unambiguously per model, so ``confidence="high"``.
    """

    model: Literal["stellar_2step", "stellar_1step", "stellar_lite"] = "stellar_2step"
    phase: Literal["phase1", "phase2", "single"] = "phase1"
    threshold_fraction: float = _STELLAR_2STEP_PROFIT_TARGET_PHASE1

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """Return an Achievement if equity has reached target from starting balance."""
        gain_usd = state.current_equity - state.account_size
        threshold_usd = state.account_size * self.threshold_fraction
        if gain_usd >= threshold_usd:
            gain_pct = (gain_usd / state.account_size) * 100.0
            threshold_pct = self.threshold_fraction * 100.0
            return self._achievement(
                f"profit target reached: +{gain_pct:.2f}% from starting balance "
                f"{state.account_size:.2f}; target +{threshold_pct:.2f}% "
                f"(model={self.model}, phase={self.phase})",
                kind="profit_target",
            )
        return None


# --------------------------------------------------------------------------- #
# Banned-technique predicates (Phase 0 no-ops; full detection lands in Task 12)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextHftCheck(Predicate):
    """HFT detection — same Phase-0 no-op pattern as FTMO."""

    orders_per_min_threshold: int = _HFT_ORDERS_PER_MIN_THRESHOLD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; sustained-window state lives in Task 12."""
        return None


@dataclass(frozen=True)
class FundedNextLatencyArbCheck(Predicate):
    """Latency-arbitrage detection — Phase-0 no-op."""

    rtt_ms_threshold: float = _LATENCY_ARB_RTT_MS_THRESHOLD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; RTT window lives downstream."""
        return None


@dataclass(frozen=True)
class FundedNextCopyTradingCheck(Predicate):
    """Copy-trading detection.

    Confidence ``"high"`` — FundedNext publishes the USD 300,000 same-owner
    combined-capital threshold and the categorical different-owner ban
    explicitly. Phase-0 evaluation is no-op pending the multi-account
    ledger (Task 12).
    """

    capital_threshold_usd: float = _COPY_TRADING_CAPITAL_THRESHOLD_USD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; multi-account ledger lands in Task 12."""
        return None


@dataclass(frozen=True)
class FundedNextMartingaleCheck(Predicate):
    """Martingale detection — carried for cross-firm symmetry, flagged uncertain.

    FundedNext's published banned-technique catalog does not list martingale
    by name. The predicate exists for cross-firm parity (loader iterates a
    common interface) and never kill-switches.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; not enforced by FundedNext anyway."""
        return None


@dataclass(frozen=True)
class FundedNextHyperactivityCheck(Predicate):
    """Hyperactivity: > 200 trades or > 2000 server messages in a single day.

    Confidence ``"high"`` — FundedNext publishes both numerics.
    Phase-0 evaluation is no-op pending the per-day trade-count and
    message-count counters (Task 12).
    """

    trades_per_day_threshold: int = _HYPERACTIVITY_TRADES_PER_DAY

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; per-day trade/message counters land in Task 12."""
        return None


# --------------------------------------------------------------------------- #
# News blackout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextNewsBlackoutWindow(Predicate):
    """5-minute pre/post window around high-impact news on the funded stage.

    Confidence ``"high"`` on the time window itself (5 min is published).
    News-list filtering is delegated to the caller — out of scope for W4b.
    """

    window_seconds: int = _NEWS_BLACKOUT_SECONDS
    funded_only: bool = True

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; news-list integration lands later."""
        return None


# --------------------------------------------------------------------------- #
# Completion-gate predicates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextMinTradingDays(Predicate):
    """Minimum trading days per phase — completion-gate, not kill.

    Confidence ``"high"`` — numeric. Returns an :class:`Achievement` (not
    Violation) at end-of-phase when the day count reaches ``min_days``.
    No-op in Phase 0 pending the end-of-phase trading-day counter
    (Task 12).
    """

    model: Literal["stellar_2step", "stellar_1step", "stellar_lite"] = "stellar_2step"
    min_days: int = _STELLAR_2STEP_MIN_DAYS

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """No-op in Phase 0; end-of-phase trading-day counter lands in Task 12."""
        return None


@dataclass(frozen=True)
class FundedNextTimeLimit(Predicate):
    """Time-limit predicate.

    As of 2026-05-12 FundedNext publishes **no time limit** on any of
    Stellar 2-Step, Stellar 1-Step, Stellar Lite. Permanent no-op until
    ToS changes. Confidence ``"high"`` — the "no time limit" status is
    itself explicit in each model's help-center article.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Permanent no-op: FundedNext publishes no time limit as of 2026-05-12."""
        return None


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundedNextConsistencyCheck(Predicate):
    """Single-day-profit-share consistency check.

    Confidence ``"uncertain"`` — FundedNext does not publish a numeric
    single-day-profit-share threshold for the challenge or funded stages.
    Working interpretation: 50% (same heuristic as FTMO since both firms
    review case-by-case). Returns warn Violation on breach so the kill
    switch never trips.

    Same evaluation logic as :class:`propfarm.rules.ftmo.FtmoConsistencyCheck`.
    Implemented here rather than re-using the FTMO class to keep the
    per-firm registries clean and to allow the FundedNext predicate's
    ``name`` / ``firm`` / ``tos_quote`` to be FundedNext-specific.
    """

    single_day_fraction: float = _CONSISTENCY_SINGLE_DAY_FRACTION

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Flag any single-day profit > heuristic threshold of total."""
        if not state.cumulative_pnl_by_day:
            return None
        per_day = [pnl for _, pnl in state.cumulative_pnl_by_day]
        total = sum(per_day)
        if total <= 0.0:
            return None
        biggest_day_pnl = max(per_day)
        if biggest_day_pnl > total * self.single_day_fraction:
            share_pct = (biggest_day_pnl / total) * 100.0
            threshold_pct = self.single_day_fraction * 100.0
            return self._violation(
                f"single-day profit share = {share_pct:.2f}% of total profit "
                f"{total:.2f} USD; review threshold {threshold_pct:.2f}%"
            )
        return None


# --------------------------------------------------------------------------- #
# Module-level instances
# --------------------------------------------------------------------------- #
_HIGH: Literal["high"] = "high"
_UNCERTAIN: Literal["uncertain"] = "uncertain"


# Stellar 2-Step ------------------------------------------------------------ #
FUNDEDNEXT_STELLAR_2STEP_DAILY_DD: Final[FundedNextDailyDrawdown] = FundedNextDailyDrawdown(
    name="fundednext_stellar_2step_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_2STEP_DAILY_DD,
    interpretation=(
        "Equity (closed + floating PnL) must not drop more than 5% below the "
        "max of daily_start_equity and current_balance, captured at the most "
        "recent 00:00 GMT+2/+3 server-midnight crossing. Reset daily."
    ),
    model="stellar_2step",
    threshold_fraction=_STELLAR_2STEP_DAILY_DD,
)


FUNDEDNEXT_STELLAR_2STEP_MAX_DD: Final[FundedNextMaxDrawdown] = FundedNextMaxDrawdown(
    name="fundednext_stellar_2step_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_2STEP_MAX_DD,
    interpretation=(
        "Equity must not drop more than 10% below the initial account balance "
        "at any time during the Stellar 2-Step Challenge. NON-trailing: "
        "threshold fixed at -10% of starting balance."
    ),
    model="stellar_2step",
    threshold_fraction=_STELLAR_2STEP_MAX_DD,
)


FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1: Final[FundedNextProfitTarget] = (
    FundedNextProfitTarget(
        name="fundednext_stellar_2step_profit_target_phase1",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_STELLAR_2STEP_PROFIT_TARGET,
        interpretation=(
            "Stellar 2-Step Phase 1 profit target: equity must reach +8% of "
            "starting balance to complete the phase. Emits Achievement on hit."
        ),
        model="stellar_2step",
        phase="phase1",
        threshold_fraction=_STELLAR_2STEP_PROFIT_TARGET_PHASE1,
    )
)


FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE2: Final[FundedNextProfitTarget] = (
    FundedNextProfitTarget(
        name="fundednext_stellar_2step_profit_target_phase2",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_STELLAR_2STEP_PROFIT_TARGET,
        interpretation=(
            "Stellar 2-Step Phase 2 profit target: equity must reach +5% of "
            "starting balance to complete the phase. Emits Achievement on hit."
        ),
        model="stellar_2step",
        phase="phase2",
        threshold_fraction=_STELLAR_2STEP_PROFIT_TARGET_PHASE2,
    )
)


FUNDEDNEXT_STELLAR_2STEP_MIN_TRADING_DAYS: Final[FundedNextMinTradingDays] = (
    FundedNextMinTradingDays(
        name="fundednext_stellar_2step_min_trading_days",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_MIN_TRADING_DAYS,
        interpretation=(
            "Phase completion requires trading on at least 5 distinct calendar "
            "days. End-of-phase trigger; emits Achievement, not Violation."
        ),
        model="stellar_2step",
        min_days=_STELLAR_2STEP_MIN_DAYS,
    )
)


# Stellar 1-Step ------------------------------------------------------------ #
FUNDEDNEXT_STELLAR_1STEP_DAILY_DD: Final[FundedNextDailyDrawdown] = FundedNextDailyDrawdown(
    name="fundednext_stellar_1step_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_1STEP_DAILY_DD,
    interpretation=(
        "Equity must not drop more than 3% below the max of daily_start_equity "
        "and current_balance. Tighter than Stellar 2-Step's 5%."
    ),
    model="stellar_1step",
    threshold_fraction=_STELLAR_1STEP_DAILY_DD,
)


FUNDEDNEXT_STELLAR_1STEP_MAX_DD: Final[FundedNextMaxDrawdown] = FundedNextMaxDrawdown(
    name="fundednext_stellar_1step_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_1STEP_MAX_DD,
    interpretation=(
        "Equity must not drop more than 6% below starting balance. Non-trailing. "
        "Tighter than Stellar 2-Step's 10%."
    ),
    model="stellar_1step",
    threshold_fraction=_STELLAR_1STEP_MAX_DD,
)


FUNDEDNEXT_STELLAR_1STEP_PROFIT_TARGET: Final[FundedNextProfitTarget] = FundedNextProfitTarget(
    name="fundednext_stellar_1step_profit_target",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_1STEP_PROFIT_TARGET,
    interpretation=(
        "Stellar 1-Step single-phase profit target: equity must reach +10% of "
        "starting balance. Emits Achievement on hit."
    ),
    model="stellar_1step",
    phase="single",
    threshold_fraction=_STELLAR_1STEP_PROFIT_TARGET,
)


FUNDEDNEXT_STELLAR_1STEP_MIN_TRADING_DAYS: Final[FundedNextMinTradingDays] = (
    FundedNextMinTradingDays(
        name="fundednext_stellar_1step_min_trading_days",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_MIN_TRADING_DAYS,
        interpretation=(
            "Phase completion requires trading on at least 2 distinct calendar "
            "days. End-of-phase trigger; emits Achievement."
        ),
        model="stellar_1step",
        min_days=_STELLAR_1STEP_MIN_DAYS,
    )
)


# Stellar Lite -------------------------------------------------------------- #
FUNDEDNEXT_STELLAR_LITE_DAILY_DD: Final[FundedNextDailyDrawdown] = FundedNextDailyDrawdown(
    name="fundednext_stellar_lite_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_LITE_DAILY_DD,
    interpretation=(
        "Equity must not drop more than 4% below the max of daily_start_equity and current_balance."
    ),
    model="stellar_lite",
    threshold_fraction=_STELLAR_LITE_DAILY_DD,
)


FUNDEDNEXT_STELLAR_LITE_MAX_DD: Final[FundedNextMaxDrawdown] = FundedNextMaxDrawdown(
    name="fundednext_stellar_lite_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_LITE_MAX_DD,
    interpretation=("Equity must not drop more than 8% below starting balance. Non-trailing."),
    model="stellar_lite",
    threshold_fraction=_STELLAR_LITE_MAX_DD,
)


FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE1: Final[FundedNextProfitTarget] = (
    FundedNextProfitTarget(
        name="fundednext_stellar_lite_profit_target_phase1",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_STELLAR_LITE_PROFIT_TARGET,
        interpretation=(
            "Stellar Lite Phase 1 profit target: equity must reach +8% of "
            "starting balance. Emits Achievement on hit."
        ),
        model="stellar_lite",
        phase="phase1",
        threshold_fraction=_STELLAR_LITE_PROFIT_TARGET_PHASE1,
    )
)


FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2: Final[FundedNextProfitTarget] = (
    FundedNextProfitTarget(
        name="fundednext_stellar_lite_profit_target_phase2",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_STELLAR_LITE_PROFIT_TARGET,
        interpretation=(
            "Stellar Lite Phase 2 profit target: equity must reach +4% of "
            "starting balance. Emits Achievement on hit."
        ),
        model="stellar_lite",
        phase="phase2",
        threshold_fraction=_STELLAR_LITE_PROFIT_TARGET_PHASE2,
    )
)


FUNDEDNEXT_STELLAR_LITE_MIN_TRADING_DAYS: Final[FundedNextMinTradingDays] = (
    FundedNextMinTradingDays(
        name="fundednext_stellar_lite_min_trading_days",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_MIN_TRADING_DAYS,
        interpretation=(
            "Phase completion requires trading on at least 5 distinct calendar "
            "days. End-of-phase trigger; emits Achievement."
        ),
        model="stellar_lite",
        min_days=_STELLAR_LITE_MIN_DAYS,
    )
)


# Cross-model predicates ---------------------------------------------------- #
FUNDEDNEXT_TIME_LIMIT: Final[FundedNextTimeLimit] = FundedNextTimeLimit(
    name="fundednext_time_limit",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_STELLAR_2STEP_TIME_LIMIT,
    interpretation=(
        "FundedNext publishes NO time limit on any of Stellar 2-Step, "
        "Stellar 1-Step, Stellar Lite. Permanent no-op until ToS changes."
    ),
)


FUNDEDNEXT_NEWS_BLACKOUT: Final[FundedNextNewsBlackoutWindow] = FundedNextNewsBlackoutWindow(
    name="fundednext_news_blackout_window",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_NEWS,
    interpretation=(
        "Funded (Master) stage only: 5 minutes pre / 5 minutes post a "
        "high-impact news event triggers a 60% profit forfeiture. Time window "
        "is numerically published (high confidence); news-list integration is "
        "delegated to the caller (out of W4b scope)."
    ),
)


FUNDEDNEXT_HFT: Final[FundedNextHftCheck] = FundedNextHftCheck(
    name="fundednext_hft_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_HFT,
    interpretation=(
        "Working heuristic: > 5 order submissions per 60-second window sustained "
        "over 10 minutes on a single account. FundedNext publishes NO numeric."
    ),
)


FUNDEDNEXT_LATENCY_ARB: Final[FundedNextLatencyArbCheck] = FundedNextLatencyArbCheck(
    name="fundednext_latency_arb_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_LATENCY_ARB,
    interpretation=(
        "Working heuristic: average submit-to-fill RTT < 50 ms across 20 trades. "
        "FundedNext publishes NO numeric."
    ),
)


FUNDEDNEXT_COPY_TRADING: Final[FundedNextCopyTradingCheck] = FundedNextCopyTradingCheck(
    name="fundednext_copy_trading_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_COPY_TRADING,
    interpretation=(
        "Same-owner copy-trading limited to USD 300,000 combined capital; "
        "different-owner copy-trading is categorically prohibited. "
        "Cross-account ledger lands in Task 12."
    ),
)


FUNDEDNEXT_HYPERACTIVITY: Final[FundedNextHyperactivityCheck] = FundedNextHyperactivityCheck(
    name="fundednext_hyperactivity_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_HYPERACTIVITY,
    interpretation=(
        "200 trades or 2,000 server messages in a single day breaches the "
        "hyperactivity rule. Per-day counters land in Task 12."
    ),
)


FUNDEDNEXT_MARTINGALE: Final[FundedNextMartingaleCheck] = FundedNextMartingaleCheck(
    name="fundednext_martingale_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_MARTINGALE,
    interpretation=(
        "FundedNext does not list martingale by name in its banned-technique "
        "catalog; predicate carried for cross-firm symmetry."
    ),
)


FUNDEDNEXT_CONSISTENCY: Final[FundedNextConsistencyCheck] = FundedNextConsistencyCheck(
    name="fundednext_consistency_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_CONSISTENCY,
    interpretation=(
        "Working heuristic: any single trading day with realized profit > 50% "
        "of cumulative realized profit is flagged. FundedNext publishes no "
        "numeric threshold; case-by-case review."
    ),
)


# --------------------------------------------------------------------------- #
# Multi-model predicate registry
# --------------------------------------------------------------------------- #
#: Cross-model predicates (banned techniques, news, time limit, consistency)
#: are shared across all FundedNext models since they apply firm-wide.
_FUNDEDNEXT_CROSS_MODEL_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDEDNEXT_TIME_LIMIT,
    FUNDEDNEXT_NEWS_BLACKOUT,
    FUNDEDNEXT_HFT,
    FUNDEDNEXT_LATENCY_ARB,
    FUNDEDNEXT_COPY_TRADING,
    FUNDEDNEXT_HYPERACTIVITY,
    FUNDEDNEXT_MARTINGALE,
    FUNDEDNEXT_CONSISTENCY,
)


#: Stellar 2-Step predicate tuple. Evaluation-order priority: drawdown first,
#: then profit targets per phase, then min days, then cross-model rules.
FUNDEDNEXT_STELLAR_2STEP_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDEDNEXT_STELLAR_2STEP_DAILY_DD,
    FUNDEDNEXT_STELLAR_2STEP_MAX_DD,
    FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1,
    FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE2,
    FUNDEDNEXT_STELLAR_2STEP_MIN_TRADING_DAYS,
    *_FUNDEDNEXT_CROSS_MODEL_PREDICATES,
)


FUNDEDNEXT_STELLAR_1STEP_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDEDNEXT_STELLAR_1STEP_DAILY_DD,
    FUNDEDNEXT_STELLAR_1STEP_MAX_DD,
    FUNDEDNEXT_STELLAR_1STEP_PROFIT_TARGET,
    FUNDEDNEXT_STELLAR_1STEP_MIN_TRADING_DAYS,
    *_FUNDEDNEXT_CROSS_MODEL_PREDICATES,
)


FUNDEDNEXT_STELLAR_LITE_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDEDNEXT_STELLAR_LITE_DAILY_DD,
    FUNDEDNEXT_STELLAR_LITE_MAX_DD,
    FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE1,
    FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2,
    FUNDEDNEXT_STELLAR_LITE_MIN_TRADING_DAYS,
    *_FUNDEDNEXT_CROSS_MODEL_PREDICATES,
)


#: Per-model predicate registry. Loader pattern: a consumer selects a model
#: key (e.g. ``"stellar_2step"``) and iterates the resulting tuple.
FUNDEDNEXT_PREDICATES_BY_MODEL: Final[dict[str, tuple[Predicate, ...]]] = {
    "stellar_2step": FUNDEDNEXT_STELLAR_2STEP_PREDICATES,
    "stellar_1step": FUNDEDNEXT_STELLAR_1STEP_PREDICATES,
    "stellar_lite": FUNDEDNEXT_STELLAR_LITE_PREDICATES,
}


#: Firm-level default model. **Stellar 2-Step** is the project default per
#: the brief's $50k FTMO + $50k FundedNext Phase-B parallel run (matches the
#: FTMO 5%/10% two-step shape most closely).
FUNDEDNEXT_PREDICATES: Final[tuple[Predicate, ...]] = FUNDEDNEXT_PREDICATES_BY_MODEL[
    "stellar_2step"
]
