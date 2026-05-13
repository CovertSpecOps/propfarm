"""FundingPips rule predicates (Task 11.2 — W4b).

Concrete consumer of :class:`propfarm.rules.predicates.Predicate` covering
the three documented FundingPips challenge models (1-Step, 2-Step,
2-Step Pro). FundingPips Zero and the giveaway 1k Instant account are
**out of scope** — different rule profile, not used by this project.

This module **does not modify the W4a ABC**. The W4a abstractions
(:class:`Predicate`, :class:`Violation`, :class:`Achievement`,
:class:`AccountState`, :class:`OpenPosition`, :class:`CandidateTrade`) are
inherited unchanged. Per-class fields (``model``, ``threshold_fraction``)
are added on FundingPips predicate subclasses for metadata only.

Source of truth
---------------
Every predicate's ``tos_quote`` is verbatim from
``docs/firm-tos-snapshots/fundingpips-rules-2026-05-12.md``. The integrity
test ``test_fundingpips_predicate_tos_quotes_appear_in_snapshot`` asserts
each quote still appears in the snapshot at test time, closing the
silent-drift window.

Server-midnight handling
------------------------
FundingPips's MT5 server runs on **GMT+3 year-round (fixed offset, no
DST)** per the help-center model articles' "00:00 Platform Time (UTC+3)"
language. **Different from FTMO/FundedNext** (which use EET/EEST with DST).
``zoneinfo.ZoneInfo("Etc/GMT-3")`` is the canonical IANA zone for fixed
UTC+3 (note the IANA-sign-inversion convention: ``Etc/GMT-3`` means UTC+3).
The :func:`server_midnight_before` helper here is FundingPips-specific
because the timezone differs; the FTMO helper cannot be reused as-is.

Multi-model selector
--------------------
FundingPips has three challenge models with **different numeric drawdown
thresholds**:

* 1-Step: 3% daily / 6% max / 10% target / 3 min days
* 2-Step: 5% daily / 10% max / 8%-or-10% Phase-I + 5% Phase-II / 3 min days
* 2-Step Pro: 3% daily / 6% max / 6%+6% targets / 1 min day

Same as FundedNext: rule **structure** is the same across models (static
drawdown vs starting balance, max(daily_start_equity, current_balance) as
the reference base), per-model differences encoded as separate Predicate
instances with different ``threshold_fraction`` values plus a ``model``
metadata field. No ABC change.

:data:`FUNDINGPIPS_PREDICATES_BY_MODEL` exposes per-model tuples;
:data:`FUNDINGPIPS_PREDICATES` aliases the **2-Step** entry as the default
— matches the FTMO 5%/10% shape that most strategies are tuned for.

Constraints
-----------
* No ``MetaTrader5`` import. No broker hostname. No VPS IP.
* All datetimes flowing through predicates MUST be tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from propfarm.rules.predicates import (
    AccountState,
    CandidateTrade,
    Event,
    Predicate,
    Violation,
)

__all__ = [
    "FIRM_SLUG",
    "FUNDINGPIPS_1STEP_DAILY_DD",
    "FUNDINGPIPS_1STEP_MAX_DD",
    "FUNDINGPIPS_1STEP_MIN_TRADING_DAYS",
    "FUNDINGPIPS_1STEP_PREDICATES",
    "FUNDINGPIPS_1STEP_PROFIT_TARGET",
    "FUNDINGPIPS_2STEP_DAILY_DD",
    "FUNDINGPIPS_2STEP_MAX_DD",
    "FUNDINGPIPS_2STEP_MIN_TRADING_DAYS",
    "FUNDINGPIPS_2STEP_PREDICATES",
    "FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT",
    "FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT",
    "FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE2",
    "FUNDINGPIPS_2STEP_PRO_DAILY_DD",
    "FUNDINGPIPS_2STEP_PRO_MAX_DD",
    "FUNDINGPIPS_2STEP_PRO_MIN_TRADING_DAYS",
    "FUNDINGPIPS_2STEP_PRO_PREDICATES",
    "FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE1",
    "FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE2",
    "FUNDINGPIPS_CONSISTENCY",
    "FUNDINGPIPS_COPY_TRADING",
    "FUNDINGPIPS_HFT",
    "FUNDINGPIPS_LATENCY_ARB",
    "FUNDINGPIPS_MARTINGALE",
    "FUNDINGPIPS_NEWS_BLACKOUT",
    "FUNDINGPIPS_PREDICATES",
    "FUNDINGPIPS_PREDICATES_BY_MODEL",
    "FUNDINGPIPS_TIME_LIMIT",
    "FundingPipsConsistencyCheck",
    "FundingPipsCopyTradingCheck",
    "FundingPipsDailyDrawdown",
    "FundingPipsHftCheck",
    "FundingPipsLatencyArbCheck",
    "FundingPipsMartingaleCheck",
    "FundingPipsMaxDrawdown",
    "FundingPipsMinTradingDays",
    "FundingPipsNewsBlackoutWindow",
    "FundingPipsProfitTarget",
    "FundingPipsTimeLimit",
    "server_midnight_before",
]


# --------------------------------------------------------------------------- #
# Firm constants
# --------------------------------------------------------------------------- #
FIRM_SLUG: Final[str] = "fundingpips"

#: FundingPips MT5 platform server timezone. Fixed UTC+3 year-round (no DST)
#: per "00:00 Platform Time (UTC+3)" in the model articles. IANA's
#: ``Etc/GMT-3`` is the canonical fixed-offset zone for UTC+3 (the sign is
#: intentionally inverted in the IANA Etc family — Etc/GMT-3 = UTC+3).
_SERVER_TZ: Final[ZoneInfo] = ZoneInfo("Etc/GMT-3")

#: Per-model numeric thresholds. See snapshot file for sources.
_1STEP_DAILY_DD: Final[float] = 0.03
_1STEP_MAX_DD: Final[float] = 0.06
_1STEP_PROFIT_TARGET: Final[float] = 0.10
_1STEP_MIN_DAYS: Final[int] = 3

_2STEP_DAILY_DD: Final[float] = 0.05
_2STEP_MAX_DD: Final[float] = 0.10
_2STEP_PROFIT_TARGET_PHASE1_8PCT: Final[float] = 0.08
_2STEP_PROFIT_TARGET_PHASE1_10PCT: Final[float] = 0.10
_2STEP_PROFIT_TARGET_PHASE2: Final[float] = 0.05
_2STEP_MIN_DAYS: Final[int] = 3

_2STEP_PRO_DAILY_DD: Final[float] = 0.03
_2STEP_PRO_MAX_DD: Final[float] = 0.06
_2STEP_PRO_PROFIT_TARGET: Final[float] = 0.06
_2STEP_PRO_MIN_DAYS: Final[int] = 1

#: 35% single-day-profit-share consistency threshold (Master Account).
_CONSISTENCY_SINGLE_DAY_FRACTION: Final[float] = 0.35

#: News blackout half-window seconds (5 minutes pre and post).
_NEWS_BLACKOUT_SECONDS: Final[int] = 300

#: HFT and latency-arb working heuristics (FundingPips publishes no numeric).
_HFT_ORDERS_PER_MIN_THRESHOLD: Final[int] = 5
_LATENCY_ARB_RTT_MS_THRESHOLD: Final[float] = 50.0


# --------------------------------------------------------------------------- #
# Server-midnight helper (fixed UTC+3, no DST)
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str = "ts_utc") -> None:
    """Reject naive datetimes — every caller must pass tz-aware UTC."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def server_midnight_before(ts_utc: datetime) -> datetime:
    """Return the most recent FundingPips-server-midnight UTC instant.

    FundingPips's MT5 server is fixed UTC+3 year-round (no DST). Server
    midnight is **21:00 UTC** on the previous calendar day, always.

    Parameters
    ----------
    ts_utc : datetime.datetime
        Tz-aware UTC timestamp.

    Returns
    -------
    datetime.datetime
        UTC timestamp of the most recent server-midnight crossing at or
        before ``ts_utc``. Always tz-aware UTC.

    Raises
    ------
    ValueError
        If ``ts_utc`` is naive.
    """
    _require_utc(ts_utc, arg_name="ts_utc")
    local = ts_utc.astimezone(_SERVER_TZ)
    midnight_local = datetime.combine(local.date(), time(0, 0), tzinfo=_SERVER_TZ)
    midnight_utc = midnight_local.astimezone(UTC)
    if midnight_utc > ts_utc:
        prev_local = datetime.combine(
            local.date() - timedelta(days=1), time(0, 0), tzinfo=_SERVER_TZ
        )
        midnight_utc = prev_local.astimezone(UTC)
    return midnight_utc


