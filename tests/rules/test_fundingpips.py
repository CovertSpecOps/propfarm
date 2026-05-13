"""Tests for ``propfarm.rules.fundingpips`` — FundingPips rule predicates (W4b).

Mirrors ``test_ftmo.py`` and ``test_fundednext.py``. FundingPips uses a
**different server timezone** (fixed UTC+3, no DST) than FTMO/FundedNext,
so the server-midnight tests verify that 21:00-UTC offset year-round.

Every test is offline. No MT5, broker, or VPS strings appear.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from propfarm.rules.fundingpips import (
    FUNDINGPIPS_1STEP_DAILY_DD,
    FUNDINGPIPS_1STEP_MAX_DD,
    FUNDINGPIPS_1STEP_PREDICATES,
    FUNDINGPIPS_1STEP_PROFIT_TARGET,
    FUNDINGPIPS_2STEP_DAILY_DD,
    FUNDINGPIPS_2STEP_MAX_DD,
    FUNDINGPIPS_2STEP_PREDICATES,
    FUNDINGPIPS_2STEP_PRO_DAILY_DD,
    FUNDINGPIPS_2STEP_PRO_MAX_DD,
    FUNDINGPIPS_2STEP_PRO_PREDICATES,
    FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE1,
    FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE2,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT,
    FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE2,
    FUNDINGPIPS_CONSISTENCY,
    FUNDINGPIPS_COPY_TRADING,
    FUNDINGPIPS_HFT,
    FUNDINGPIPS_LATENCY_ARB,
    FUNDINGPIPS_MARTINGALE,
    FUNDINGPIPS_NEWS_BLACKOUT,
    FUNDINGPIPS_PREDICATES,
    FUNDINGPIPS_PREDICATES_BY_MODEL,
    FUNDINGPIPS_TIME_LIMIT,
    server_midnight_before,
)
from propfarm.rules.predicates import (
    AccountState,
    Achievement,
    Predicate,
    Violation,
)

# --------------------------------------------------------------------------- #
# Snapshot file path
# --------------------------------------------------------------------------- #
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH: Path = (
    _REPO_ROOT / "docs" / "firm-tos-snapshots" / "fundingpips-rules-2026-05-12.md"
)


def _state(
    *,
    account_size: float = 100_000.0,
    current_balance: float | None = None,
    current_equity: float | None = None,
    daily_high_water_mark: float | None = None,
    overall_high_water_mark: float | None = None,
    daily_start_equity: float | None = None,
    ts_utc: datetime | None = None,
    cumulative_pnl_by_day: tuple[tuple[str, float], ...] = (),
) -> AccountState:
    """Build an :class:`AccountState` with sensible defaults for tests."""
    if current_balance is None:
        current_balance = account_size
    if current_equity is None:
        current_equity = current_balance
    if daily_start_equity is None:
        daily_start_equity = current_equity
    if daily_high_water_mark is None:
        daily_high_water_mark = max(current_equity, daily_start_equity)
    if overall_high_water_mark is None:
        overall_high_water_mark = max(daily_high_water_mark, account_size)
    if ts_utc is None:
        ts_utc = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    return AccountState(
        firm="fundingpips",
        account_size=account_size,
        current_balance=current_balance,
        current_equity=current_equity,
        daily_high_water_mark=daily_high_water_mark,
        overall_high_water_mark=overall_high_water_mark,
        daily_start_equity=daily_start_equity,
        ts_utc=ts_utc,
        cumulative_pnl_by_day=cumulative_pnl_by_day,
    )


# --------------------------------------------------------------------------- #
# Boundary tests: Daily DD
# --------------------------------------------------------------------------- #
class TestDailyDDBoundary:
    """3% / 5% / 3% — 1-Step / 2-Step / 2-Step Pro."""

    def test_2step_violation_at_5_01_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_990.0
        )
        result = FUNDINGPIPS_2STEP_DAILY_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_2step_no_violation_at_4_99_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=95_010.0
        )
        assert FUNDINGPIPS_2STEP_DAILY_DD.evaluate(state) is None

    def test_2step_no_violation_exactly_at_5_00_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=95_000.0
        )
        assert FUNDINGPIPS_2STEP_DAILY_DD.evaluate(state) is None

    def test_1step_violation_at_3_01_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=96_990.0
        )
        result = FUNDINGPIPS_1STEP_DAILY_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_1step_no_violation_at_2_99_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=97_010.0
        )
        assert FUNDINGPIPS_1STEP_DAILY_DD.evaluate(state) is None

    def test_2step_pro_violation_at_3_01_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=96_990.0
        )
        result = FUNDINGPIPS_2STEP_PRO_DAILY_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_2step_pro_no_violation_at_2_99_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=97_010.0
        )
        assert FUNDINGPIPS_2STEP_PRO_DAILY_DD.evaluate(state) is None

    def test_reference_base_is_max_of_equity_and_balance(self) -> None:
        """Daily-loss reference base is max(daily_start_equity, current_balance)."""
        # current_balance $103k → floor = $103k - $5k = $98k. Equity $97k → -6% from ref.
        state = _state(
            daily_start_equity=100_000.0, current_balance=103_000.0, current_equity=97_000.0
        )
        result = FUNDINGPIPS_2STEP_DAILY_DD.evaluate(state)
        assert result is not None and result.severity == "kill"


# --------------------------------------------------------------------------- #
# Boundary tests: Max DD
# --------------------------------------------------------------------------- #
class TestMaxDDBoundary:
    """6% / 10% / 6% — 1-Step / 2-Step / 2-Step Pro."""

    def test_2step_violation_at_10_01_percent(self) -> None:
        state = _state(current_equity=89_990.0)
        result = FUNDINGPIPS_2STEP_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_2step_no_violation_at_9_99_percent(self) -> None:
        state = _state(current_equity=90_010.0)
        assert FUNDINGPIPS_2STEP_MAX_DD.evaluate(state) is None

    def test_1step_violation_at_6_01_percent(self) -> None:
        state = _state(current_equity=93_990.0)
        result = FUNDINGPIPS_1STEP_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_2step_pro_violation_at_6_01_percent(self) -> None:
        state = _state(current_equity=93_990.0)
        result = FUNDINGPIPS_2STEP_PRO_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_max_dd_non_trailing_does_not_trip_on_equity_below_peak(self) -> None:
        state = _state(current_equity=95_000.0, overall_high_water_mark=115_000.0)
        assert FUNDINGPIPS_2STEP_MAX_DD.evaluate(state) is None


# --------------------------------------------------------------------------- #
# Boundary tests: Profit targets
# --------------------------------------------------------------------------- #
class TestProfitTargetBoundary:
    """Targets: 10% (1-Step), 8%/10%/5% (2-Step), 6%/6% (2-Step Pro)."""

    def test_1step_at_10_percent(self) -> None:
        state = _state(current_equity=110_000.0)
        result = FUNDINGPIPS_1STEP_PROFIT_TARGET.evaluate(state)
        assert isinstance(result, Achievement)
        assert result.achievement_kind == "profit_target"
        assert not isinstance(result, Violation)

    def test_1step_below_10_percent_no_event(self) -> None:
        state = _state(current_equity=109_990.0)
        assert FUNDINGPIPS_1STEP_PROFIT_TARGET.evaluate(state) is None

    def test_2step_phase1_8pct_at_8_percent(self) -> None:
        state = _state(current_equity=108_000.0)
        result = FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT.evaluate(state)
        assert isinstance(result, Achievement)

    def test_2step_phase1_10pct_at_10_percent(self) -> None:
        state = _state(current_equity=110_000.0)
        result = FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT.evaluate(state)
        assert isinstance(result, Achievement)

    def test_2step_phase1_10pct_below_10_percent_no_event(self) -> None:
        state = _state(current_equity=109_990.0)
        assert FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_10PCT.evaluate(state) is None

    def test_2step_phase2_at_5_percent(self) -> None:
        state = _state(current_equity=105_000.0)
        result = FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE2.evaluate(state)
        assert isinstance(result, Achievement)

    def test_2step_pro_phase1_at_6_percent(self) -> None:
        state = _state(current_equity=106_000.0)
        result = FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE1.evaluate(state)
        assert isinstance(result, Achievement)

    def test_2step_pro_phase2_at_6_percent(self) -> None:
        state = _state(current_equity=106_000.0)
        result = FUNDINGPIPS_2STEP_PRO_PROFIT_TARGET_PHASE2.evaluate(state)
        assert isinstance(result, Achievement)


# --------------------------------------------------------------------------- #
# Confidence-driven behavior
# --------------------------------------------------------------------------- #
class TestConfidenceBehavior:
    """Severity follows confidence on every FundingPips predicate."""

    def test_high_confidence_violation_has_kill_severity(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_000.0
        )
        violation = FUNDINGPIPS_2STEP_DAILY_DD.evaluate(state)
        assert violation is not None
        assert FUNDINGPIPS_2STEP_DAILY_DD.confidence == "high"
        assert violation.severity == "kill"

    def test_consistency_violation_is_kill_severity(self) -> None:
        """FundingPips consistency rule IS published numerically (35% high)
        — not warn like FTMO/FundedNext."""
        # 800/200/100 → day1 = 800/1100 = 72.7% > 35%.
        ledger = (("2026-06-01", 800.0), ("2026-06-02", 200.0), ("2026-06-03", 100.0))
        state = _state(cumulative_pnl_by_day=ledger)
        violation = FUNDINGPIPS_CONSISTENCY.evaluate(state)
        assert violation is not None
        assert FUNDINGPIPS_CONSISTENCY.confidence == "high"
        assert violation.severity == "kill"

    def test_uncertain_predicate_violation_has_warn_severity(self) -> None:
        """HFT is uncertain → _violation maps to warn."""
        violation = FUNDINGPIPS_HFT._violation("test")
        assert FUNDINGPIPS_HFT.confidence == "uncertain"
        assert violation.severity == "warn"

    @pytest.mark.parametrize("model", ["1step", "2step", "2step_pro"])
    def test_all_model_predicates_have_valid_confidence(self, model: str) -> None:
        for predicate in FUNDINGPIPS_PREDICATES_BY_MODEL[model]:
            assert predicate.confidence in {"high", "uncertain"}

    @pytest.mark.parametrize("model", ["1step", "2step", "2step_pro"])
    def test_all_model_predicates_have_required_fields(self, model: str) -> None:
        for predicate in FUNDINGPIPS_PREDICATES_BY_MODEL[model]:
            assert predicate.name
            assert predicate.firm == "fundingpips"
            assert predicate.tos_quote
            assert predicate.interpretation

    def test_severity_mapping_invariant_across_all_predicates(self) -> None:
        for predicate in FUNDINGPIPS_PREDICATES:
            v = predicate._violation("test message")
            expected_severity = "kill" if predicate.confidence == "high" else "warn"
            assert v.severity == expected_severity, predicate.name


# --------------------------------------------------------------------------- #
# Server-time — fixed UTC+3, no DST
# --------------------------------------------------------------------------- #
class TestServerMidnightFixedUtcPlus3:
    """FundingPips's server is fixed UTC+3; midnight is 21:00 UTC year-round."""

    def test_winter_midnight_at_21_utc(self) -> None:
        """In January (no DST anywhere), midnight = 21:00 UTC of previous day."""
        ts = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
        midnight = server_midnight_before(ts)
        assert midnight == datetime(2026, 1, 15, 21, 0, tzinfo=UTC)

    def test_summer_midnight_at_21_utc(self) -> None:
        """In July, still 21:00 UTC because FundingPips's server has NO DST.
        This is the **key difference** from FTMO/FundedNext."""
        ts = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
        midnight = server_midnight_before(ts)
        assert midnight == datetime(2026, 7, 15, 21, 0, tzinfo=UTC)

    def test_server_midnight_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="must be tz-aware"):
            server_midnight_before(datetime(2026, 6, 15, 12, 0))  # naive


