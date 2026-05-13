"""Tests for ``propfarm.rules.fundednext`` — FundedNext rule predicates (W4b).

Mirrors ``test_ftmo.py``: boundary tests at exact percentages, confidence-driven
behavior, server-time / DST, snapshot integrity, reviewer-mandated dual-fire
test (Achievement + Violation), reviewer-mandated snapshot↔code confidence
agreement test.

Every test is offline. No MT5, broker, or VPS strings appear.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from propfarm.rules.fundednext import (
    FUNDEDNEXT_CONSISTENCY,
    FUNDEDNEXT_COPY_TRADING,
    FUNDEDNEXT_HFT,
    FUNDEDNEXT_HYPERACTIVITY,
    FUNDEDNEXT_LATENCY_ARB,
    FUNDEDNEXT_MARTINGALE,
    FUNDEDNEXT_NEWS_BLACKOUT,
    FUNDEDNEXT_PREDICATES,
    FUNDEDNEXT_PREDICATES_BY_MODEL,
    FUNDEDNEXT_STELLAR_1STEP_DAILY_DD,
    FUNDEDNEXT_STELLAR_1STEP_MAX_DD,
    FUNDEDNEXT_STELLAR_1STEP_PREDICATES,
    FUNDEDNEXT_STELLAR_1STEP_PROFIT_TARGET,
    FUNDEDNEXT_STELLAR_2STEP_DAILY_DD,
    FUNDEDNEXT_STELLAR_2STEP_MAX_DD,
    FUNDEDNEXT_STELLAR_2STEP_PREDICATES,
    FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1,
    FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE2,
    FUNDEDNEXT_STELLAR_LITE_DAILY_DD,
    FUNDEDNEXT_STELLAR_LITE_MAX_DD,
    FUNDEDNEXT_STELLAR_LITE_PREDICATES,
    FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE1,
    FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2,
    FUNDEDNEXT_TIME_LIMIT,
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
_SNAPSHOT_PATH: Path = _REPO_ROOT / "docs" / "firm-tos-snapshots" / "fundednext-rules-2026-05-12.md"


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
        firm="fundednext",
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
# Boundary tests: Daily DD — Stellar 2-Step (5%)
# --------------------------------------------------------------------------- #
class TestStellar2StepDailyDDBoundary:
    """5.01% trips, 4.99% does not — Stellar 2-Step."""

    def test_violation_at_5_01_percent(self) -> None:
        """5.01% intraday loss → kill Violation."""
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_990.0
        )
        result = FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state)
        assert result is not None
        assert result.severity == "kill"
        assert result.predicate_name == "fundednext_stellar_2step_daily_drawdown"

    def test_no_violation_at_4_99_percent(self) -> None:
        """4.99% intraday loss → None."""
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=95_010.0
        )
        assert FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state) is None

    def test_no_violation_exactly_at_5_00_percent(self) -> None:
        """Exactly 5.00% → strict-> semantics: not a violation."""
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=95_000.0
        )
        assert FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state) is None

    def test_reference_base_is_max_of_equity_and_balance(self) -> None:
        """Daily-loss reference base is max(daily_start_equity, current_balance)."""
        # current_balance is up to $103k mid-day; floor is now $103k - $5k = $98k,
        # NOT $100k - $5k = $95k. Equity at $97k → loss from reference $103k
        # = $6k = 6.0% > 5% → violation.
        state = _state(
            daily_start_equity=100_000.0, current_balance=103_000.0, current_equity=97_000.0
        )
        result = FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state)
        assert result is not None
        assert result.severity == "kill"


# --------------------------------------------------------------------------- #
# Boundary tests: Daily DD — Stellar 1-Step (3%) and Stellar Lite (4%)
# --------------------------------------------------------------------------- #
class TestStellar1StepDailyDDBoundary:
    """3.01% trips, 2.99% does not — Stellar 1-Step."""

    def test_violation_at_3_01_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=96_990.0
        )
        result = FUNDEDNEXT_STELLAR_1STEP_DAILY_DD.evaluate(state)
        assert result is not None
        assert result.severity == "kill"

    def test_no_violation_at_2_99_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=97_010.0
        )
        assert FUNDEDNEXT_STELLAR_1STEP_DAILY_DD.evaluate(state) is None


class TestStellarLiteDailyDDBoundary:
    """4.01% trips, 3.99% does not — Stellar Lite."""

    def test_violation_at_4_01_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=95_990.0
        )
        result = FUNDEDNEXT_STELLAR_LITE_DAILY_DD.evaluate(state)
        assert result is not None
        assert result.severity == "kill"

    def test_no_violation_at_3_99_percent(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=96_010.0
        )
        assert FUNDEDNEXT_STELLAR_LITE_DAILY_DD.evaluate(state) is None


# --------------------------------------------------------------------------- #
# Boundary tests: Max DD per model
# --------------------------------------------------------------------------- #
class TestMaxDrawdownBoundary:
    """10% / 6% / 8% — Stellar 2-Step / 1-Step / Lite."""

    def test_2step_violation_at_10_01_percent(self) -> None:
        state = _state(current_equity=89_990.0)
        result = FUNDEDNEXT_STELLAR_2STEP_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_2step_no_violation_at_9_99_percent(self) -> None:
        state = _state(current_equity=90_010.0)
        assert FUNDEDNEXT_STELLAR_2STEP_MAX_DD.evaluate(state) is None

    def test_1step_violation_at_6_01_percent(self) -> None:
        state = _state(current_equity=93_990.0)
        result = FUNDEDNEXT_STELLAR_1STEP_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_1step_no_violation_at_5_99_percent(self) -> None:
        state = _state(current_equity=94_010.0)
        assert FUNDEDNEXT_STELLAR_1STEP_MAX_DD.evaluate(state) is None

    def test_lite_violation_at_8_01_percent(self) -> None:
        state = _state(current_equity=91_990.0)
        result = FUNDEDNEXT_STELLAR_LITE_MAX_DD.evaluate(state)
        assert result is not None and result.severity == "kill"

    def test_lite_no_violation_at_7_99_percent(self) -> None:
        state = _state(current_equity=92_010.0)
        assert FUNDEDNEXT_STELLAR_LITE_MAX_DD.evaluate(state) is None

    def test_max_dd_non_trailing_does_not_trip_on_equity_below_peak(self) -> None:
        """Max DD is non-trailing — relative to starting balance only."""
        state = _state(current_equity=95_000.0, overall_high_water_mark=115_000.0)
        assert FUNDEDNEXT_STELLAR_2STEP_MAX_DD.evaluate(state) is None


# --------------------------------------------------------------------------- #
# Boundary tests: Profit target per model + phase
# --------------------------------------------------------------------------- #
class TestProfitTargetBoundary:
    """Targets: 8%/5% (2-Step), 10% (1-Step), 8%/4% (Lite). Achievement on hit."""

    def test_stellar_2step_phase1_at_8_percent(self) -> None:
        state = _state(current_equity=108_000.0)
        result = FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1.evaluate(state)
        assert isinstance(result, Achievement)
        assert result.achievement_kind == "profit_target"
        assert not isinstance(result, Violation)

    def test_stellar_2step_phase1_below_8_percent_no_event(self) -> None:
        state = _state(current_equity=107_990.0)
        assert FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1.evaluate(state) is None

    def test_stellar_2step_phase2_at_5_percent(self) -> None:
        state = _state(current_equity=105_000.0)
        result = FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE2.evaluate(state)
        assert isinstance(result, Achievement)

    def test_stellar_1step_at_10_percent(self) -> None:
        state = _state(current_equity=110_000.0)
        result = FUNDEDNEXT_STELLAR_1STEP_PROFIT_TARGET.evaluate(state)
        assert isinstance(result, Achievement)

    def test_stellar_lite_phase1_at_8_percent(self) -> None:
        state = _state(current_equity=108_000.0)
        result = FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE1.evaluate(state)
        assert isinstance(result, Achievement)

    def test_stellar_lite_phase2_at_4_percent(self) -> None:
        state = _state(current_equity=104_000.0)
        result = FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2.evaluate(state)
        assert isinstance(result, Achievement)

    def test_stellar_lite_phase2_below_4_percent_no_event(self) -> None:
        state = _state(current_equity=103_990.0)
        assert FUNDEDNEXT_STELLAR_LITE_PROFIT_TARGET_PHASE2.evaluate(state) is None


# --------------------------------------------------------------------------- #
# Confidence-driven behavior
# --------------------------------------------------------------------------- #
class TestConfidenceBehavior:
    """Severity follows confidence on every FundedNext predicate."""

    def test_high_confidence_violation_has_kill_severity(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_000.0
        )
        violation = FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state)
        assert violation is not None
        assert FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.confidence == "high"
        assert violation.severity == "kill"

    def test_uncertain_predicate_violation_has_warn_severity(self) -> None:
        ledger = (("2026-06-01", 800.0), ("2026-06-02", 100.0), ("2026-06-03", 100.0))
        state = _state(cumulative_pnl_by_day=ledger)
        violation = FUNDEDNEXT_CONSISTENCY.evaluate(state)
        assert violation is not None
        assert FUNDEDNEXT_CONSISTENCY.confidence == "uncertain"
        assert violation.severity == "warn"

    def test_violation_carries_tos_quote_and_confidence(self) -> None:
        state = _state(
            daily_start_equity=100_000.0, current_balance=100_000.0, current_equity=94_000.0
        )
        violation = FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(state)
        assert violation is not None
        assert violation.tos_quote == FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.tos_quote
        assert violation.confidence == FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.confidence

    @pytest.mark.parametrize("model", ["stellar_2step", "stellar_1step", "stellar_lite"])
    def test_all_model_predicates_have_valid_confidence(self, model: str) -> None:
        for predicate in FUNDEDNEXT_PREDICATES_BY_MODEL[model]:
            assert predicate.confidence in {"high", "uncertain"}

    @pytest.mark.parametrize("model", ["stellar_2step", "stellar_1step", "stellar_lite"])
    def test_all_model_predicates_have_required_fields(self, model: str) -> None:
        for predicate in FUNDEDNEXT_PREDICATES_BY_MODEL[model]:
            assert predicate.name
            assert predicate.firm == "fundednext"
            assert predicate.tos_quote
            assert predicate.interpretation

    def test_severity_mapping_invariant_across_all_predicates(self) -> None:
        """Iterate every predicate; severity matches confidence via _violation."""
        for predicate in FUNDEDNEXT_PREDICATES:
            v = predicate._violation("test message")
            expected_severity = "kill" if predicate.confidence == "high" else "warn"
            assert v.severity == expected_severity, predicate.name


# --------------------------------------------------------------------------- #
# Server-time / DST — same Europe/Athens zone as FTMO (re-exported helper)
# --------------------------------------------------------------------------- #
class TestServerMidnightDST:
    """server_midnight_before respects EET (winter) and EEST (summer) DST."""

    def test_winter_midnight_at_22_utc(self) -> None:
        ts = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)
        midnight = server_midnight_before(ts)
        assert midnight == datetime(2026, 1, 15, 22, 0, tzinfo=UTC)

    def test_summer_midnight_at_21_utc(self) -> None:
        ts = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
        midnight = server_midnight_before(ts)
        assert midnight == datetime(2026, 7, 15, 21, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Snapshot integrity
# --------------------------------------------------------------------------- #
class TestSnapshotIntegrity:
    """Every predicate's tos_quote appears in the snapshot file."""

    def test_snapshot_file_exists(self) -> None:
        assert _SNAPSHOT_PATH.is_file()

    def test_fundednext_predicate_tos_quotes_appear_in_snapshot(self) -> None:
        snapshot_text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
        snapshot_lines = [
            line.lstrip("> ").rstrip() if line.startswith(">") else line
            for line in snapshot_text.splitlines()
        ]
        normalized_snapshot = " ".join(" ".join(snapshot_lines).split())
        # Cover all per-model predicate tuples plus cross-model.
        all_preds: set[Predicate] = set()
        for tup in FUNDEDNEXT_PREDICATES_BY_MODEL.values():
            all_preds.update(tup)
        for predicate in all_preds:
            normalized_quote = " ".join(predicate.tos_quote.split())
            assert normalized_quote in normalized_snapshot, (
                f"{predicate.name}: tos_quote not found in snapshot. "
                f"Quote (normalized): {normalized_quote!r}"
            )


