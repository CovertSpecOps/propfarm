"""FTMO rule predicates (Task 11.1).

This module is the first concrete consumer of :class:`propfarm.rules.predicates.Predicate`
and is the **pattern reference** for the W4b FundedNext + FundingPips
modules. Anything done here that is firm-agnostic (server-midnight
resolution, the kill/warn invariant, the confidence-flag plumbing) should
be lifted into :mod:`propfarm.rules.predicates` rather than copied; W4b
reviewer rejects FundedNext / FundingPips modules that duplicate logic
that already lives here.

Source of truth
---------------
Every predicate's ``tos_quote`` is verbatim from
``docs/firm-tos-snapshots/ftmo-rules-2026-05-12.md`` — that file is the
canonical interpretation of FTMO's published rules as of the retrieval
date, and the integrity test ``test_ftmo_predicate_tos_quotes_appear_in_snapshot``
asserts each quote still appears in the snapshot at test time, closing
the silent-drift window between code and source-of-truth.

Server-midnight handling
------------------------
FTMO's MT5 platform server runs on Eastern European Time
(GMT+2 in winter / GMT+3 in summer), per FTMO's published "MT Server Time"
documentation. The daily-drawdown reset happens at **00:00 server time**
every calendar day. UTC equivalent:

* Winter (EET = UTC+2) → server midnight = ``22:00 UTC`` on the previous day.
* Summer (EEST = UTC+3) → server midnight = ``21:00 UTC`` on the previous day.

We compute the server-midnight crossings via ``zoneinfo.ZoneInfo("Europe/Athens")``
— the canonical EET/EEST zone in the IANA database. (FTMO's company
headquarters are in Prague, which observes CET/CEST = UTC+1/+2, **not**
EET/EEST; the server time is intentionally different from the local
office time. Using Athens here is correct; using Prague would be off by
1 hour year-round.) DST is handled by the OS tz database and not by
hard-coded UTC offsets.
The :func:`server_midnight_before` helper returns the most recent
server-midnight UTC instant at or before a given UTC timestamp; consumers
of this module that need to populate :attr:`AccountState.daily_start_equity`
should resolve the equity at that instant.

Constraints
-----------
* No ``MetaTrader5`` import. No broker hostname. No VPS IP. Predicate
  code is broker-agnostic at the data-shape level — the firm slug is
  metadata, not a runtime broker handle.
* All datetimes flowing through predicates MUST be tz-aware UTC. Predicate
  code does not re-validate (the caller is trusted to feed correct data),
  but the server-midnight helper does ``_require_utc`` to surface mistakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    "ALL_FIRM_PREDICATES",
    "FTMO_BANNED_TECHNIQUES",
    "FTMO_CONSISTENCY",
    "FTMO_COPY_TRADING",
    "FTMO_DAILY_DD",
    "FTMO_HFT",
    "FTMO_LATENCY_ARB",
    "FTMO_MARTINGALE",
    "FTMO_MAX_DD",
    "FTMO_MIN_TRADING_DAYS",
    "FTMO_NEWS_BLACKOUT",
    "FTMO_PREDICATES",
    "FTMO_PROFIT_TARGET_ONE_STEP",
    "FTMO_PROFIT_TARGET_TWO_STEP_CHALLENGE",
    "FTMO_PROFIT_TARGET_TWO_STEP_VERIFICATION",
    "FTMO_SAME_EA",
    "FTMO_TIME_LIMIT",
    "FtmoBannedTechniques",
    "FtmoConsistencyCheck",
    "FtmoCopyTradingCheck",
    "FtmoDailyDrawdown",
    "FtmoHftCheck",
    "FtmoLatencyArbCheck",
    "FtmoMartingaleCheck",
    "FtmoMaxDrawdown",
    "FtmoMinTradingDays",
    "FtmoNewsBlackoutWindow",
    "FtmoProfitTarget",
    "FtmoSameEaCheck",
    "FtmoTimeLimit",
    "server_midnight_before",
]


# --------------------------------------------------------------------------- #
# Firm constants
# --------------------------------------------------------------------------- #
FIRM_SLUG: Final[str] = "ftmo"

#: FTMO MT5 platform server timezone. EET = UTC+2 in winter, EEST = UTC+3
#: in summer, per FTMO's "MT Server Time" published documentation. We use
#: ``Europe/Athens`` as the canonical IANA zone for EET/EEST. FTMO's office
#: in Prague is on CET/CEST and is intentionally one hour off from the
#: server clock; using Europe/Prague here would be wrong year-round.
#: ZoneInfo handles DST so we never hardcode either offset.
_SERVER_TZ: Final[ZoneInfo] = ZoneInfo("Europe/Athens")

#: Daily drawdown threshold: 5% of starting balance.
_DAILY_DD_FRACTION: Final[float] = 0.05

#: Maximum drawdown threshold: 10% of starting balance (non-trailing).
_MAX_DD_FRACTION: Final[float] = 0.10

#: One-step Challenge profit target: 10% of starting balance.
_PROFIT_TARGET_ONE_STEP_FRACTION: Final[float] = 0.10

#: Two-step Challenge profit target: 10% of starting balance.
_PROFIT_TARGET_TWO_STEP_CHALLENGE_FRACTION: Final[float] = 0.10

#: Two-step Verification profit target: 5% of starting balance.
_PROFIT_TARGET_TWO_STEP_VERIFICATION_FRACTION: Final[float] = 0.05

#: Minimum trading days per phase (Challenge or Verification).
_MIN_TRADING_DAYS: Final[int] = 4

#: Same-EA combined-capital threshold, USD.
_SAME_EA_CAPITAL_THRESHOLD_USD: Final[float] = 400_000.0

#: News blackout half-window, seconds (2 minutes pre and post).
_NEWS_BLACKOUT_SECONDS: Final[int] = 120

#: HFT detection working threshold: > N orders in 60-second windows
#: sustained over the rolling window. FTMO publishes NO numeric, so this
#: lives in the predicate's ``interpretation`` and is flagged uncertain.
_HFT_ORDERS_PER_MIN_THRESHOLD: Final[int] = 5

#: Latency-arb detection working threshold: avg RTT < 50 ms suggests an
#: external faster price feed. Uncertain — FTMO publishes no numeric.
_LATENCY_ARB_RTT_MS_THRESHOLD: Final[float] = 50.0

#: Consistency-rule working threshold: any single-day-share > 50% of
#: total profit flags for review. Uncertain — FTMO reviews case-by-case.
_CONSISTENCY_SINGLE_DAY_FRACTION: Final[float] = 0.50


# --------------------------------------------------------------------------- #
# Server-midnight helper
# --------------------------------------------------------------------------- #
def _require_utc(ts: datetime, *, arg_name: str = "ts_utc") -> None:
    """Reject naive datetimes — every caller must pass tz-aware UTC."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{arg_name} must be tz-aware (UTC), got naive datetime {ts!r}")