# --------------------------------------------------------------------------- #
# Snapshot integrity
# --------------------------------------------------------------------------- #
class TestSnapshotIntegrity:
    """Every predicate's tos_quote appears in the snapshot file."""

    def test_snapshot_file_exists(self) -> None:
        assert _SNAPSHOT_PATH.is_file()

    def test_fundingpips_predicate_tos_quotes_appear_in_snapshot(self) -> None:
        snapshot_text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
        snapshot_lines = [
            line.lstrip("> ").rstrip() if line.startswith(">") else line
            for line in snapshot_text.splitlines()
        ]
        normalized_snapshot = " ".join(" ".join(snapshot_lines).split())
        all_preds: set[Predicate] = set()
        for tup in FUNDINGPIPS_PREDICATES_BY_MODEL.values():
            all_preds.update(tup)
        for predicate in all_preds:
            normalized_quote = " ".join(predicate.tos_quote.split())
            assert normalized_quote in normalized_snapshot, (
                f"{predicate.name}: tos_quote not found in snapshot. "
                f"Quote (normalized): {normalized_quote!r}"
            )


# --------------------------------------------------------------------------- #
# Multi-model registry
# --------------------------------------------------------------------------- #
class TestModelRegistry:
    """FUNDINGPIPS_PREDICATES_BY_MODEL exposes per-model tuples; default aliases 2-Step."""

    def test_default_alias_is_2step(self) -> None:
        assert FUNDINGPIPS_PREDICATES is FUNDINGPIPS_PREDICATES_BY_MODEL["2step"]

    def test_model_keys_are_documented_set(self) -> None:
        assert set(FUNDINGPIPS_PREDICATES_BY_MODEL.keys()) == {"1step", "2step", "2step_pro"}

    def test_per_model_drawdown_thresholds_match_snapshot(self) -> None:
        assert FUNDINGPIPS_1STEP_DAILY_DD.threshold_fraction == 0.03
        assert FUNDINGPIPS_2STEP_DAILY_DD.threshold_fraction == 0.05
        assert FUNDINGPIPS_2STEP_PRO_DAILY_DD.threshold_fraction == 0.03

    def test_per_model_max_dd_thresholds_match_snapshot(self) -> None:
        assert FUNDINGPIPS_1STEP_MAX_DD.threshold_fraction == 0.06
        assert FUNDINGPIPS_2STEP_MAX_DD.threshold_fraction == 0.10
        assert FUNDINGPIPS_2STEP_PRO_MAX_DD.threshold_fraction == 0.06

    def test_each_model_contains_drawdown_target_and_min_days(self) -> None:
        for model, preds in FUNDINGPIPS_PREDICATES_BY_MODEL.items():
            names = {p.name for p in preds}
            assert any("daily_drawdown" in n for n in names), model
            assert any("max_drawdown" in n for n in names), model
            assert any("profit_target" in n for n in names), model
            assert any("min_trading_days" in n for n in names), model


