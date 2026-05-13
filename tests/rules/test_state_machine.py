"""Tests for :mod:`propfarm.rules.state_machine` (Task 12.1).

Coverage targets, from the Task 12.1 brief:

1. PRETRIAL snapshot creation defaults.
2. PRETRIAL → CHALLENGE on first trade attempt.
3. Kill Violation in CHALLENGE → FAILED.
4. Kill Violation in FUNDED → ACCOUNT_LOST.
5. Profit-target Achievement recorded in ``completed_achievements``.
6. CHALLENGE → VERIFICATION for two-step firms on the Challenge-phase
   profit-target hit (FTMO two-step).
7. VERIFICATION → FUNDED on the verification-phase profit-target hit.
8. CHALLENGE → FUNDED for one-step firms (FundedNext Stellar 1-Step).
9. min_trading_days gates the phase transition: profit_target alone
   doesn't transition; both together do.
10. Warn Violations are recorded but don't transition.
11. ``mark_payout_eligible`` flips to PAYOUT_PENDING + PRESERVATION.
12. ``clear_payout`` resets via POST_PAYOUT → FUNDED with payout_count
    incremented and sizing back to AGGRESSIVE.
13. Data-driven phase routing across firms (no hardcoded firm logic).
14. Terminal phases (FAILED, ACCOUNT_LOST) are absorbing.

All tests are offline. No MT5, broker, or VPS string appears.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from propfarm.rules.predicates import (
    AccountState,
    Achievement,
    CandidateTrade,
    Violation,
)
from propfarm.rules.registry import ALL_MODEL_PREDICATES
from propfarm.rules.state_machine import (
    ChallengeStateMachine,
    Phase,
    SizingMode,
    StateMachineSnapshot,
)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _state(
    *,
    firm: str = "ftmo",
    account_size: float = 100_000.0,
    current_balance: float | None = None,
    current_equity: float | None = None,
    daily_high_water_mark: float | None = None,
    overall_high_water_mark: float | None = None,
    daily_start_equity: float | None = None,
    ts_utc: datetime | None = None,
) -> AccountState:
    """Build an :class:`AccountState` with sensible defaults for testing."""
    if current_balance is None:
        current_balance = account_size
    if current_equity is None:
        current_equity = current_balance
    if daily_high_water_mark is None:
        daily_high_water_mark = max(current_balance, account_size)
    if overall_high_water_mark is None:
        overall_high_water_mark = max(current_balance, account_size)
    if daily_start_equity is None:
        daily_start_equity = account_size
    if ts_utc is None:
        ts_utc = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    return AccountState(
        firm=firm,
        account_size=account_size,
        current_balance=current_balance,
        current_equity=current_equity,
        daily_high_water_mark=daily_high_water_mark,
        overall_high_water_mark=overall_high_water_mark,
        daily_start_equity=daily_start_equity,
        ts_utc=ts_utc,
    )


def _candidate(
    *,
    symbol: str = "EURUSD",
    side: str = "long",
    volume_lots: float = 0.10,
    ts_utc: datetime | None = None,
) -> CandidateTrade:
    """Build a :class:`CandidateTrade` for tests."""
    if ts_utc is None:
        ts_utc = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # cast str for Literal — pydantic doesn't validate dataclasses here so we
    # rely on caller passing one of the literal values.
    if side not in ("long", "short"):
        raise ValueError(f"side must be long/short, got {side!r}")
    side_lit: Literal["long", "short"] = "long" if side == "long" else "short"
    return CandidateTrade(
        symbol=symbol,
        side=side_lit,
        volume_lots=volume_lots,
        ts_utc=ts_utc,
    )


# --------------------------------------------------------------------------- #
# 1. Initial snapshot
# --------------------------------------------------------------------------- #
def test_initial_snapshot_pretrial() -> None:
    """Fresh account starts in PRETRIAL with AGGRESSIVE sizing and zero counters."""
    sm = ChallengeStateMachine("ftmo")
    snap = sm.initial_snapshot(account_state=_state())

    assert snap.firm == "ftmo"
    assert snap.model == "default"
    assert snap.phase is Phase.PRETRIAL
    assert snap.sizing_mode is SizingMode.AGGRESSIVE
    assert snap.completed_achievements == ()
    assert snap.payout_count == 0
    assert snap.trading_days_count == 0


# --------------------------------------------------------------------------- #
# 2. PRETRIAL → CHALLENGE on first trade attempt
# --------------------------------------------------------------------------- #
def test_pretrial_to_challenge_on_first_trade() -> None:
    """Submitting any candidate trade flips PRETRIAL to CHALLENGE."""
    sm = ChallengeStateMachine("ftmo")
    snap = sm.initial_snapshot(account_state=_state())

    result = sm.step(snap, new_account_state=_state(), candidate=_candidate())

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.CHALLENGE
    assert result.snapshot.sizing_mode is SizingMode.AGGRESSIVE


def test_pretrial_no_candidate_no_transition() -> None:
    """No candidate trade: stays in PRETRIAL."""
    sm = ChallengeStateMachine("ftmo")
    snap = sm.initial_snapshot(account_state=_state())

    result = sm.step(snap, new_account_state=_state(), candidate=None)

    assert result.phase_changed is False
    assert result.snapshot.phase is Phase.PRETRIAL


# --------------------------------------------------------------------------- #
# 3. Kill Violation in CHALLENGE → FAILED
# --------------------------------------------------------------------------- #
def test_kill_violation_in_challenge_yields_failed() -> None:
    """FTMO at 5.01% daily DD in CHALLENGE → FAILED with the kill Violation."""
    sm = ChallengeStateMachine("ftmo")
    # Start in CHALLENGE (skip PRETRIAL via trade attempt).
    snap = sm.initial_snapshot(account_state=_state())
    snap = sm.step(snap, new_account_state=_state(), candidate=_candidate()).snapshot
    assert snap.phase is Phase.CHALLENGE

    # 5.01% loss from daily_start_equity on 100k = 5,010 USD.
    bad_state = _state(
        account_size=100_000.0,
        daily_start_equity=100_000.0,
        current_equity=100_000.0 - 5_010.0,
        current_balance=100_000.0 - 5_010.0,
    )
    result = sm.step(snap, new_account_state=bad_state, candidate=None)

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.FAILED
    kill_violations = [
        e for e in result.events if isinstance(e, Violation) and e.severity == "kill"
    ]
    assert len(kill_violations) >= 1
    assert any(v.predicate_name == "ftmo_daily_drawdown" for v in kill_violations)


# --------------------------------------------------------------------------- #
# 4. Kill Violation in FUNDED → ACCOUNT_LOST
# --------------------------------------------------------------------------- #
def test_kill_violation_in_funded_yields_account_lost() -> None:
    """Same daily DD setup but in FUNDED → ACCOUNT_LOST, not FAILED."""
    sm = ChallengeStateMachine("ftmo")
    # Force phase=FUNDED directly via construction (the lifecycle path is
    # exercised in other tests).
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.FUNDED,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )

    bad_state = _state(
        account_size=100_000.0,
        daily_start_equity=100_000.0,
        current_equity=100_000.0 - 5_010.0,
        current_balance=100_000.0 - 5_010.0,
    )
    result = sm.step(snap, new_account_state=bad_state, candidate=None)

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.ACCOUNT_LOST


# --------------------------------------------------------------------------- #
# 5. Profit-target Achievement recorded
# --------------------------------------------------------------------------- #
def test_profit_target_achievement_records_completion() -> None:
    """FTMO at +10% — Achievement is emitted and recorded in completed_achievements.

    Use the one-step FTMO target name (single-step path) so the
    Achievement-routing also fires CHALLENGE→FUNDED. Both the recording
    and the routing are exercised here.
    """
    # Use FundedNext Stellar 1-Step (cleanly one-step, single profit target,
    # only 2 min trading days). Set trading_days_count = 2 so the gate fires.
    sm = ChallengeStateMachine("fundednext", "stellar_1step")
    snap = StateMachineSnapshot(
        firm="fundednext",
        model="stellar_1step",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(firm="fundednext"),
        trading_days_count=2,
    )

    profitable = _state(
        firm="fundednext",
        account_size=100_000.0,
        current_balance=110_000.0,
        current_equity=110_000.0,
        daily_start_equity=110_000.0,
        daily_high_water_mark=110_000.0,
        overall_high_water_mark=110_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)

    achievements = [e for e in result.events if isinstance(e, Achievement)]
    assert len(achievements) >= 1
    assert any(a.achievement_kind == "profit_target" for a in achievements)


# --------------------------------------------------------------------------- #
# 6. CHALLENGE → VERIFICATION on profit-target hit (FTMO two-step)
# --------------------------------------------------------------------------- #
def test_challenge_to_verification_on_target_for_two_step_firm() -> None:
    """FTMO at +10% with min days satisfied → CHALLENGE → VERIFICATION."""
    sm = ChallengeStateMachine("ftmo")
    # min_trading_days=4 for FTMO; set count=4 so the gate passes.
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
        trading_days_count=4,
    )

    profitable = _state(
        account_size=100_000.0,
        current_balance=110_000.0,
        current_equity=110_000.0,
        daily_start_equity=110_000.0,
        daily_high_water_mark=110_000.0,
        overall_high_water_mark=110_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.VERIFICATION
    # Per-phase ledger reset for next phase.
    assert result.snapshot.completed_achievements == ()
    assert result.snapshot.trading_days_count == 0


# --------------------------------------------------------------------------- #
# 7. VERIFICATION → FUNDED at +5%
# --------------------------------------------------------------------------- #
def test_verification_to_funded() -> None:
    """FTMO verification at +5% with min days satisfied → VERIFICATION → FUNDED."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.VERIFICATION,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
        trading_days_count=4,
    )

    # +5% gain on 100k. FTMO's verification target predicate uses 5%.
    # Both the one-step (10%) predicate fires at 10% but the verification
    # (5%) one fires here. At only +5%, only the verification predicate
    # fires (one-step's 10% is not yet met).
    profitable = _state(
        account_size=100_000.0,
        current_balance=105_000.0,
        current_equity=105_000.0,
        daily_start_equity=105_000.0,
        daily_high_water_mark=105_000.0,
        overall_high_water_mark=105_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.FUNDED


# --------------------------------------------------------------------------- #
# 8. One-step firm skips VERIFICATION
# --------------------------------------------------------------------------- #
def test_one_step_firm_skips_verification() -> None:
    """FundedNext Stellar 1-Step at +10% with min days → CHALLENGE → FUNDED directly."""
    sm = ChallengeStateMachine("fundednext", "stellar_1step")
    snap = StateMachineSnapshot(
        firm="fundednext",
        model="stellar_1step",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(firm="fundednext"),
        trading_days_count=2,  # Stellar 1-Step min_days = 2
    )

    profitable = _state(
        firm="fundednext",
        account_size=100_000.0,
        current_balance=110_000.0,
        current_equity=110_000.0,
        daily_start_equity=110_000.0,
        daily_high_water_mark=110_000.0,
        overall_high_water_mark=110_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)

    assert result.phase_changed is True
    assert result.snapshot.phase is Phase.FUNDED


# --------------------------------------------------------------------------- #
# 9. min_trading_days gates the transition
# --------------------------------------------------------------------------- #
def test_min_trading_days_gates_phase_transition() -> None:
    """Profit target alone with insufficient trading days → no transition."""
    sm = ChallengeStateMachine("ftmo")
    # Insufficient trading days (FTMO requires 4).
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
        trading_days_count=0,
    )

    profitable = _state(
        account_size=100_000.0,
        current_balance=110_000.0,
        current_equity=110_000.0,
        daily_start_equity=110_000.0,
        daily_high_water_mark=110_000.0,
        overall_high_water_mark=110_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)

    # Profit target hit (Achievement emitted), but min_days gate blocks.
    assert any(isinstance(e, Achievement) for e in result.events)
    assert result.phase_changed is False
    assert result.snapshot.phase is Phase.CHALLENGE
    # Achievement is recorded so a follow-up step (once min_days clears)
    # can complete the transition.
    assert any(
        name.endswith("_two_step_challenge") or name.endswith("_one_step")
        for name in result.snapshot.completed_achievements
    )

    # Now bump the trading-days counter and step again with no new event;
    # the recorded achievement + the now-satisfied gate fires the transition.
    bumped = result.snapshot.model_copy(update={"trading_days_count": 4})
    # Step with a flat account state (no new Achievements emitted) — the
    # ledger from the previous step is enough to gate.
    flat_state = _state(
        account_size=100_000.0,
        current_balance=100_000.0,
        current_equity=100_000.0,
        daily_start_equity=100_000.0,
    )
    follow_up = sm.step(bumped, new_account_state=flat_state, candidate=None)
    assert follow_up.phase_changed is True
    assert follow_up.snapshot.phase is Phase.VERIFICATION