# --------------------------------------------------------------------------- #
# Verbatim ToS quotes — must match docs/firm-tos-snapshots/fundingpips-rules-2026-05-12.md
# --------------------------------------------------------------------------- #
# 1-Step
_QUOTE_1STEP_DAILY_DD: Final[str] = (
    "Daily Loss Limit: 3% (of the higher value between your daily starting balance or equity)"
)
_QUOTE_1STEP_MAX_DD: Final[str] = "Maximum Loss Limit: 6% (of the initial account size)"
_QUOTE_1STEP_PROFIT_TARGET: Final[str] = "Achieve a 10% profit target during the Student Phase."

# 2-Step
_QUOTE_2STEP_DAILY_DD: Final[str] = (
    "Daily Loss Limit: 5% (of the higher value between your daily starting balance or equity)"
)
_QUOTE_2STEP_MAX_DD: Final[str] = "Maximum Loss Limit: 10% (of the initial account size)"
_QUOTE_2STEP_PROFIT_TARGET: Final[str] = (
    "Option One: Achieve an 8% profit target during the Student Phase. "
    "Option Two: Achieve a 10% profit target during the Student Phase. "
    "Achieve a 5% profit target during the Practitioner Phase."
)

# 2-Step Pro
_QUOTE_2STEP_PRO_DAILY_DD: Final[str] = (
    "Daily Loss Limit: 3% (of the higher value between your daily starting balance or equity)"
)
_QUOTE_2STEP_PRO_MAX_DD: Final[str] = "Maximum Loss Limit: 6% (of the initial account size)"
_QUOTE_2STEP_PRO_PROFIT_TARGET: Final[str] = (
    "Achieve a 6% profit target during the Student Phase. "
    "Achieve a 6% profit target during the Practitioner Phase."
)

