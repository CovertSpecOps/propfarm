"""Predicate ABC for prop-firm rule predicates (Task 11.1).

This module defines the :class:`Predicate` abstract base class and the three
companion frozen dataclasses that flow through it:

* :class:`AccountState` — a tz-aware snapshot of the account at one instant.
* :class:`OpenPosition` — one currently-open position inside that state.
* :class:`CandidateTrade` — a trade about to be submitted, evaluated against
  the state.
* :class:`Violation` — what a predicate returns when a rule trips.

The confidence flag — cross-pollinated from W3
--------------------------------------------------
Every :class:`Predicate` carries a class-level
``confidence: Literal["high", "uncertain"]`` flag with **the same Literal
type and the same value names** as the W3 cost tables. Specifically:

* :attr:`propfarm.sim.commission.CommissionTable.confidence`
* :attr:`propfarm.sim.swap.SwapTable.confidence`

A value of ``"uncertain"`` means the rule was reconstructed from secondary
sources, or codifies an interpretive ToS clause that lacks a published
numeric threshold, and **must NOT auto-terminate a live account**.

* **High-confidence** predicates: numeric rules (drawdown %, profit-target
  %, time limits, news-blackout time windows, published combined-capital
  thresholds). On breach they emit a :class:`Violation` with
  ``severity="kill"`` — the kill switch trips.
* **Uncertain** predicates: interpretive rules ("trading must reflect
  realistic risk management", undefined HFT thresholds, ambiguous copy-
  trading definitions, "case-by-case" consistency rules). On breach they
  emit a :class:`Violation` with ``severity="warn"`` — the breach is
  logged and surfaced to the daily auto-report, but does **not**
  terminate the account during the challenge or during the Phase A/B
  demo. Phase 4 funded-deploy certification rejects any firm whose
  loaded predicate set contains any ``"uncertain"`` entry; that gate is
  enforced at the loader, not in this module.

The :meth:`Predicate._violation` helper centralizes the severity-from-
confidence mapping so a future predicate subclass cannot bypass it by
hand-building a :class:`Violation` with ``severity="kill"`` while
declaring itself ``confidence="uncertain"``. Reviewer rejects subclasses
that construct :class:`Violation` directly instead of via
``self._violation(...)``.

Loader-pattern symmetry with W3
-------------------------------
The W3 loader pattern is: a consumer iterates
``propfarm.sim.commission.ALL_TABLES["ftmo"]`` (a tuple/dict of
:class:`CommissionTable` instances) and reads ``.confidence`` on each. The
W4 loader pattern is: a consumer iterates
``propfarm.rules.ftmo.FTMO_PREDICATES`` (a tuple of :class:`Predicate`
instances) and reads ``.confidence`` on each. Same field name, same
Literal type, same value names. A single loader interface can therefore
check confidence flags across both costs and rules without specializing.

Constraints
-----------
* This module imports nothing from ``propfarm.bridge``. Predicates are
  broker-agnostic at the data-shape level — the firm slug is metadata,
  not a runtime broker handle.
* All datetimes flowing through :class:`AccountState`, :class:`OpenPosition`,
  and :class:`CandidateTrade` MUST be tz-aware UTC. Naive datetimes are a
  programmer error that we want to surface loudly, not silently coerce.
* Dataclasses are ``frozen=True`` so accidental mutation during predicate
  evaluation cannot corrupt the state passed in by the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

__all__ = [
    "AccountState",
    "Achievement",
    "CandidateTrade",
    "Event",
    "OpenPosition",
    "Predicate",
    "Violation",
]


# --------------------------------------------------------------------------- #
# Data carriers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OpenPosition:
    """One currently-open position inside an :class:`AccountState`.

    Frozen by design so a predicate cannot mutate the caller's state.

    Attributes
    ----------
    symbol : str
        Instrument symbol (e.g. ``"EURUSD"``).
    side : Literal["long", "short"]
        Trade direction. Same Literal as :class:`CandidateTrade.side`.
    volume_lots : float
        Position size in lots. Non-negative; direction is encoded in ``side``.
    open_ts_utc : datetime.datetime
        Tz-aware UTC timestamp of position open.
    open_price : float
        Fill price at open.
    """

    symbol: str
    side: Literal["long", "short"]
    volume_lots: float
    open_ts_utc: datetime
    open_price: float


@dataclass(frozen=True)
class AccountState:
    """A point-in-time snapshot of the trading account. Predicates read this.

    Frozen so a predicate cannot mutate the caller's state. All datetimes
    must be tz-aware UTC; the runtime caller (kill switch / backtester) is
    responsible for that — predicates do not re-validate per call to keep
    the hot-path cheap.

    Attributes
    ----------
    firm : str
        Firm slug, lowercase: ``"ftmo"`` / ``"fundednext"`` / ``"fundingpips"``.
    account_size : float
        Starting balance of the challenge / funded account, USD.
    current_balance : float
        Closed-trade equity (no open-position floating PnL), USD.
    current_equity : float
        ``current_balance`` plus unrealized PnL from open positions, USD.
        This is the field FTMO and most other firms evaluate drawdown
        against; the predicate code reads this, not ``current_balance``.
    daily_high_water_mark : float
        Highest equity observed since the last server-midnight reset.
        Used by predicates that compare against the day's peak (e.g.
        intra-day trailing-DD rules on some firms; FTMO does not use it
        but FundedNext does).
    overall_high_water_mark : float
        All-time-high equity since account open. Used by trailing-Max-DD
        predicates (some firms; FTMO's current rule is non-trailing).
    daily_start_equity : float
        Equity captured at the most recent server-midnight crossing.
        Daily-drawdown predicates compare ``current_equity`` against this.
    ts_utc : datetime.datetime
        Current UTC timestamp this snapshot was taken. Tz-aware.
    open_positions : tuple[OpenPosition, ...]
        Currently-open positions. Tuple (not list) so the dataclass stays
        hashable and accidental mutation is impossible.
    cumulative_pnl_by_day : tuple[tuple[str, float], ...]
        Per-day cumulative PnL ledger, ``((iso-date, pnl_usd), ...)`` sorted
        by date ascending. Used by the consistency-check predicate
        (was-any-single-day-> 50% of total profit?). Empty tuple means the
        consistency check has no data; the predicate returns ``None``
        rather than asserting on zero data, since predicates must be
        callable on any partially-populated snapshot.
    """

    firm: str
    account_size: float
    current_balance: float
    current_equity: float
    daily_high_water_mark: float
    overall_high_water_mark: float
    daily_start_equity: float
    ts_utc: datetime
    open_positions: tuple[OpenPosition, ...] = ()
    cumulative_pnl_by_day: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class CandidateTrade:
    """A trade about to be submitted. Optional input to :meth:`Predicate.evaluate`.

    For pure account-state queries (e.g. "have I tripped daily DD right
    now?") this is :data:`None`. For pre-submission queries (e.g. "would
    submitting this trade trip the news blackout?") the caller passes a
    :class:`CandidateTrade` instance.

    Attributes
    ----------
    symbol : str
        Instrument symbol.
    side : Literal["long", "short"]
        Trade direction.
    volume_lots : float
        Position size, lots. Non-negative.
    ts_utc : datetime.datetime
        Intended submission timestamp, tz-aware UTC. The predicate compares
        this against news-blackout windows, session-open windows, etc.
    """

    symbol: str
    side: Literal["long", "short"]
    volume_lots: float
    ts_utc: datetime


@dataclass(frozen=True)
class Event:
    """Marker base for predicate evaluation outputs.

    A predicate's :meth:`Predicate.evaluate` returns ``Event | None``. The
    runtime / state machine consumer dispatches on the concrete subclass:

    * :class:`Violation` — a rule was breached. The severity field tells
      the kill switch whether to terminate the account.
    * :class:`Achievement` — a non-failure transition fired (e.g. profit
      target hit, minimum trading days reached). The state machine reads
      this as a phase transition (Challenge → Verification, Verification
      → Funded, etc.), **not** as a failure. Never trips the kill switch.

    Both subclasses share the common audit fields (``predicate_name``,
    ``firm``, ``message``, ``tos_quote``). They differ in whether they
    encode a failure (Violation) or a success-completion (Achievement).

    This separation exists because the older shape — overloading
    ``Violation.severity="warn"`` to mean "completion event, don't kill"
    — conflated two unrelated facts on one field: "is this rule
    interpretive?" and "is this event a failure?". A future agent
    reading ``confidence="uncertain"`` on a profit-target predicate
    would misread the rule as fuzzy when in fact the rule is
    crystal-clear (10% of starting balance, explicitly published).
    The reviewer-driven refactor restores ``confidence="high"`` for
    those rules and routes the non-failure semantics through this
    Achievement type.
    """

    predicate_name: str
    firm: str
    message: str
    tos_quote: str


@dataclass(frozen=True)
class Violation(Event):
    """A rule breach.

    The :attr:`severity` field is **derived from the source predicate's
    confidence**:

    * ``confidence="high"`` → ``severity="kill"`` (kill switch trips).
    * ``confidence="uncertain"`` → ``severity="warn"`` (logged, no
      auto-termination; surfaced to the daily auto-report).

    This invariant is enforced in :meth:`Predicate._violation`. Subclasses
    constructing :class:`Violation` directly bypass the invariant and are
    rejected at review time.

    Attributes
    ----------
    severity : Literal["kill", "warn"]
        Derived from confidence by :meth:`Predicate._violation`.
    confidence : Literal["high", "uncertain"]
        Mirrors the source predicate's confidence. Carried on the
        :class:`Violation` so a downstream report can group violations
        by confidence without re-resolving the predicate.
    """

    severity: Literal["kill", "warn"] = "kill"
    confidence: Literal["high", "uncertain"] = "high"


@dataclass(frozen=True)
class Achievement(Event):
    """A non-failure phase-transition event.

    Emitted by completion-gate predicates (profit target, minimum trading
    days, minimum time-in-phase) when their numeric threshold is met.
    The state machine reads this as a phase transition and never trips
    the kill switch on it.

    No ``severity`` field — an Achievement is **not** a breach, not even
    a soft one. No ``confidence`` field either: completion-gate predicates
    encode unambiguous numeric thresholds (10% profit target, 4 trading
    days minimum). If a firm ever publishes an interpretive completion
    rule, its predicate should return a :class:`Violation` with
    ``severity="warn"`` instead, and a follow-up ADR should decide
    whether to extend :class:`Achievement` with a confidence field.

    Attributes
    ----------
    achievement_kind : Literal["profit_target", "min_trading_days", "min_phase_duration"]
        Stable identifier for the achievement type. The state machine
        dispatches on this; new completion-gate kinds extend the Literal.
    """

    achievement_kind: Literal["profit_target", "min_trading_days", "min_phase_duration"] = (
        "profit_target"
    )


# --------------------------------------------------------------------------- #
# Predicate ABC
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Predicate(ABC):
    """Abstract base class for a single firm rule.

    Subclasses MUST:

    1. Override :meth:`evaluate` to return either a :class:`Violation`
       (constructed via :meth:`_violation`) or :data:`None`.
    2. Set the dataclass fields ``name``, ``firm``, ``confidence``,
       ``tos_quote``, and ``interpretation`` (typically as ``field(default=...)``
       on the subclass so each predicate instance carries its own metadata).

    Subclasses MUST NOT construct :class:`Violation` instances directly;
    use :meth:`_violation` so the severity-from-confidence invariant is
    enforced in one place.

    Attributes
    ----------
    name : str
        Stable snake_case identifier, e.g. ``"ftmo_daily_drawdown"``. Used
        by the state machine and audit logs.
    firm : str
        Firm slug: ``"ftmo"`` / ``"fundednext"`` / ``"fundingpips"``.
    confidence : Literal["high", "uncertain"]
        Same Literal type as :attr:`propfarm.sim.commission.CommissionTable.confidence`
        and :attr:`propfarm.sim.swap.SwapTable.confidence`. See module
        docstring for full semantics.
    tos_quote : str
        Verbatim ToS text this predicate enforces. Reviewer asserts this
        text appears as a substring of the on-disk snapshot file (see
        ``test_ftmo_predicate_tos_quotes_appear_in_snapshot``).
    interpretation : str
        For ``"uncertain"`` predicates: how the agent interpreted the
        rule (the working numeric threshold, the heuristic used). For
        ``"high"`` predicates: a one-line restatement of the numeric rule
        in plain English. Read by the daily auto-report for human review.
    """

    name: str
    firm: str
    confidence: Literal["high", "uncertain"]
    tos_quote: str
    interpretation: str

    @abstractmethod
    def evaluate(
        self,
        state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> Event | None:
        """Return an :class:`Event` if the predicate fires, else :data:`None`.

        Parameters
        ----------
        state : AccountState
            Current account snapshot. Tz-aware UTC.
        candidate : CandidateTrade or None
            Trade about to be submitted, or :data:`None` for pure state queries.

        Returns
        -------
        Event or None
            On rule breach: a :class:`Violation` constructed via
            :meth:`_violation` (severity follows confidence).
            On completion-gate hit: an :class:`Achievement` constructed via
            :meth:`_achievement` (no severity; never kills).
            On no event: :data:`None`.

            Implementations MUST use the helpers — bypassing
            :meth:`_violation` allows the severity-from-confidence
            invariant to drift; bypassing :meth:`_achievement` allows a
            completion-gate predicate to mistakenly emit a Violation that
            the kill switch acts on.
        """

    def _violation(self, message: str) -> Violation:
        """Construct a :class:`Violation` with severity derived from confidence.

        This is the **only** way subclasses should construct breach events.
        It enforces ``confidence="high" → severity="kill"`` and
        ``confidence="uncertain" → severity="warn"`` in one place, so a
        future agent cannot accidentally encode an uncertain rule as a
        kill predicate by typo.

        Parameters
        ----------
        message : str
            Human-readable summary including the relevant numbers.

        Returns
        -------
        Violation
            With ``predicate_name``, ``firm``, ``tos_quote``, and
            ``confidence`` populated from ``self``, ``severity`` derived
            from ``self.confidence``.
        """
        severity: Literal["kill", "warn"] = "kill" if self.confidence == "high" else "warn"
        return Violation(
            predicate_name=self.name,
            firm=self.firm,
            message=message,
            tos_quote=self.tos_quote,
            severity=severity,
            confidence=self.confidence,
        )

    def _achievement(
        self,
        message: str,
        *,
        kind: Literal["profit_target", "min_trading_days", "min_phase_duration"],
    ) -> Achievement:
        """Construct an :class:`Achievement` for a completion-gate predicate.

        This is the **only** way subclasses should construct non-failure
        events. Using ``_violation`` for a completion event would emit a
        Violation that the kill switch could act on; using ``_achievement``
        routes the event through the state-machine-dispatch path instead.

        Completion-gate predicates carry ``confidence="high"`` because
        their thresholds are numeric and unambiguous; the confidence
        field is NOT propagated onto the Achievement (an Achievement
        without confidence is by design — see the :class:`Achievement`
        docstring).

        Parameters
        ----------
        message : str
            Human-readable summary including the relevant numbers.
        kind : Literal[...]
            Stable identifier for the achievement type.
        """
        return Achievement(
            predicate_name=self.name,
            firm=self.firm,
            message=message,
            tos_quote=self.tos_quote,
            achievement_kind=kind,
        )


# --------------------------------------------------------------------------- #
# Internal helper re-exported under ``__all__`` only if a use case appears.
# Currently a private alias so subclasses can `from .predicates import field`
# without re-importing dataclasses.
# --------------------------------------------------------------------------- #
_field = field  # re-exported privately for subclass convenience