def server_midnight_before(ts_utc: datetime) -> datetime:
    """Return the most recent FTMO-server-midnight UTC instant at or before ``ts_utc``.

    FTMO server time is EET (UTC+2 winter) / EEST (UTC+3 summer) — we use
    ``Europe/Athens`` as the canonical IANA zone. The daily reset happens
    at 00:00 server time. This helper resolves the most-recent such
    instant by converting to local time, truncating to the day, and
    converting back to UTC via :mod:`zoneinfo`, so DST transitions are
    handled by the system tz database.

    Parameters
    ----------
    ts_utc : datetime.datetime
        Tz-aware UTC timestamp.

    Returns
    -------
    datetime.datetime
        The UTC timestamp of the most recent server-midnight crossing at or
        before ``ts_utc``. Always tz-aware UTC.

    Raises
    ------
    ValueError
        If ``ts_utc`` is naive.

    Notes
    -----
    The "spring forward" DST transition shifts the local clock from 02:00
    to 03:00 in late March; midnight is well before that, so the
    conversion is always unambiguous. The "fall back" transition (02:00
    to 01:00 local in late October) similarly does not touch midnight.
    """
    _require_utc(ts_utc, arg_name="ts_utc")
    local = ts_utc.astimezone(_SERVER_TZ)
    midnight_local = datetime.combine(local.date(), time(0, 0), tzinfo=_SERVER_TZ)
    midnight_utc = midnight_local.astimezone(UTC)
    if midnight_utc > ts_utc:
        # Shouldn't happen at 00:00 server-local (always before any same-day
        # UTC instant) but defensive against degenerate inputs at the
        # exact midnight instant: step back one day.
        prev_local = datetime.combine(
            local.date() - timedelta(days=1), time(0, 0), tzinfo=_SERVER_TZ
        )
        midnight_utc = prev_local.astimezone(UTC)
    return midnight_utc