# Cross-model
_QUOTE_TIME_LIMIT: Final[str] = "The Daily Loss Limit resets at 00:00 Platform Time (UTC+3)."
_QUOTE_MIN_TRADING_DAYS_1STEP: Final[str] = (
    "Complete a minimum of 3 trading days to pass the evaluation."
)
_QUOTE_MIN_TRADING_DAYS_2STEP: Final[str] = (
    "Complete a minimum of 3 trading days to pass the evaluation."
)
_QUOTE_MIN_TRADING_DAYS_2STEP_PRO: Final[str] = (
    "Complete a minimum of 1 trading day to pass the evaluation"
)
_QUOTE_CONSISTENCY: Final[str] = (
    "A 35% consistency score must be achieved, meaning no single trading day "
    "can account for more than 35% of the total profit."
)
_QUOTE_NEWS: Final[str] = (
    "1 Step, 2 Step & 2 Step Pro (Master Account): cannot open or close positions "
    "within a 10-minute window surrounding a high-impact news event "
    "(5 minutes before and 5 minutes after)."
)
_QUOTE_HFT: Final[str] = "high-frequency trading"
_QUOTE_LATENCY_ARB: Final[str] = "latency arbitrage"
_QUOTE_COPY_TRADING: Final[str] = (
    "You are allowed to copy trades between your own FundingPips accounts "
    "(i.e., accounts registered under the same individual). "
    "Copying trades between FundingPips accounts owned by different users is prohibited."
)
_QUOTE_MARTINGALE: Final[str] = (
    "FundingPips's forbidden-strategies catalog does not list martingale by name. "
    "Predicate carried for cross-firm symmetry and flagged uncertain."
)