# --------------------------------------------------------------------------- #
# Multi-model registry / loader-pattern consistency
# --------------------------------------------------------------------------- #
class TestModelRegistry:
    """FUNDEDNEXT_PREDICATES_BY_MODEL exposes per-model tuples; default aliases Stellar 2-Step."""

    def test_default_alias_is_stellar_2step(self) -> None:
        assert FUNDEDNEXT_PREDICATES is FUNDEDNEXT_PREDICATES_BY_MODEL["stellar_2step"]

    def test_model_keys_are_documented_set(self) -> None:
        assert set(FUNDEDNEXT_PREDICATES_BY_MODEL.keys()) == {
            "stellar_2step",
            "stellar_1step",
            "stellar_lite",
        }

    def test_each_model_tuple_is_non_empty(self) -> None:
        for tup in FUNDEDNEXT_PREDICATES_BY_MODEL.values():
            assert len(tup) > 0

    def test_each_model_contains_drawdown_target_and_min_days(self) -> None:
        """Sanity: every model's predicate set covers the minimum rule shape."""
        for model, preds in FUNDEDNEXT_PREDICATES_BY_MODEL.items():
            names = {p.name for p in preds}
            assert any("daily_drawdown" in n for n in names), model
            assert any("max_drawdown" in n for n in names), model
            assert any("profit_target" in n for n in names), model
            assert any("min_trading_days" in n for n in names), model

    def test_per_model_drawdown_thresholds_match_snapshot(self) -> None:
        """Each model's daily-DD threshold matches the snapshot's numeric."""
        assert FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.threshold_fraction == 0.05
        assert FUNDEDNEXT_STELLAR_1STEP_DAILY_DD.threshold_fraction == 0.03
        assert FUNDEDNEXT_STELLAR_LITE_DAILY_DD.threshold_fraction == 0.04

    def test_per_model_max_dd_thresholds_match_snapshot(self) -> None:
        assert FUNDEDNEXT_STELLAR_2STEP_MAX_DD.threshold_fraction == 0.10
        assert FUNDEDNEXT_STELLAR_1STEP_MAX_DD.threshold_fraction == 0.06
        assert FUNDEDNEXT_STELLAR_LITE_MAX_DD.threshold_fraction == 0.08