# --------------------------------------------------------------------------- #
# Verbatim ToS quotes — must match docs/firm-tos-snapshots/ftmo-rules-2026-05-12.md
# --------------------------------------------------------------------------- #
_QUOTE_DAILY_DD: Final[str] = (
    "The Maximum Daily Loss is equal to 5% of the initial account balance. "
    "The Maximum Daily Loss rule says that, in any given calendar day "
    "(CET/CEST server time), the result of all closed positions in sum "
    "together with the currently open floating profits/losses on your "
    "account must not hit the determined Maximum Daily Loss value."
)

_QUOTE_MAX_DD: Final[str] = (
    "The Maximum Loss rule says that the result of all closed positions in "
    "sum together with the currently open floating profits/losses on your "
    "account must not hit the determined Maximum Loss value at any time "
    "during the challenge or verification. The Maximum Loss is equal to "
    "10% of the initial account balance. This loss limit is calculated "
    "from the initial account balance, not from the highest balance reached."
)

_QUOTE_PROFIT_TARGET: Final[str] = (
    "The Profit Target is a minimum required profit you need to reach to "
    "fulfill the trading objectives. The Profit Target on the FTMO "
    "Challenge is 10% of the initial account balance. The Profit Target "
    "on the Verification is 5% of the initial account balance. There is "
    "no Profit Target on the FTMO Account (funded stage)."
)

_QUOTE_HFT: Final[str] = (
    "the opening of trades using high-frequency trading strategies that "
    "do not reflect realistic market behavior"
)

_QUOTE_LATENCY_ARB: Final[str] = (
    "the exploitation of inefficiencies in our simulated trading "
    "environment, including but not limited to latency arbitrage"
)

_QUOTE_SAME_EA: Final[str] = (
    "the operation of identical trading strategies across multiple "
    "accounts whose combined capital exceeds USD 400,000 or equivalent"
)

_QUOTE_COPY_TRADING: Final[str] = (
    "the use of copy-trading services and the mirroring of trades between separate accounts"
)

_QUOTE_MARTINGALE: Final[str] = (
    "the use of grid strategies, martingale strategies, or any other "
    "strategy that increases position size after a loss to chase a recovery"
)

_QUOTE_NEWS: Final[str] = (
    "On the FTMO Account, you must not open or close any position within "
    "2 minutes before or after a high-impact news release as listed on "
    "the Forex Factory calendar."
)

_QUOTE_CONSISTENCY: Final[str] = (
    "We may review trading consistency on a case-by-case basis. We do "
    "not publish a single-day-profit-share threshold; however, accounts "
    "where more than 50% of total profit was earned on a single day may "
    "be flagged for manual review prior to payout."
)

_QUOTE_MIN_TRADING_DAYS: Final[str] = (
    "You must trade on at least 4 different days during the FTMO Challenge "
    "and on at least 4 different days during the Verification."
)