# --------------------------------------------------------------------------- #
# Drawdown predicates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundingPipsDailyDrawdown(Predicate):
    """FundingPips daily drawdown — model-parametrized.

    Reference base: ``max(daily_start_equity, current_balance)`` —
    "the higher value between your daily starting balance or equity"
    per the model articles. Confidence ``"high"``.
    """

    model: Literal["1step", "2step", "2step_pro"] = "2step"
    threshold_fraction: float = _2STEP_DAILY_DD

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
class FundingPipsMaxDrawdown(Predicate):
    """FundingPips static max drawdown — model-parametrized. Non-trailing."""

    model: Literal["1step", "2step", "2step_pro"] = "2step"
    threshold_fraction: float = _2STEP_MAX_DD

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
class FundingPipsProfitTarget(Predicate):
    """FundingPips profit-target predicate — model-parametrized.

    Emits :class:`Achievement` on threshold hit (not Violation), so the
    kill switch never trips. Rule is high-confidence: numeric thresholds
    are published.
    """

    model: Literal["1step", "2step", "2step_pro"] = "2step"
    phase: Literal["phase1", "phase2", "single"] = "phase1"
    threshold_fraction: float = _2STEP_PROFIT_TARGET_PHASE1_8PCT

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
# Banned-technique predicates (Phase 0 no-ops)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundingPipsHftCheck(Predicate):
    """HFT / tick-scalping / server-spamming composite — Phase 0 no-op.

    FundingPips's banned-strategy catalog includes high-frequency trading,
    server spamming, tick scalping, and churning-and-burning. All share
    the "sub-second trade-frequency / micro-duration" smoke-test. Bundled
    here.
    """

    orders_per_min_threshold: int = _HFT_ORDERS_PER_MIN_THRESHOLD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; sustained-window state lives in Task 12."""
        return None


@dataclass(frozen=True)
class FundingPipsLatencyArbCheck(Predicate):
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
class FundingPipsCopyTradingCheck(Predicate):
    """Copy-trading detection.

    Confidence ``"high"`` — FundingPips's policy is categorical: same-owner
    copy-trading is allowed (no combined-capital threshold published);
    different-owner copy-trading is prohibited. Phase-0 evaluation is
    no-op pending the cross-account ownership ledger (Task 12).
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; cross-account ownership ledger lands in Task 12."""
        return None


@dataclass(frozen=True)
class FundingPipsMartingaleCheck(Predicate):
    """Martingale detection — carried for cross-firm symmetry, flagged uncertain.

    FundingPips does not list martingale by name in its banned-strategies
    catalog; the predicate exists for cross-firm parity.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; not enforced by FundingPips anyway."""
        return None


# --------------------------------------------------------------------------- #
# News blackout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundingPipsNewsBlackoutWindow(Predicate):
    """5-minute pre/post window around high-impact news on the Master stage.

    Confidence ``"high"`` on the time window (5 min is published).
    News-list filtering delegated to caller — out of W4b scope.
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
class FundingPipsMinTradingDays(Predicate):
    """Minimum trading days per phase — completion-gate, not kill."""

    model: Literal["1step", "2step", "2step_pro"] = "2step"
    min_days: int = _2STEP_MIN_DAYS

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """No-op in Phase 0; end-of-phase trading-day counter lands in Task 12."""
        return None


