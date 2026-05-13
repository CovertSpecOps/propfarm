"""Challenge state machine (Task 12.1).

This module assembles the W4 predicates from
:mod:`propfarm.rules.registry.ALL_MODEL_PREDICATES` into a runnable state
machine that drives a single prop-firm account through its lifecycle:

    PRETRIAL → CHALLENGE → VERIFICATION → FUNDED
                                          ↓ ↑
                                  PAYOUT_PENDING ↔ POST_PAYOUT
                                       (back to FUNDED on reset)

Plus the two terminal phases:

* ``FAILED`` — a kill :class:`Violation` fired during CHALLENGE or VERIFICATION.
* ``ACCOUNT_LOST`` — a kill :class:`Violation` fired during FUNDED.

Data-driven phase routing
-------------------------
Reviewer-mandated invariant: **no hardcoded per-firm logic**. The phase
routing introspects the ``ALL_MODEL_PREDICATES[(firm, model)]`` tuple at
construction time and classifies each :class:`Achievement`-emitting
predicate by the phase it gates. Two introspection signals are used:

1. The optional ``phase`` attribute on FundedNext / FundingPips profit-
   target predicates: ``"phase1"`` / ``"phase2"`` / ``"single"``.
2. The predicate ``name`` field's stable suffixes for FTMO, which does
   not carry a ``phase`` attribute: ``_one_step``, ``_two_step_challenge``,
   ``_two_step_verification``. These suffixes are part of the W4a
   contract (asserted by the FTMO test suite) and serve as the
   classification protocol.

A firm is treated as two-step **iff** its predicate set contains at
least one Achievement-emitting predicate classified as
:attr:`_AchievementPhase.VERIFICATION`. Otherwise it is one-step
(CHALLENGE goes directly to FUNDED).

The min_trading_days gating is also data-driven: the snapshot carries
a ``trading_days_count`` counter. A phase transition fires only when
**both** the relevant profit-target Achievement is observed in the
event stream **and** ``trading_days_count >= min_days`` for the
firm-model's :class:`propfarm.rules.predicates.Predicate` that has a
``min_days`` attribute. If no such predicate exists for the firm-model,
the gate is trivially satisfied.

Sizing mode (per ADR-0001)
--------------------------
* :attr:`SizingMode.AGGRESSIVE` is the default in CHALLENGE, VERIFICATION,
  and FUNDED.
* :attr:`SizingMode.PRESERVATION` engages in PAYOUT_PENDING (the kill
  switch's payout-aware mode). Reverts to AGGRESSIVE on the
  POST_PAYOUT → FUNDED reset.

The state machine exposes the mode as a flag on the snapshot. The
risk-layer (Phase 2) reads it and applies firm-specific multipliers.
This module does NOT enforce sizing.

Frozen-snapshot semantics
-------------------------
:class:`StateMachineSnapshot` is a frozen pydantic ``BaseModel``. Every
:meth:`ChallengeStateMachine.step` call returns a NEW snapshot — the
caller's snapshot is never mutated. The same holds for
:meth:`mark_payout_eligible` and :meth:`clear_payout`. This mirrors
the W4a predicate-input frozen-dataclass design and is enforced by
pydantic's ``frozen=True`` config.

Constraints
-----------
* mypy strict.
* No MT5 / broker host / VPS string.
* Predicate ABC is NOT modified. AccountState is NOT modified. All
  lifecycle state (phase, sizing mode, completed achievements, payout
  counter, trading-days counter) lives on the state-machine snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from propfarm.rules.predicates import (
    AccountState,
    Achievement,
    CandidateTrade,
    Event,
    Predicate,
    Violation,
)
from propfarm.rules.registry import ALL_MODEL_PREDICATES

__all__ = [
    "ChallengeStateMachine",
    "Phase",
    "SizingMode",
    "StateMachineSnapshot",
    "TransitionResult",
]


# --------------------------------------------------------------------------- #
# Phase / sizing enums
# --------------------------------------------------------------------------- #
class Phase(StrEnum):
    """Lifecycle phases of a single prop-firm account.

    The ordering reflects the forward-only progression in pre-funded
    phases; FUNDED is the steady state, and the two terminal phases
    (``FAILED`` / ``ACCOUNT_LOST``) absorb further transitions.

    ``POST_PAYOUT`` is a transient phase: :meth:`ChallengeStateMachine.clear_payout`
    automatically returns the account to FUNDED while emitting it once
    so callers can observe the cycle reset.
    """

    PRETRIAL = "pretrial"
    CHALLENGE = "challenge"
    VERIFICATION = "verification"
    FUNDED = "funded"
    PAYOUT_PENDING = "payout_pending"
    POST_PAYOUT = "post_payout"
    FAILED = "failed"
    ACCOUNT_LOST = "account_lost"


class SizingMode(StrEnum):
    """Position-sizing posture flag, consumed by the Phase-2 risk layer.

    The state machine flips this between :attr:`AGGRESSIVE` and
    :attr:`PRESERVATION` based on lifecycle phase per ADR-0001. The
    actual multiplier (e.g. 0.5x in PRESERVATION) is not encoded here;
    that's the risk-layer's job.
    """

    AGGRESSIVE = "aggressive"
    PRESERVATION = "preservation"


# Terminal phases — no transitions out of these (besides clear_payout, which
# does NOT apply to FAILED / ACCOUNT_LOST since those are post-failure).
_TERMINAL_PHASES: Final[frozenset[Phase]] = frozenset({Phase.FAILED, Phase.ACCOUNT_LOST})


# --------------------------------------------------------------------------- #
# Achievement-phase classification (data-driven)
# --------------------------------------------------------------------------- #
class _AchievementPhase(StrEnum):
    """Which phase an Achievement-emitting predicate gates.

    Internal helper used only by the state machine for routing.
    """

    CHALLENGE = "challenge"
    VERIFICATION = "verification"


def _classify_profit_target(pred: Predicate) -> _AchievementPhase | None:
    """Classify a profit-target predicate by the phase it gates.

    Returns ``None`` if the predicate is not an Achievement-emitting
    profit-target predicate, otherwise the phase it gates.

    Data-driven: introspects the optional ``phase`` attribute first
    (FundedNext / FundingPips convention), falls back to ``name``
    suffix matching for FTMO (whose profit-target class does not carry
    a ``phase`` attribute).
    """
    # FundedNext / FundingPips: explicit phase attribute.
    phase_attr = getattr(pred, "phase", None)
    if phase_attr is not None:
        if phase_attr in ("phase1", "single"):
            return _AchievementPhase.CHALLENGE
        if phase_attr == "phase2":
            return _AchievementPhase.VERIFICATION
        return None

    # FTMO: name-suffix protocol.
    name = pred.name
    if name.endswith("_two_step_verification"):
        return _AchievementPhase.VERIFICATION
    if name.endswith("_two_step_challenge") or name.endswith("_one_step"):
        return _AchievementPhase.CHALLENGE
    return None


def _is_min_trading_days_predicate(pred: Predicate) -> bool:
    """Return True iff ``pred`` is a min-trading-days completion-gate predicate.

    Detected by the presence of a ``min_days`` attribute — the W4
    contract for min-trading-days predicates across all firms.
    """
    return hasattr(pred, "min_days")


def _is_profit_target_predicate(pred: Predicate) -> bool:
    """Return True iff ``pred`` is an Achievement-emitting profit-target predicate.

    Detected by the result of :func:`_classify_profit_target`.
    """
    return _classify_profit_target(pred) is not None


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class StateMachineSnapshot(BaseModel):
    """Immutable snapshot of state-machine state at one instant.

    Every :meth:`ChallengeStateMachine.step` call returns a NEW snapshot;
    the caller's snapshot is never mutated. The snapshot carries all
    lifecycle state that is NOT on :class:`AccountState`:

    * :attr:`phase` — current lifecycle phase.
    * :attr:`sizing_mode` — current sizing posture (AGGRESSIVE / PRESERVATION).
    * :attr:`account_state` — the latest :class:`AccountState` observed.
    * :attr:`completed_achievements` — names of profit-target /
      min-trading-days Achievements observed so far in the CURRENT phase.
      Cleared on phase transition so the next phase's gates are re-checked.
    * :attr:`payout_count` — how many payout cycles have completed.
    * :attr:`trading_days_count` — distinct trading days observed in
      the current phase. Used to gate min_trading_days completion. The
      caller increments this externally (the state machine does not
      auto-detect day rollover — that's a Phase-1 concern).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    firm: str
    model: str
    phase: Phase
    sizing_mode: SizingMode
    account_state: AccountState
    completed_achievements: tuple[str, ...] = ()
    payout_count: int = 0
    trading_days_count: int = 0


class TransitionResult(BaseModel):
    """Result of one :meth:`ChallengeStateMachine.step` call.

    Attributes
    ----------
    snapshot : StateMachineSnapshot
        The new snapshot. Always present, even when no phase transition
        occurred (the embedded ``account_state`` will have been updated).
    events : tuple[Event, ...]
        All :class:`propfarm.rules.predicates.Event` instances emitted by
        the predicate evaluations on this step, in evaluation order.
        Includes both :class:`propfarm.rules.predicates.Violation` and
        :class:`propfarm.rules.predicates.Achievement` instances.
    phase_changed : bool
        ``True`` if ``snapshot.phase != input_snapshot.phase``. Convenience
        flag — equivalent to comparing the two snapshots manually.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    snapshot: StateMachineSnapshot
    events: tuple[Event, ...]
    phase_changed: bool


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
class ChallengeStateMachine:
    """Predicate-driven lifecycle state machine for a single prop-firm account.

    Construction loads the firm-model's predicate set from
    :data:`propfarm.rules.registry.ALL_MODEL_PREDICATES` and pre-computes
    the phase-routing tables (which Achievement-emitting predicate names
    gate CHALLENGE→? and VERIFICATION→FUNDED) by introspecting the
    predicate set. The pre-computation runs once per state-machine
    instance; :meth:`step` then dispatches O(predicates) per call.

    The state machine is **stateless across step()s** — the caller is
    responsible for threading the :class:`StateMachineSnapshot` between
    calls. This mirrors the W3 / W4 immutable-input pattern and makes
    the state machine safe to share across accounts / threads.
    """

    def __init__(self, firm: str, model: str | None = None) -> None:
        """Initialize for a specific firm and model.

        Parameters
        ----------
        firm : str
            Firm slug, lowercase: ``"ftmo"`` / ``"fundednext"`` /
            ``"fundingpips"``.
        model : str or None
            Model key for the firm. If ``None``, defaults to the firm's
            registered default model (``"default"`` for FTMO,
            ``"stellar_2step"`` for FundedNext, ``"2step"`` for FundingPips).
        """
        resolved_model = model if model is not None else self._default_model_for(firm)
        key = (firm, resolved_model)
        if key not in ALL_MODEL_PREDICATES:
            raise ValueError(
                f"No predicate set registered for firm={firm!r}, model={resolved_model!r}. "
                f"Known keys: {sorted(ALL_MODEL_PREDICATES.keys())}"
            )
        self.firm: Final[str] = firm
        self.model: Final[str] = resolved_model
        self.predicates: Final[tuple[Predicate, ...]] = ALL_MODEL_PREDICATES[key]

        # Pre-compute phase-routing tables once per instance.
        challenge_targets: list[str] = []
        verification_targets: list[str] = []
        min_days_predicate: Predicate | None = None
        for pred in self.predicates:
            phase = _classify_profit_target(pred)
            if phase is _AchievementPhase.CHALLENGE:
                challenge_targets.append(pred.name)
            elif phase is _AchievementPhase.VERIFICATION:
                verification_targets.append(pred.name)
            if _is_min_trading_days_predicate(pred):
                # Per firm/model there's at most one min_trading_days predicate
                # (verified for FTMO, FundedNext, FundingPips). If a future
                # firm splits min_days per phase, the routing here will need
                # extending; for now the single-predicate assumption holds.
                min_days_predicate = pred

        self._challenge_profit_target_names: Final[frozenset[str]] = frozenset(challenge_targets)
        self._verification_profit_target_names: Final[frozenset[str]] = frozenset(
            verification_targets
        )
        self._min_days_predicate: Final[Predicate | None] = min_days_predicate
        # Firm is two-step iff it has any verification-phase profit-target
        # predicate. Otherwise CHALLENGE goes directly to FUNDED.
        self._is_two_step: Final[bool] = len(verification_targets) > 0

    @staticmethod
    def _default_model_for(firm: str) -> str:
        """Map a firm slug to its default model key.

        Mirrors :data:`propfarm.rules.registry.ALL_FIRM_PREDICATES`:

        * ``ftmo`` → ``"default"`` (FTMO ships a single rule set).
        * ``fundednext`` → ``"stellar_2step"`` (project default).
        * ``fundingpips`` → ``"2step"`` (FTMO-shape default).
        """
        defaults = {
            "ftmo": "default",
            "fundednext": "stellar_2step",
            "fundingpips": "2step",
        }
        if firm not in defaults:
            raise ValueError(f"Unknown firm slug {firm!r}; known firms: {sorted(defaults.keys())}")
        return defaults[firm]

    # ----------------------------------------------------------------- #
    # Snapshot constructors and payout helpers
    # ----------------------------------------------------------------- #
    def initial_snapshot(self, *, account_state: AccountState) -> StateMachineSnapshot:
        """Construct the starting snapshot for a fresh account.

        The fresh snapshot starts in :attr:`Phase.PRETRIAL` with
        :attr:`SizingMode.AGGRESSIVE`, no recorded achievements, zero
        payouts, and zero trading-day count.
        """
        return StateMachineSnapshot(
            firm=self.firm,
            model=self.model,
            phase=Phase.PRETRIAL,
            sizing_mode=SizingMode.AGGRESSIVE,
            account_state=account_state,
            completed_achievements=(),
            payout_count=0,
            trading_days_count=0,
        )

    def mark_payout_eligible(self, snapshot: StateMachineSnapshot) -> StateMachineSnapshot:
        """Flip a FUNDED snapshot to PAYOUT_PENDING + PRESERVATION.

        Per ADR-0001, the moment the account is payout-eligible the
        sizing layer switches to preservation mode to avoid losing the
        cycle's reward to a daily DD on the last day. Returns a new
        snapshot; the input is not mutated.

        Raises
        ------
        ValueError
            If ``snapshot.phase`` is not :attr:`Phase.FUNDED`. The kill
            switch should never have requested a payout on a pre-funded
            or terminated account; surfacing the call site bug here is
            preferable to silently no-op'ing.
        """
        if snapshot.phase is not Phase.FUNDED:
            raise ValueError(
                f"mark_payout_eligible requires phase=FUNDED; got phase={snapshot.phase}"
            )
        return snapshot.model_copy(
            update={"phase": Phase.PAYOUT_PENDING, "sizing_mode": SizingMode.PRESERVATION}
        )

    def clear_payout(self, snapshot: StateMachineSnapshot) -> StateMachineSnapshot:
        """Complete a payout cycle: PAYOUT_PENDING → POST_PAYOUT → FUNDED.

        The POST_PAYOUT phase is transient. This helper auto-advances
        through it to FUNDED while bumping :attr:`payout_count` and
        resetting :attr:`sizing_mode` back to AGGRESSIVE. The returned
        snapshot is in FUNDED.

        The caller is expected to have separately reset the account
        balance to ``account_size`` (the firm has paid out the profit
        and the simulated next-cycle starts at the headline balance);
        this method does not modify the embedded ``account_state``.

        Raises
        ------
        ValueError
            If ``snapshot.phase`` is not PAYOUT_PENDING.
        """
        if snapshot.phase is not Phase.PAYOUT_PENDING:
            raise ValueError(
                f"clear_payout requires phase=PAYOUT_PENDING; got phase={snapshot.phase}"
            )
        return snapshot.model_copy(
            update={
                "phase": Phase.FUNDED,
                "sizing_mode": SizingMode.AGGRESSIVE,
                "payout_count": snapshot.payout_count + 1,
            }
        )

    # ----------------------------------------------------------------- #
    # The main step()
    # ----------------------------------------------------------------- #
    def step(
        self,
        snapshot: StateMachineSnapshot,
        *,
        new_account_state: AccountState,
        candidate: CandidateTrade | None = None,
    ) -> TransitionResult:
        """Advance the state machine by one observation.

        Evaluates every predicate against ``new_account_state`` and
        ``candidate``. Routes the resulting :class:`Event` set:

        * A kill :class:`Violation` (``severity="kill"``) → terminal
          phase. FAILED if we were in CHALLENGE / VERIFICATION;
          ACCOUNT_LOST if we were in FUNDED / PAYOUT_PENDING. Warn
          Violations are recorded in :attr:`TransitionResult.events`
          but do NOT trigger a transition.
        * An :class:`Achievement` whose name is in the routing table for
          the current phase → check the gate (profit_target +
          min_trading_days both satisfied) and transition if so.
        * Trading from PRETRIAL (``candidate is not None``) → CHALLENGE.

        Terminal phases (FAILED / ACCOUNT_LOST) are absorbing: ``step``
        does not transition out of them. Predicate evaluations are
        still performed and surfaced via ``events`` so the caller can
        log post-mortem state, but the phase does not change.
        """
        new_snapshot = snapshot.model_copy(update={"account_state": new_account_state})

        # Evaluate every predicate; collect all events for the caller's audit log.
        events: list[Event] = []
        for pred in self.predicates:
            result = pred.evaluate(new_account_state, candidate)
            if result is not None:
                events.append(result)

        # Terminal phases absorb further transitions.
        if new_snapshot.phase in _TERMINAL_PHASES:
            return TransitionResult(
                snapshot=new_snapshot, events=tuple(events), phase_changed=False
            )

        # 1. Kill Violation → terminal phase.
        kill_violation = self._first_kill_violation(events)
        if kill_violation is not None:
            terminal = self._terminal_phase_for(new_snapshot.phase)
            new_snapshot = new_snapshot.model_copy(update={"phase": terminal})
            return TransitionResult(snapshot=new_snapshot, events=tuple(events), phase_changed=True)

        # 2. PRETRIAL + trade attempt → CHALLENGE.
        if new_snapshot.phase is Phase.PRETRIAL and candidate is not None:
            new_snapshot = new_snapshot.model_copy(
                update={"phase": Phase.CHALLENGE, "completed_achievements": ()}
            )
            return TransitionResult(snapshot=new_snapshot, events=tuple(events), phase_changed=True)

        # 3. Achievement-driven phase transitions. Append new achievements
        # to the per-phase ledger so a later step() (potentially without
        # a fresh profit-target event) can still complete the transition
        # once min_trading_days is also satisfied.
        achievement_names_this_step = tuple(
            e.predicate_name for e in events if isinstance(e, Achievement)
        )
        if achievement_names_this_step:
            merged_names = tuple(
                dict.fromkeys((*new_snapshot.completed_achievements, *achievement_names_this_step))
            )
            new_snapshot = new_snapshot.model_copy(update={"completed_achievements": merged_names})

        transitioned = self._try_achievement_transition(new_snapshot)
        if transitioned is not None:
            return TransitionResult(snapshot=transitioned, events=tuple(events), phase_changed=True)

        return TransitionResult(snapshot=new_snapshot, events=tuple(events), phase_changed=False)

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    @staticmethod
    def _first_kill_violation(events: Iterable[Event]) -> Violation | None:
        """Return the first kill-severity Violation in ``events``, or ``None``.

        Warn-severity violations are logged but do not terminate the
        account — see :mod:`propfarm.rules.predicates` for the
        confidence-from-severity invariant.
        """
        for event in events:
            if isinstance(event, Violation) and event.severity == "kill":
                return event
        return None

    @staticmethod
    def _terminal_phase_for(current_phase: Phase) -> Phase:
        """Map the current non-terminal phase to its terminal failure phase.

        * CHALLENGE / VERIFICATION → :attr:`Phase.FAILED`.
        * FUNDED / PAYOUT_PENDING / POST_PAYOUT → :attr:`Phase.ACCOUNT_LOST`.
        * PRETRIAL → :attr:`Phase.FAILED` (a kill before trading starts
          is still a failed challenge; this edge case is unlikely but
          surfaces the same way).
        """
        if current_phase in (Phase.FUNDED, Phase.PAYOUT_PENDING, Phase.POST_PAYOUT):
            return Phase.ACCOUNT_LOST
        return Phase.FAILED

    def _try_achievement_transition(
        self, snapshot: StateMachineSnapshot
    ) -> StateMachineSnapshot | None:
        """Return the post-transition snapshot if a phase transition fires.

        Inspects :attr:`StateMachineSnapshot.completed_achievements` against
        the pre-computed phase-routing tables and the
        :attr:`StateMachineSnapshot.trading_days_count` against the
        firm-model's min_trading_days threshold. Returns ``None`` if
        no transition fires.

        The min-days gate is satisfied when:

        * The firm-model has no min_trading_days predicate, OR
        * ``trading_days_count >= predicate.min_days``.

        This means the test for "profit_target alone doesn't transition
        if min_trading_days is also required" is enforced even when the
        predicate's own ``evaluate()`` is a no-op (the Phase-0 default).
        The state machine carries the counter; the predicate carries
        the threshold.
        """
        achievements = frozenset(snapshot.completed_achievements)
        if not self._min_days_gate_satisfied(snapshot):
            return None

        # CHALLENGE: needs at least one challenge-phase profit-target hit.
        if snapshot.phase is Phase.CHALLENGE:
            if achievements & self._challenge_profit_target_names:
                next_phase = Phase.VERIFICATION if self._is_two_step else Phase.FUNDED
                return snapshot.model_copy(
                    update={
                        "phase": next_phase,
                        # Reset the per-phase ledger so the next phase's gates
                        # are re-checked. Trading-day count also resets so
                        # the next phase's min_days is enforced separately.
                        "completed_achievements": (),
                        "trading_days_count": 0,
                    }
                )
            return None

        # VERIFICATION: needs the verification-phase profit-target hit.
        if snapshot.phase is Phase.VERIFICATION:
            if achievements & self._verification_profit_target_names:
                return snapshot.model_copy(
                    update={
                        "phase": Phase.FUNDED,
                        "completed_achievements": (),
                        "trading_days_count": 0,
                    }
                )
            return None

        return None

    def _min_days_gate_satisfied(self, snapshot: StateMachineSnapshot) -> bool:
        """Return True iff the firm-model's min_trading_days gate is satisfied.

        Trivially True if the firm-model has no min_trading_days
        predicate; otherwise compares the snapshot's
        :attr:`trading_days_count` against the predicate's ``min_days``
        threshold.
        """
        pred = self._min_days_predicate
        if pred is None:
            return True
        min_days = getattr(pred, "min_days", 0)
        return snapshot.trading_days_count >= min_days