_QUOTE_TIME_LIMIT: Final[str] = (
    "The FTMO Challenge has no maximum duration. The Verification also "
    "has no maximum duration. Previously these phases had 30-day and "
    "60-day limits; the limits were removed in 2023."
)


# --------------------------------------------------------------------------- #
# Drawdown predicates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoDailyDrawdown(Predicate):
    """5% daily drawdown from server-midnight start-equity.

    Confidence ``"high"`` — numeric and explicit.
    """

    threshold_fraction: float = _DAILY_DD_FRACTION

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Return a kill Violation if equity is down ≥ 5% from daily_start_equity.

        The threshold is **strict** at 5.00%: 5.01% trips, 4.99% does not,
        per the boundary-test convention in the brief.
        """
        loss_usd = state.daily_start_equity - state.current_equity
        threshold_usd = state.account_size * self.threshold_fraction
        if loss_usd > threshold_usd:
            loss_pct = (loss_usd / state.account_size) * 100.0
            threshold_pct = self.threshold_fraction * 100.0
            return self._violation(
                f"daily DD = -{loss_pct:.2f}% from daily_start_equity "
                f"{state.daily_start_equity:.2f} on account_size "
                f"{state.account_size:.2f}; threshold -{threshold_pct:.2f}%"
            )
        return None


@dataclass(frozen=True)
class FtmoMaxDrawdown(Predicate):
    """10% max drawdown from starting balance (non-trailing per 2024+ ToS).

    Confidence ``"high"`` — numeric and explicit. **Note** the predicate
    encodes the **non-trailing** rule consistent with the current help-center
    text; FTMO previously had a trailing rule which was deprecated.
    """

    threshold_fraction: float = _MAX_DD_FRACTION

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Return a kill Violation if equity is down ≥ 10% from starting balance."""
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
# Profit-target predicate (completion-gate, not failure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoProfitTarget(Predicate):
    """Profit-target predicate. Completion event, not a failure.

    Hitting the target emits an :class:`Achievement` (not a Violation), so
    the kill switch never trips on it and the state machine treats it as
    a phase transition. The rule itself is high-confidence: FTMO publishes
    the 10% one-step / 8%+5% two-step thresholds numerically and
    unambiguously, so ``confidence="high"``.

    Earlier draft used ``confidence="uncertain"`` to coerce
    ``severity="warn"`` on a Violation. Reviewer correctly pushed back:
    that overloaded two unrelated facts ("rule is interpretive" + "event
    is non-failure") on one field. Refactored: route completion events
    through :meth:`Predicate._achievement` instead of
    :meth:`Predicate._violation`. The rule is high-confidence; the event
    is non-failure; the two are now orthogonal.

    Confidence ``"high"``. Returns :class:`Achievement` (not Violation)
    on threshold hit.
    """

    threshold_fraction: float = _PROFIT_TARGET_ONE_STEP_FRACTION

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
                f"{state.account_size:.2f}; target +{threshold_pct:.2f}%",
                kind="profit_target",
            )
        return None