@dataclass(frozen=True)
class FundingPipsTimeLimit(Predicate):
    """Time-limit predicate. FundingPips publishes no time limit on any model.

    Confidence ``"high"`` — absence is explicit. Permanent no-op until
    ToS changes.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Permanent no-op: FundingPips publishes no time limit as of 2026-05-12."""
        return None


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundingPipsConsistencyCheck(Predicate):
    """35% single-day-profit-share consistency check (Master Account scope).

    Confidence ``"high"`` — FundingPips publishes the 35% threshold
    explicitly. Applies to On-Demand Rewards on the Master (funded)
    account, NOT the Student / Practitioner evaluation phases. Runtime
    layer (Task 12) routes the violation to the payout-cycle flow.

    Returns a kill Violation on breach (severity follows the high
    confidence). The kill switch's payout-aware mode (Task 12.1) can
    downgrade the action to "withhold this cycle's reward" rather than
    terminate the account — that decision lives outside this predicate.
    """

    single_day_fraction: float = _CONSISTENCY_SINGLE_DAY_FRACTION

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Flag any single-day profit > 35% of total. Safe on empty/negative."""
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
                f"{total:.2f} USD; threshold {threshold_pct:.2f}%"
            )
        return None


# --------------------------------------------------------------------------- #
# Module-level instances
# --------------------------------------------------------------------------- #
_HIGH: Literal["high"] = "high"
_UNCERTAIN: Literal["uncertain"] = "uncertain"


# 1-Step -------------------------------------------------------------------- #
FUNDINGPIPS_1STEP_DAILY_DD: Final[FundingPipsDailyDrawdown] = FundingPipsDailyDrawdown(
    name="fundingpips_1step_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_1STEP_DAILY_DD,
    interpretation=(
        "Equity must not drop more than 3% below max(daily_start_equity, "
        "current_balance), captured at the most recent 00:00 UTC+3 "
        "server-midnight crossing. Tighter than 2-Step's 5%."
    ),
    model="1step",
    threshold_fraction=_1STEP_DAILY_DD,
)


FUNDINGPIPS_1STEP_MAX_DD: Final[FundingPipsMaxDrawdown] = FundingPipsMaxDrawdown(
    name="fundingpips_1step_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_1STEP_MAX_DD,
    interpretation=(
        "Equity must not drop more than 6% below starting balance. Non-trailing. "
        "Tighter than 2-Step's 10%."
    ),
    model="1step",
    threshold_fraction=_1STEP_MAX_DD,
)


FUNDINGPIPS_1STEP_PROFIT_TARGET: Final[FundingPipsProfitTarget] = FundingPipsProfitTarget(
    name="fundingpips_1step_profit_target",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_1STEP_PROFIT_TARGET,
    interpretation=(
        "FundingPips 1-Step Student-phase profit target: equity must reach +10% "
        "of starting balance. Emits Achievement on hit."
    ),
    model="1step",
    phase="single",
    threshold_fraction=_1STEP_PROFIT_TARGET,
)


FUNDINGPIPS_1STEP_MIN_TRADING_DAYS: Final[FundingPipsMinTradingDays] = FundingPipsMinTradingDays(
    name="fundingpips_1step_min_trading_days",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_MIN_TRADING_DAYS_1STEP,
    interpretation=(
        "Phase completion requires trading on at least 3 distinct calendar "
        "days. End-of-phase trigger; emits Achievement."
    ),
    model="1step",
    min_days=_1STEP_MIN_DAYS,
)


# 2-Step -------------------------------------------------------------------- #
FUNDINGPIPS_2STEP_DAILY_DD: Final[FundingPipsDailyDrawdown] = FundingPipsDailyDrawdown(
    name="fundingpips_2step_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_2STEP_DAILY_DD,
    interpretation=(
        "Equity must not drop more than 5% below max(daily_start_equity, "
        "current_balance). Identical structure to FTMO's 5% rule but with "
        "the FundingPips reference base."
    ),
    model="2step",
    threshold_fraction=_2STEP_DAILY_DD,
)


FUNDINGPIPS_2STEP_MAX_DD: Final[FundingPipsMaxDrawdown] = FundingPipsMaxDrawdown(
    name="fundingpips_2step_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_2STEP_MAX_DD,
    interpretation=(
        "Equity must not drop more than 10% below starting balance. Non-trailing. "
        "Identical to FTMO's 10% rule."
    ),
    model="2step",
    threshold_fraction=_2STEP_MAX_DD,
)


FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT: Final[FundingPipsProfitTarget] = (
    FundingPipsProfitTarget(
        name="fundingpips_2step_profit_target_phase1_8pct",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_2STEP_PROFIT_TARGET,
        interpretation=(
            "FundingPips 2-Step Phase 1 profit target (8% option): equity must "
            "reach +8% of starting balance. Emits Achievement. Default Phase-I "
            "option for this project."
        ),
        model="2step",
        phase="phase1",
        threshold_fraction=_2STEP_PROFIT_TARGET_PHASE1_8PCT,
    )
)


FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT: Final[FundingPipsProfitTarget] = (
    FundingPipsProfitTarget(
        name="fundingpips_2step_profit_target_phase1_10pct",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_2STEP_PROFIT_TARGET,
        interpretation=(
            "FundingPips 2-Step Phase 1 profit target (10% option): equity must "
            "reach +10% of starting balance. Emits Achievement. Alternate "
            "Phase-I option offered at signup."
        ),
        model="2step",
        phase="phase1",
        threshold_fraction=_2STEP_PROFIT_TARGET_PHASE1_10PCT,
    )
)


FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE2: Final[FundingPipsProfitTarget] = FundingPipsProfitTarget(
    name="fundingpips_2step_profit_target_phase2",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_2STEP_PROFIT_TARGET,
    interpretation=(
        "FundingPips 2-Step Practitioner phase profit target: equity must "
        "reach +5% of starting balance. Emits Achievement on hit."
    ),
    model="2step",
    phase="phase2",
    threshold_fraction=_2STEP_PROFIT_TARGET_PHASE2,
)


FUNDINGPIPS_2STEP_MIN_TRADING_DAYS: Final[FundingPipsMinTradingDays] = FundingPipsMinTradingDays(
    name="fundingpips_2step_min_trading_days",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_MIN_TRADING_DAYS_2STEP,
    interpretation=(
        "Phase completion requires trading on at least 3 distinct calendar "
        "days. End-of-phase trigger; emits Achievement."
    ),
    model="2step",
    min_days=_2STEP_MIN_DAYS,
)


# 2-Step Pro ---------------------------------------------------------------- #
FUNDINGPIPS_2STEP_PRO_DAILY_DD: Final[FundingPipsDailyDrawdown] = FundingPipsDailyDrawdown(
    name="fundingpips_2step_pro_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_2STEP_PRO_DAILY_DD,
    interpretation=(
        "Equity must not drop more than 3% below max(daily_start_equity, "
        "current_balance). Tighter than vanilla 2-Step."
    ),
    model="2step_pro",
    threshold_fraction=_2STEP_PRO_DAILY_DD,
)


FUNDINGPIPS_2STEP_PRO_MAX_DD: Final[FundingPipsMaxDrawdown] = FundingPipsMaxDrawdown(
    name="fundingpips_2step_pro_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_2STEP_PRO_MAX_DD,
    interpretation=("Equity must not drop more than 6% below starting balance. Non-trailing."),
    model="2step_pro",
    threshold_fraction=_2STEP_PRO_MAX_DD,
)


FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE1: Final[FundingPipsProfitTarget] = (
    FundingPipsProfitTarget(
        name="fundingpips_2step_pro_profit_target_phase1",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_2STEP_PRO_PROFIT_TARGET,
        interpretation=(
            "FundingPips 2-Step Pro Student-phase profit target: equity must "
            "reach +6% of starting balance. Emits Achievement on hit."
        ),
        model="2step_pro",
        phase="phase1",
        threshold_fraction=_2STEP_PRO_PROFIT_TARGET,
    )
)


FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE2: Final[FundingPipsProfitTarget] = (
    FundingPipsProfitTarget(
        name="fundingpips_2step_pro_profit_target_phase2",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_2STEP_PRO_PROFIT_TARGET,
        interpretation=(
            "FundingPips 2-Step Pro Practitioner-phase profit target: equity "
            "must reach +6% of starting balance. Emits Achievement on hit."
        ),
        model="2step_pro",
        phase="phase2",
        threshold_fraction=_2STEP_PRO_PROFIT_TARGET,
    )
)


FUNDINGPIPS_2STEP_PRO_MIN_TRADING_DAYS: Final[FundingPipsMinTradingDays] = (
    FundingPipsMinTradingDays(
        name="fundingpips_2step_pro_min_trading_days",
        firm=FIRM_SLUG,
        confidence=_HIGH,
        tos_quote=_QUOTE_MIN_TRADING_DAYS_2STEP_PRO,
        interpretation=(
            "Phase completion requires trading on at least 1 calendar day. "
            "End-of-phase trigger; emits Achievement."
        ),
        model="2step_pro",
        min_days=_2STEP_PRO_MIN_DAYS,
    )
)


# Cross-model predicates ---------------------------------------------------- #
FUNDINGPIPS_TIME_LIMIT: Final[FundingPipsTimeLimit] = FundingPipsTimeLimit(
    name="fundingpips_time_limit",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_TIME_LIMIT,
    interpretation=(
        "FundingPips publishes NO time limit on any of 1-Step / 2-Step / "
        "2-Step Pro. Permanent no-op until ToS changes."
    ),
)


FUNDINGPIPS_NEWS_BLACKOUT: Final[FundingPipsNewsBlackoutWindow] = FundingPipsNewsBlackoutWindow(
    name="fundingpips_news_blackout_window",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_NEWS,
    interpretation=(
        "Master (funded) stage only: 5 minutes pre / 5 minutes post high-impact "
        "news event triggers profit forfeiture on the affected trade. Time window "
        "is numerically published; news-list integration delegated to the caller."
    ),
)


FUNDINGPIPS_HFT: Final[FundingPipsHftCheck] = FundingPipsHftCheck(
    name="fundingpips_hft_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_HFT,
    interpretation=(
        "Working heuristic: > 5 order submissions per 60-second window sustained "
        "over 10 minutes. Bundles HFT, tick scalping, server spamming, and "
        "churning-and-burning since they share the sub-second-frequency profile. "
        "FundingPips publishes NO numeric threshold."
    ),
)


FUNDINGPIPS_LATENCY_ARB: Final[FundingPipsLatencyArbCheck] = FundingPipsLatencyArbCheck(
    name="fundingpips_latency_arb_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_LATENCY_ARB,
    interpretation=(
        "Working heuristic: average submit-to-fill RTT < 50 ms across 20 trades. "
        "FundingPips publishes NO numeric threshold."
    ),
)


FUNDINGPIPS_COPY_TRADING: Final[FundingPipsCopyTradingCheck] = FundingPipsCopyTradingCheck(
    name="fundingpips_copy_trading_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_COPY_TRADING,
    interpretation=(
        "Same-owner copy-trading is allowed (no combined-capital threshold "
        "published); different-owner copy-trading is categorically prohibited. "
        "Cross-account ownership ledger lands in Task 12."
    ),
)


FUNDINGPIPS_MARTINGALE: Final[FundingPipsMartingaleCheck] = FundingPipsMartingaleCheck(
    name="fundingpips_martingale_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_MARTINGALE,
    interpretation=(
        "FundingPips does not list martingale by name; predicate carried for cross-firm symmetry."
    ),
)


FUNDINGPIPS_CONSISTENCY: Final[FundingPipsConsistencyCheck] = FundingPipsConsistencyCheck(
    name="fundingpips_consistency_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_CONSISTENCY,
    interpretation=(
        "35% single-day-profit-share threshold on the Master (funded) account "
        "for On-Demand Rewards. Numerically published; runtime layer (Task 12) "
        "routes the breach to the payout cycle, not account termination."
    ),
)


# --------------------------------------------------------------------------- #
# Multi-model predicate registry
# --------------------------------------------------------------------------- #
_FUNDINGPIPS_CROSS_MODEL_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDINGPIPS_TIME_LIMIT,
    FUNDINGPIPS_NEWS_BLACKOUT,
    FUNDINGPIPS_HFT,
    FUNDINGPIPS_LATENCY_ARB,
    FUNDINGPIPS_COPY_TRADING,
    FUNDINGPIPS_MARTINGALE,
    FUNDINGPIPS_CONSISTENCY,
)


FUNDINGPIPS_1STEP_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDINGPIPS_1STEP_DAILY_DD,
    FUNDINGPIPS_1STEP_MAX_DD,
    FUNDINGPIPS_1STEP_PROFIT_TARGET,
    FUNDINGPIPS_1STEP_MIN_TRADING_DAYS,
    *_FUNDINGPIPS_CROSS_MODEL_PREDICATES,
)


#: 2-Step bundles both Phase-I profit-target options. Loader selects which
#: option the trader configured at signup.
FUNDINGPIPS_2STEP_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDINGPIPS_2STEP_DAILY_DD,
    FUNDINGPIPS_2STEP_MAX_DD,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE2,
    FUNDINGPIPS_2STEP_MIN_TRADING_DAYS,
    *_FUNDINGPIPS_CROSS_MODEL_PREDICATES,
)


FUNDINGPIPS_2STEP_PRO_PREDICATES: Final[tuple[Predicate, ...]] = (
    FUNDINGPIPS_2STEP_PRO_DAILY_DD,
    FUNDINGPIPS_2STEP_PRO_MAX_DD,
    FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE1,
    FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE2,
    FUNDINGPIPS_2STEP_PRO_MIN_TRADING_DAYS,
    *_FUNDINGPIPS_CROSS_MODEL_PREDICATES,
)


#: Per-model predicate registry.
FUNDINGPIPS_PREDICATES_BY_MODEL: Final[dict[str, tuple[Predicate, ...]]] = {
    "1step": FUNDINGPIPS_1STEP_PREDICATES,
    "2step": FUNDINGPIPS_2STEP_PREDICATES,
    "2step_pro": FUNDINGPIPS_2STEP_PRO_PREDICATES,
}


#: Firm-level default model. **2-Step** is the project default — matches the
#: FTMO 5% / 10% drawdown shape that most strategies are tuned for.
FUNDINGPIPS_PREDICATES: Final[tuple[Predicate, ...]] = FUNDINGPIPS_PREDICATES_BY_MODEL["2step"]