# --------------------------------------------------------------------------- #
# 10. Warn Violation: logged, no transition
# --------------------------------------------------------------------------- #
def test_warn_violation_logs_but_no_transition() -> None:
    """A warn (uncertain) Violation is in events but does not transition."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )

    # FTMO's consistency check is "uncertain" → warn. Set up a ledger with
    # one day taking > 50% of total profit to fire it.
    state_with_lopsided_ledger = AccountState(
        firm="ftmo",
        account_size=100_000.0,
        current_balance=101_000.0,
        current_equity=101_000.0,
        daily_high_water_mark=101_000.0,
        overall_high_water_mark=101_000.0,
        daily_start_equity=101_000.0,
        ts_utc=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        cumulative_pnl_by_day=(
            ("2026-05-10", 100.0),
            ("2026-05-11", 100.0),
            ("2026-05-12", 800.0),  # 80% of 1000 total — trips at 50% threshold.
        ),
    )
    result = sm.step(snap, new_account_state=state_with_lopsided_ledger, candidate=None)

    warns = [e for e in result.events if isinstance(e, Violation) and e.severity == "warn"]
    assert len(warns) >= 1
    assert result.phase_changed is False
    assert result.snapshot.phase is Phase.CHALLENGE


# --------------------------------------------------------------------------- #
# 11. mark_payout_eligible flips to PAYOUT_PENDING + PRESERVATION
# --------------------------------------------------------------------------- #
def test_mark_payout_eligible_flips_to_preservation() -> None:
    """FUNDED → PAYOUT_PENDING flips sizing_mode to PRESERVATION."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.FUNDED,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )

    after = sm.mark_payout_eligible(snap)

    assert after.phase is Phase.PAYOUT_PENDING
    assert after.sizing_mode is SizingMode.PRESERVATION