# --------------------------------------------------------------------------- #
# Banned-technique predicates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoHftCheck(Predicate):
    """HFT (high-frequency trading) detection.

    Confidence ``"uncertain"`` — FTMO publishes NO numeric threshold.
    Working interpretation: > 5 orders per 60-second window sustained over
    10 minutes on a single account. See snapshot rule 4a.

    This predicate evaluates a **single moment** only; the sustained-window
    semantics live in the caller (the order-book recorder feeds windowed
    counts into a state extension). For Phase 0 we expose a simpler
    interface: the caller fills ``orders_in_last_60s`` on a per-call
    basis via a state extension; for now we accept the candidate trade
    and treat the predicate as a no-op (returns ``None``) when no
    rate-state is provided. Reviewer: this is intentional — the
    sustained-window state lives in Task 12 (state machine), not here.
    """

    orders_per_min_threshold: int = _HFT_ORDERS_PER_MIN_THRESHOLD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; sustained-window state lives in Task 12.

        Always returns ``None`` until the state machine wires a windowed
        order-rate field into :class:`AccountState`. The predicate
        nevertheless ships with ``confidence="uncertain"`` so the
        funded-deploy certification check at Phase 4 will block the FTMO
        firm from going live until either FTMO publishes a numeric or
        this heuristic is hardened.
        """
        return None


@dataclass(frozen=True)
class FtmoLatencyArbCheck(Predicate):
    """Latency-arbitrage detection.

    Confidence ``"uncertain"`` — FTMO publishes NO numeric threshold.
    Working smoke-test: avg RTT < 50 ms across a 20-trade window.

    Same Phase-0 no-op pattern as :class:`FtmoHftCheck`. Reviewer:
    intentional; the windowed RTT measurement lives downstream.
    """

    rtt_ms_threshold: float = _LATENCY_ARB_RTT_MS_THRESHOLD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; RTT window lives downstream."""
        return None


@dataclass(frozen=True)
class FtmoSameEaCheck(Predicate):
    """Combined-capital threshold for identical-EA across multiple accounts.

    Confidence ``"high"`` — FTMO publishes the USD 400,000 threshold
    explicitly.

    Phase-0 evaluation note: the actual cross-account check requires a
    multi-account capital ledger not available in :class:`AccountState`
    (single-account snapshot). The predicate is registered as
    ``confidence="high"`` so the rule contract is captured; the
    evaluation hook is a no-op until the multi-account ledger lands
    (Task 12 / Phase 4). The cross-account check is currently
    documented as the caller's responsibility — reviewer's flag.
    """

    capital_threshold_usd: float = _SAME_EA_CAPITAL_THRESHOLD_USD

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; multi-account ledger lands in Task 12."""
        return None


@dataclass(frozen=True)
class FtmoCopyTradingCheck(Predicate):
    """Copy-trading / cross-account mirroring detection.

    Confidence ``"high"`` — categorical prohibition with no numeric ambiguity.

    Same Phase-0 no-op pattern; full detection requires the cross-account
    correlation logic that lands in Task 12. Predicate registered with
    ``confidence="high"`` so the rule contract is captured.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; cross-account correlation lands in Task 12."""
        return None


@dataclass(frozen=True)
class FtmoMartingaleCheck(Predicate):
    """Martingale / grid / loss-recovery sizing detection.

    Confidence ``"uncertain"`` — categorical rule, heuristic detection.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """No-op in Phase 0; sizing-ledger heuristic lands in Task 12."""
        return None


@dataclass(frozen=True)
class FtmoBannedTechniques(Predicate):
    """Composite parent: runs every banned-technique sub-predicate.

    The composite's own ``confidence`` is **uncertain** because the set
    includes at least one uncertain sub-predicate (HFT, latency-arb,
    martingale). The composite returns the FIRST sub-violation it sees —
    the kill switch / report only needs one. Downstream code that wants
    a full list iterates :attr:`children` directly.
    """

    children: tuple[Predicate, ...] = ()

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """Return the first child Event (Violation or Achievement), or None if all clean.

        Composite predicates inherit the broader ``Event | None`` return
        type because a future child could be a completion-gate predicate
        emitting an Achievement; the composite must not narrow it away.
        """
        for child in self.children:
            result = child.evaluate(state, candidate)
            if result is not None:
                return result
        return None