class TestRegistryIntegration:
    """ALL_FIRM_PREDICATES (now in registry.py) exposes the fundednext default."""

    def test_registry_exposes_fundednext_default(self) -> None:
        from propfarm.rules.registry import ALL_FIRM_PREDICATES

        assert "fundednext" in ALL_FIRM_PREDICATES
        assert ALL_FIRM_PREDICATES["fundednext"] is FUNDEDNEXT_PREDICATES

    def test_model_registry_exposes_all_models(self) -> None:
        from propfarm.rules.registry import ALL_MODEL_PREDICATES

        assert ("fundednext", "stellar_2step") in ALL_MODEL_PREDICATES
        assert ("fundednext", "stellar_1step") in ALL_MODEL_PREDICATES
        assert ("fundednext", "stellar_lite") in ALL_MODEL_PREDICATES


# --------------------------------------------------------------------------- #
# Phase 0 no-op sanity
# --------------------------------------------------------------------------- #
class TestPhase0NoOps:
    """Predicates whose detection logic lands in Task 12 return None in Phase 0."""

    @pytest.mark.parametrize(
        "predicate",
        [
            FUNDEDNEXT_HFT,
            FUNDEDNEXT_LATENCY_ARB,
            FUNDEDNEXT_COPY_TRADING,
            FUNDEDNEXT_MARTINGALE,
            FUNDEDNEXT_HYPERACTIVITY,
            FUNDEDNEXT_NEWS_BLACKOUT,
            FUNDEDNEXT_TIME_LIMIT,
        ],
    )
    def test_phase0_predicate_returns_none(self, predicate: Predicate) -> None:
        state = _state()
        assert predicate.evaluate(state) is None