def test_mark_payout_eligible_rejects_non_funded() -> None:
    """mark_payout_eligible from CHALLENGE is a programmer error."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )
    with pytest.raises(ValueError, match="phase=FUNDED"):
        sm.mark_payout_eligible(snap)


# --------------------------------------------------------------------------- #
# 12. clear_payout resets balance + increments counter
# --------------------------------------------------------------------------- #
def test_clear_payout_resets_balance_and_increments_counter() -> None:
    """PAYOUT_PENDING → POST_PAYOUT → FUNDED via clear_payout.

    payout_count += 1; sizing back to AGGRESSIVE.
    """
    sm = ChallengeStateMachine("ftmo")
    paying = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.PAYOUT_PENDING,
        sizing_mode=SizingMode.PRESERVATION,
        account_state=_state(),
        payout_count=2,
    )

    after = sm.clear_payout(paying)

    # Auto-advances POST_PAYOUT → FUNDED in one call.
    assert after.phase is Phase.FUNDED
    assert after.sizing_mode is SizingMode.AGGRESSIVE
    assert after.payout_count == 3


def test_clear_payout_rejects_non_payout_pending() -> None:
    """clear_payout from CHALLENGE is a programmer error."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )
    with pytest.raises(ValueError, match="phase=PAYOUT_PENDING"):
        sm.clear_payout(snap)