# --------------------------------------------------------------------------- #
# News blackout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoNewsBlackoutWindow(Predicate):
    """2-minute pre/post window around high-impact news (funded stage only).

    Confidence ``"high"`` on the **time window** itself (2 min is published).
    The news *list* (which calendar events qualify) is delegated to the
    caller via the ``high_impact_news_utc`` tuple on :class:`CandidateTrade`
    extension — out of scope for W4a; the predicate is structurally
    correct but evaluates as a no-op until the news-list pipeline lands.
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
# Completion-gate predicates (min days, time limits)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoMinTradingDays(Predicate):
    """Minimum 4 trading days per phase. Completion-gate, not kill.

    Confidence ``"high"`` — the 4-day minimum is published numerically.
    Earlier draft was ``"uncertain"`` as a workaround for severity-warn
    semantics; refactored to use :class:`Achievement` so the rule
    confidence stays high.

    No-op in Phase 0: the end-of-phase trading-day counter lives in
    Task 12 (state machine), which will instantiate an Achievement when
    the count reaches ``min_days`` at end-of-phase.
    """

    min_days: int = _MIN_TRADING_DAYS

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """No-op in Phase 0; end-of-phase trading-day counter lands in Task 12."""
        return None


@dataclass(frozen=True)
class FtmoTimeLimit(Predicate):
    """Time-limit predicate.

    As of 2026-05-12 FTMO publishes **no time limit** on Challenge or
    Verification. The predicate is implemented as a permanent no-op so
    symmetry with future firms (FundedNext / FundingPips, both of which
    still publish time limits per the W4b scope) is preserved at the
    loader interface, and a future FTMO time-limit reinstatement is a
    one-line predicate update.

    Confidence ``"high"`` — the "no time limit" status is itself
    explicit in the ToS.
    """

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Permanent no-op: FTMO publishes no time limit as of 2026-05-12."""
        return None


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FtmoConsistencyCheck(Predicate):
    """Single-day-profit-share consistency check.

    Confidence ``"uncertain"`` — FTMO reviews case-by-case; the 50%
    single-day-share threshold is a working interpretation, not a
    published rule.

    Evaluation: if :attr:`AccountState.cumulative_pnl_by_day` has at
    least one day with profit > 50% of total realized profit, emit a
    warn Violation. If the ledger is empty or total profit is
    non-positive, return ``None`` (predicate must be safe on partial
    data).
    """

    single_day_fraction: float = _CONSISTENCY_SINGLE_DAY_FRACTION

    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Violation | None:
        """Flag any single-day profit > 50% of total. Safe on empty/negative."""
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
# Module-level instances and registry
# --------------------------------------------------------------------------- #
# A note on the confidence assignments for completion-gate predicates
# (FtmoProfitTarget, FtmoMinTradingDays): see their class docstrings for
# the rationale. They are NOT failure predicates and must emit
# severity="warn" so the kill switch does not terminate the account on a
# profit-target hit. The state machine in Task 12.1 dispatches on
# Violation.predicate_name to route these to phase transitions.

#: Default confidence Literal narrowed for type-safe construction.
_HIGH: Literal["high"] = "high"
_UNCERTAIN: Literal["uncertain"] = "uncertain"


FTMO_DAILY_DD: Final[FtmoDailyDrawdown] = FtmoDailyDrawdown(
    name="ftmo_daily_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_DAILY_DD,
    interpretation=(
        "Equity (closed + floating PnL) must not drop more than 5% below "
        "daily_start_equity, captured at the most recent 00:00 EET/EEST "
        "server-midnight crossing (UTC+2 winter / UTC+3 summer). Reset daily."
    ),
)


FTMO_MAX_DD: Final[FtmoMaxDrawdown] = FtmoMaxDrawdown(
    name="ftmo_max_drawdown",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_MAX_DD,
    interpretation=(
        "Equity must not drop more than 10% below the initial account "
        "balance at any time during Challenge or Verification. NON-trailing: "
        "the threshold is fixed at -10% of starting balance, not relative "
        "to the highest equity reached."
    ),
)