# --------------------------------------------------------------------------- #
# Consistency edges
# --------------------------------------------------------------------------- #
class TestConsistencyCheckEdges:
    def test_empty_ledger_returns_none(self) -> None:
        assert FUNDEDNEXT_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=())) is None

    def test_negative_total_returns_none(self) -> None:
        ledger = (("2026-06-01", -100.0), ("2026-06-02", -50.0))
        assert FUNDEDNEXT_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger)) is None

    def test_balanced_days_no_violation(self) -> None:
        ledger = (("2026-06-01", 100.0), ("2026-06-02", 100.0), ("2026-06-03", 100.0))
        assert FUNDEDNEXT_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger)) is None

    def test_dominant_day_warn_violation(self) -> None:
        ledger = (("2026-06-01", 600.0), ("2026-06-02", 200.0), ("2026-06-03", 200.0))
        violation = FUNDEDNEXT_CONSISTENCY.evaluate(_state(cumulative_pnl_by_day=ledger))
        assert violation is not None and violation.severity == "warn"


# --------------------------------------------------------------------------- #
# Reviewer-mandated patterns: dual-fire + snapshot↔code confidence agreement
# --------------------------------------------------------------------------- #
class TestDualFire:
    """Achievement (profit target) + Violation (daily DD) from same firm's predicates."""

    def test_profit_target_and_daily_dd_fire_simultaneously_stellar_2step(self) -> None:
        # Target hit: +8% Phase 1 from account_size starting balance.
        target_state = _state(
            account_size=100_000.0,
            current_equity=108_000.0,
            current_balance=108_000.0,
            daily_start_equity=108_000.0,
        )
        target_event = FUNDEDNEXT_STELLAR_2STEP_PROFIT_TARGET_PHASE1.evaluate(target_state)
        assert isinstance(target_event, Achievement)

        # DD breach: -5.01% from reference $108k (max of daily_start_equity / balance).
        # Equity at $102,590 → loss $5,410 → -5.41% > 5%.
        dd_state = _state(
            account_size=100_000.0,
            current_equity=102_590.0,
            current_balance=108_000.0,
            daily_start_equity=108_000.0,
        )
        dd_event = FUNDEDNEXT_STELLAR_2STEP_DAILY_DD.evaluate(dd_state)
        assert isinstance(dd_event, Violation)
        assert dd_event.severity == "kill"

    @pytest.mark.parametrize(
        ("preds_attr", "target_equity"),
        [
            (FUNDEDNEXT_STELLAR_2STEP_PREDICATES, 108_000.0),  # 2-step Phase 1 = 8%
            (FUNDEDNEXT_STELLAR_1STEP_PREDICATES, 110_000.0),  # 1-step = 10%
            (FUNDEDNEXT_STELLAR_LITE_PREDICATES, 108_000.0),  # lite Phase 1 = 8%
        ],
    )
    def test_iterating_returns_mixed_event_types_per_model(
        self,
        preds_attr: tuple[Predicate, ...],
        target_equity: float,
    ) -> None:
        """Each model's predicate tuple can produce both Violation and Achievement."""
        # Tight enough to trip 2-Step's 5% rule too (so trips all three models).
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
        for tup in FUNDEDNEXT_PREDICATES_BY_MODEL.values():
            all_preds.update(tup)
        for pred in all_preds:
            assert pred.name in snapshot_confidence, (
                f"{pred.name}: not in snapshot summary table — drift suspected"
            )
            assert snapshot_confidence[pred.name] == pred.confidence, (
                f"{pred.name}: snapshot says {snapshot_confidence[pred.name]!r}, "
                f"code says {pred.confidence!r} — drift"
            )