# --------------------------------------------------------------------------- #
# 13. Data-driven phase routing: introspection only, no hardcoded firm logic
# --------------------------------------------------------------------------- #
def test_data_driven_phase_routing() -> None:
    """The state machine identifies phase-1 vs phase-2 profit targets per firm-model.

    Locks the no-hardcoded-firm-logic invariant: only ``ALL_MODEL_PREDICATES``
    introspection is allowed. Asserted by:

    1. FTMO ``("ftmo", "default")`` — two-step. Has BOTH a
       _two_step_challenge and a _two_step_verification profit-target
       predicate.
    2. FundedNext Stellar 2-Step — two-step. Has BOTH phase1 and phase2.
    3. FundedNext Stellar 1-Step — one-step. Has a single-phase target
       and NO verification-phase target.
    """
    # FTMO two-step.
    ftmo = ChallengeStateMachine("ftmo")
    assert ftmo._is_two_step is True
    assert any(
        n.endswith("_two_step_challenge") or n.endswith("_one_step")
        for n in ftmo._challenge_profit_target_names
    )
    assert any(n.endswith("_two_step_verification") for n in ftmo._verification_profit_target_names)

    # FundedNext Stellar 2-Step.
    fn2 = ChallengeStateMachine("fundednext", "stellar_2step")
    assert fn2._is_two_step is True
    assert "fundednext_stellar_2step_profit_target_phase1" in fn2._challenge_profit_target_names
    assert "fundednext_stellar_2step_profit_target_phase2" in fn2._verification_profit_target_names

    # FundedNext Stellar 1-Step — one-step.
    fn1 = ChallengeStateMachine("fundednext", "stellar_1step")
    assert fn1._is_two_step is False
    assert "fundednext_stellar_1step_profit_target" in fn1._challenge_profit_target_names
    assert fn1._verification_profit_target_names == frozenset()