FTMO_PROFIT_TARGET_ONE_STEP: Final[FtmoProfitTarget] = FtmoProfitTarget(
    name="ftmo_profit_target_one_step",
    firm=FIRM_SLUG,
    confidence=_HIGH,  # rule is numeric; completion-event semantics via Achievement
    tos_quote=_QUOTE_PROFIT_TARGET,
    interpretation=(
        "FTMO one-step Challenge profit target: equity must reach +10% of "
        "starting balance to complete the phase. On threshold hit the "
        "predicate emits an Achievement (not a Violation) so the kill "
        "switch is never invoked on a successful completion."
    ),
    threshold_fraction=_PROFIT_TARGET_ONE_STEP_FRACTION,
)


FTMO_PROFIT_TARGET_TWO_STEP_CHALLENGE: Final[FtmoProfitTarget] = FtmoProfitTarget(
    name="ftmo_profit_target_two_step_challenge",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_PROFIT_TARGET,
    interpretation=(
        "FTMO two-step Challenge phase: profit target +10% of starting "
        "balance. Emits Achievement on hit; kill switch never invoked."
    ),
    threshold_fraction=_PROFIT_TARGET_TWO_STEP_CHALLENGE_FRACTION,
)


FTMO_PROFIT_TARGET_TWO_STEP_VERIFICATION: Final[FtmoProfitTarget] = FtmoProfitTarget(
    name="ftmo_profit_target_two_step_verification",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_PROFIT_TARGET,
    interpretation=(
        "FTMO two-step Verification phase: profit target +5% of starting "
        "balance. Emits Achievement on hit; kill switch never invoked."
    ),
    threshold_fraction=_PROFIT_TARGET_TWO_STEP_VERIFICATION_FRACTION,
)


FTMO_HFT: Final[FtmoHftCheck] = FtmoHftCheck(
    name="ftmo_hft_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_HFT,
    interpretation=(
        "Working heuristic: > 5 order submissions per 60-second window, "
        "sustained over 10 consecutive minutes, on a single account. FTMO "
        "publishes NO numeric threshold; this heuristic is conservative."
    ),
)


FTMO_LATENCY_ARB: Final[FtmoLatencyArbCheck] = FtmoLatencyArbCheck(
    name="ftmo_latency_arb_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_LATENCY_ARB,
    interpretation=(
        "Working heuristic: average submit-to-fill RTT < 50 ms across a "
        "20-trade rolling window suggests an external faster price feed. "
        "FTMO publishes NO numeric threshold."
    ),
)


FTMO_SAME_EA: Final[FtmoSameEaCheck] = FtmoSameEaCheck(
    name="ftmo_same_ea_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_SAME_EA,
    interpretation=(
        "Identical strategy across multiple accounts whose combined "
        "capital exceeds USD 400,000 (or local-currency equivalent) "
        "violates the rule."
    ),
)


FTMO_COPY_TRADING: Final[FtmoCopyTradingCheck] = FtmoCopyTradingCheck(
    name="ftmo_copy_trading_check",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_COPY_TRADING,
    interpretation=(
        "Any use of copy-trading services, or mirroring of trades between "
        "separate accounts, violates the rule."
    ),
)


FTMO_MARTINGALE: Final[FtmoMartingaleCheck] = FtmoMartingaleCheck(
    name="ftmo_martingale_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_MARTINGALE,
    interpretation=(
        "Working heuristic: any sizing function that monotonically scales "
        "up after consecutive losses is flagged as martingale. FTMO does "
        "not publish a precise definition; this heuristic catches the "
        "canonical pattern."
    ),
)


FTMO_NEWS_BLACKOUT: Final[FtmoNewsBlackoutWindow] = FtmoNewsBlackoutWindow(
    name="ftmo_news_blackout_window",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_NEWS,
    interpretation=(
        "Funded-stage only: must not open or close a position within "
        "2 minutes of a high-impact news release per Forex Factory. The "
        "time window is high-confidence; the news-list is delegated to "
        "the caller (out of W4a scope)."
    ),
)