class TestRegistryIntegration:
    """ALL_FIRM_PREDICATES exposes the fundingpips default."""

    def test_registry_exposes_fundingpips_default(self) -> None:
        from propfarm.rules.registry import ALL_FIRM_PREDICATES

        assert "fundingpips" in ALL_FIRM_PREDICATES
        assert ALL_FIRM_PREDICATES["fundingpips"] is FUNDINGPIPS_PREDICATES

    def test_model_registry_exposes_all_models(self) -> None:
        from propfarm.rules.registry import ALL_MODEL_PREDICATES

        assert ("fundingpips", "1step") in ALL_MODEL_PREDICATES
        assert ("fundingpips", "2step") in ALL_MODEL_PREDICATES
        assert ("fundingpips", "2step_pro") in ALL_MODEL_PREDICATES


# --------------------------------------------------------------------------- #
# Phase 0 no-op sanity
# --------------------------------------------------------------------------- #
class TestPhase0NoOps:
    @pytest.mark.parametrize(
        "predicate",
        [
            FUNDINGPIPS_HFT,
            FUNDINGPIPS_LATENCY_ARB,
            FUNDINGPIPS_COPY_TRADING,
            FUNDINGPIPS_MARTINGALE,
            FUNDINGPIPS_NEWS_BLACKOUT,
            FUNDINGPIPS_TIME_LIMIT,
        ],
    )
    def test_phase0_predicate_returns_none(self, predicate: Predicate) -> None:
        assert predicate.evaluate(_state()) is None