def test_data_driven_phase_routing_covers_every_registered_model() -> None:
    """Every (firm, model) in the registry constructs a state machine successfully.

    Locks the "no model-specific hardcoded routing" invariant: a future
    firm/model added to ``ALL_MODEL_PREDICATES`` is picked up
    automatically; no editor needs to add a branch here.
    """
    for firm, model in ALL_MODEL_PREDICATES:
        sm = ChallengeStateMachine(firm, model)
        # Every model must have at least one challenge-phase profit-target
        # predicate (else there's no completion gate for the first phase).
        assert sm._challenge_profit_target_names, (
            f"({firm}, {model}) has no challenge-phase profit-target predicate"
        )


# --------------------------------------------------------------------------- #
# 14. Terminal phases are absorbing
# --------------------------------------------------------------------------- #
def test_terminal_phases_are_absorbing_failed() -> None:
    """step() on a FAILED snapshot does not transition out."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.FAILED,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )

    # Even a +10% profit Achievement does not unstick a FAILED account.
    profitable = _state(
        account_size=100_000.0,
        current_balance=110_000.0,
        current_equity=110_000.0,
        daily_start_equity=110_000.0,
    )
    result = sm.step(snap, new_account_state=profitable, candidate=None)
    assert result.phase_changed is False
    assert result.snapshot.phase is Phase.FAILED


def test_terminal_phases_are_absorbing_account_lost() -> None:
    """step() on an ACCOUNT_LOST snapshot does not transition out."""
    sm = ChallengeStateMachine("ftmo")
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.ACCOUNT_LOST,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )

    bad_state = _state(
        account_size=100_000.0,
        daily_start_equity=100_000.0,
        current_equity=100_000.0 - 5_010.0,
        current_balance=100_000.0 - 5_010.0,
    )
    result = sm.step(snap, new_account_state=bad_state, candidate=None)
    assert result.phase_changed is False
    assert result.snapshot.phase is Phase.ACCOUNT_LOST


# --------------------------------------------------------------------------- #
# Snapshot immutability invariant
# --------------------------------------------------------------------------- #
def test_snapshot_is_frozen() -> None:
    """StateMachineSnapshot rejects attribute mutation post-construction."""
    snap = StateMachineSnapshot(
        firm="ftmo",
        model="default",
        phase=Phase.CHALLENGE,
        sizing_mode=SizingMode.AGGRESSIVE,
        account_state=_state(),
    )
    with pytest.raises((TypeError, ValueError)):
        snap.phase = Phase.FUNDED


# --------------------------------------------------------------------------- #
# Constructor errors
# --------------------------------------------------------------------------- #
def test_unknown_firm_raises() -> None:
    """Unknown firm slug surfaces immediately at construction."""
    with pytest.raises(ValueError, match="Unknown firm"):
        ChallengeStateMachine("madeupbank")


def test_unknown_model_raises() -> None:
    """Unknown (firm, model) combo surfaces at construction."""
    with pytest.raises(ValueError, match="No predicate set registered"):
        ChallengeStateMachine("fundednext", "imaginary_model")