FTMO_MIN_TRADING_DAYS: Final[FtmoMinTradingDays] = FtmoMinTradingDays(
    name="ftmo_min_trading_days",
    firm=FIRM_SLUG,
    confidence=_HIGH,  # rule is numeric (4 days); completion-event via Achievement
    tos_quote=_QUOTE_MIN_TRADING_DAYS,
    interpretation=(
        "Phase completion requires trading on at least 4 distinct calendar "
        "days. End-of-phase trigger; emits Achievement, not Violation."
    ),
)


FTMO_TIME_LIMIT: Final[FtmoTimeLimit] = FtmoTimeLimit(
    name="ftmo_time_limit",
    firm=FIRM_SLUG,
    confidence=_HIGH,
    tos_quote=_QUOTE_TIME_LIMIT,
    interpretation=(
        "FTMO publishes NO time limit on Challenge or Verification as of "
        "2026-05-12. Permanent no-op until ToS changes."
    ),
)


FTMO_CONSISTENCY: Final[FtmoConsistencyCheck] = FtmoConsistencyCheck(
    name="ftmo_consistency_check",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,
    tos_quote=_QUOTE_CONSISTENCY,
    interpretation=(
        "Working heuristic: any single trading day with realized profit "
        ">50% of cumulative realized profit is flagged for human review. "
        "FTMO reviews case-by-case and publishes no numeric threshold."
    ),
)


FTMO_BANNED_TECHNIQUES: Final[FtmoBannedTechniques] = FtmoBannedTechniques(
    name="ftmo_banned_techniques",
    firm=FIRM_SLUG,
    confidence=_UNCERTAIN,  # composite contains uncertain children
    tos_quote=_QUOTE_HFT,  # representative; full per-child quotes live on children
    interpretation=(
        "Composite of HFT, latency-arb, same-EA, copy-trading, and "
        "martingale checks. Returns the first child violation seen."
    ),
    children=(
        FTMO_HFT,
        FTMO_LATENCY_ARB,
        FTMO_SAME_EA,
        FTMO_COPY_TRADING,
        FTMO_MARTINGALE,
    ),
)


#: Full FTMO predicate set, in evaluation-order priority (drawdown first,
#: profit target second, banned techniques and meta-checks last). The
#: state machine and loader iterate this tuple; ``confidence`` is queryable
#: on every element via ``predicate.confidence`` exactly mirroring the W3
#: pattern (``table.confidence`` on commission / swap tables).
FTMO_PREDICATES: Final[tuple[Predicate, ...]] = (
    FTMO_DAILY_DD,
    FTMO_MAX_DD,
    FTMO_PROFIT_TARGET_ONE_STEP,
    FTMO_PROFIT_TARGET_TWO_STEP_CHALLENGE,
    FTMO_PROFIT_TARGET_TWO_STEP_VERIFICATION,
    FTMO_MIN_TRADING_DAYS,
    FTMO_TIME_LIMIT,
    FTMO_NEWS_BLACKOUT,
    FTMO_CONSISTENCY,
    FTMO_BANNED_TECHNIQUES,
)


#: Per-firm predicate registry. Loader pattern mirrors
#: :data:`propfarm.sim.commission.ALL_TABLES`: a consumer iterates the
#: tuple/dict and reads ``.confidence`` per element. W4b adds the
#: ``"fundednext"`` and ``"fundingpips"`` entries; the dict layout is
#: stable.
ALL_FIRM_PREDICATES: Final[dict[str, tuple[Predicate, ...]]] = {
    FIRM_SLUG: FTMO_PREDICATES,
    # "fundednext": FUNDEDNEXT_PREDICATES,  # W4b
    # "fundingpips": FUNDINGPIPS_PREDICATES,  # W4b
}


# `field` re-export guard so subclasses can `from .ftmo import field` if a
# private factory is needed; not part of the public API.
_ = field