# --------------------------------------------------------------------------- #
# Consistency edges (FundingPips uses 35% threshold, kill severity)
# --------------------------------------------------------------------------- #
class TestConsistencyCheckEdges:
    def test_empty_ledger_returns_none(self) -> None:
        assert FUNDINGPIPS_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=())) is None

    def test_negative_total_returns_none(self) -> None:
        ledger = (("2026-06-01", -100.0), ("2026-06-02", -50.0))
        assert FUNDINGPIPS_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger)) is None

    def test_balanced_days_no_violation(self) -> None:
        # 100/100/100 → 33.3% each, below 35% threshold.
        ledger = (("2026-06-01", 100.0), ("2026-06-02", 100.0), ("2026-06-03", 100.0))
        assert FUNDINGPIPS_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger)) is None

    def test_dominant_day_kill_violation(self) -> None:
        """36% day-share trips with severity=kill (high confidence)."""
        # 36/32/32 → day1 = 36% > 35%.
        ledger = (("2026-06-01", 360.0), ("2026-06-02", 320.0), ("2026-06-03", 320.0))
        violation = FUNDINGPIPS_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger))
        assert violation is not None
        assert violation.severity == "kill"


# --------------------------------------------------------------------------- #
# Reviewer-mandated dual-fire + snapshot↔code confidence agreement
# --------------------------------------------------------------------------- #
class TestDualFire:
    """Achievement (profit target) + Violation (daily DD) from same firm's predicates."""

    def test_profit_target_and_daily_dd_fire_simultaneously_2step(self) -> None:
        # Target: +8% Phase 1.
        target_state = _state(
            account_size=100_000.0,
            current_equity=108_000.0,
            current_balance=108_000.0,
            daily_start_equity=108_000.0,
        )
        target_event = FUNDINGPIPS_2STEP_PROFIT_TARGET_PHASE1_8PCT.evaluate(target_state)
        assert isinstance(target_event, Achievement)

        # DD: -5.41% from $108k reference.
        dd_state = _state(
            account_size=100_000.0,
            current_equity=102_590.0,
            current_balance=108_000.0,
            daily_start_equity=108_000.0,
        )
        dd_event = FUNDINGPIPS_2STEP_DAILY_DD.evaluate(dd_state)
        assert isinstance(dd_event, Violation)
        assert dd_event.severity == "kill"

    @pytest.mark.parametrize(
        ("preds_attr", "target_equity"),
        [
            (FUNDINGPIPS_1STEP_PREDICATES, 110_000.0),  # 10% target
            (FUNDINGPIPS_2STEP_PREDICATES, 108_000.0),  # 8% Phase-1 target
            (FUNDINGPIPS_2STEP_PRO_PREDICATES, 106_000.0),  # 6% target
        ],
    )
    def test_iterating_returns_mixed_event_types_per_model(
        self,
        preds_attr: tuple[Predicate, ...],
        target_equity: float,
    ) -> None:
        # -3.01% trips 1-Step / 2-Step Pro; -5.01% trips 2-Step.
        # Use -5.5% which trips all three's daily DD.
        state_dd = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_500.0
        )
        state_target = _state(current_equity=target_equity)
        events_dd = [p.evaluate(state_dd) for p in preds_attr]
        events_target = [p.evaluate(state_target) for p in preds_attr]
        assert any(isinstance(e, Violation) for e in events_dd if e is not None)
        assert any(isinstance(e, Achievement) for e in events_target if e is not None)


class TestSnapshotConfidenceAgreement:
    """Snapshot summary table's confidence column matches runtime predicates."""

    def test_snapshot_summary_table_confidence_matches_code(self) -> None:
        text = _SNAPSHOT_PATH.read_text()
        row_re = re.compile(
            r"^\|\s*`(?P<name>[a-z_0-9]+)`\s*\|\s*(?P<conf>high|uncertain)\s*\|",
            re.MULTILINE,
        )
        snapshot_confidence = {m.group("name"): m.group("conf") for m in row_re.finditer(text)}
        assert snapshot_confidence

        all_preds: set[Predicate] = set()
        for tup in FUNDINGPIPS_PREDICATES_BY_MODEL.values():
            all_preds.update(tup)
        for pred in all_preds:
            assert pred.name in snapshot_confidence, (
                f"{pred.name}: not in snapshot summary table — drift suspected"
            )
            assert snapshot_confidence[pred.name] == pred.confidence, (
                f"{pred.name}: snapshot says {snapshot_confidence[pred.name]!r}, "
                f"code says {pred.confidence!r} — drift"
            )
