"""Tests for the Gate 2B comparison harness.

Synthesizes capture parquets in temp directories — never touches a real
broker, never imports MetaTrader5, never hits the network. The synthetic
captures are crafted to exercise both the happy path (a clean capture
where sim ≈ live) and adversarial paths (systematic bias, retcode
mismatch, schema drift).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from propfarm.gates.gate_2b import (
    SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS,
    SYMBOL_SPREAD_P95_THRESHOLD_PIPS,
    Gate2BReport,
    MarketStateReconstruction,
    compute_residual,
    reconstruct_fill_request,
    reconstruct_market_state,
    run_gate_2b,
)
from propfarm.sim.fill_engine import simulate_fill
from propfarm.sim.market import MarketState

# --------------------------------------------------------------------------- #
# Synthesis helpers
# --------------------------------------------------------------------------- #
_START = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
_EURUSD_BASE = 1.08000


def _make_row(
    *,
    idx: int,
    run_id: str = "synthetic",
    symbol: str = "EURUSD",
    order_type: str = "market",
    side: str = "buy",
    volume_lots: float = 0.01,
    requested_price: float | None = None,
    fill_price_offset_pips: float = 0.0,
    spread_at_request_pips: float = 0.3,
    slippage_observed_pips: float | None = None,
    broker_latency_ms: float = 150.0,
    retcode: int = 10009,
    comment: str = "",
    pip_size: float = 0.0001,
) -> dict[str, Any]:
    """Synthesize one ``FillRecord`` dict.

    Defaults model a clean EURUSD market-buy that filled at the requested
    price with the engine-default slippage. Overrides simulate bias / errors.
    """
    if requested_price is None:
        requested_price = _EURUSD_BASE + idx * pip_size
    request_time = _START + timedelta(seconds=60 * idx)
    fill_time = request_time + timedelta(milliseconds=broker_latency_ms)
    if retcode == 10009:
        if side == "buy":
            fill_price = requested_price + fill_price_offset_pips * pip_size
            slip = (
                slippage_observed_pips
                if slippage_observed_pips is not None
                else fill_price_offset_pips
            )
        else:
            fill_price = requested_price - fill_price_offset_pips * pip_size
            slip = (
                slippage_observed_pips
                if slippage_observed_pips is not None
                else fill_price_offset_pips
            )
    else:
        fill_price = math.nan
        slip = math.nan
    return {
        "run_id": run_id,
        "request_time_utc": request_time,
        "broker_fill_time_utc": fill_time,
        "symbol": symbol,
        "order_type": order_type,
        "side": side,
        "volume_lots": volume_lots,
        "requested_price": requested_price,
        "fill_price": fill_price,
        "spread_at_request_pips": spread_at_request_pips,
        "slippage_observed_pips": slip,
        "broker_latency_ms": broker_latency_ms,
        "retcode": retcode,
        "comment": comment,
    }


def _write_capture(rows: list[dict[str, Any]], path: Path) -> None:
    """Persist a list of FillRecord-shaped dicts to a parquet at ``path``."""
    df = pl.DataFrame(rows)
    df = df.with_columns(
        [
            pl.col("request_time_utc").cast(pl.Datetime(time_zone="UTC")),
            pl.col("broker_fill_time_utc").cast(pl.Datetime(time_zone="UTC")),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


# --------------------------------------------------------------------------- #
# Test 1: reconstruct_fill_request round-trip
# --------------------------------------------------------------------------- #
def test_reconstruct_fill_request_round_trip() -> None:
    """FillRequest reconstruction is field-equal on shared columns."""
    row = _make_row(idx=3, comment="test-comment")
    req = reconstruct_fill_request(row)
    assert req.run_id == row["run_id"]
    assert req.symbol == row["symbol"]
    assert req.order_type == row["order_type"]
    assert req.side == row["side"]
    assert req.volume_lots == row["volume_lots"]
    assert req.requested_price == row["requested_price"]
    assert req.request_time_utc == row["request_time_utc"]
    assert req.comment == row["comment"]


# --------------------------------------------------------------------------- #
# Test 2: reconstruct_market_state surfaces all field sources
# --------------------------------------------------------------------------- #
def test_reconstruct_market_state_surfaces_all_field_sources() -> None:
    """Audit trail has one entry per MarketState field (5 total)."""
    row = _make_row(idx=0)
    state, audit = reconstruct_market_state(row, prior_same_symbol_prices=None)
    assert isinstance(state, MarketState)
    expected = {"symbol", "ts_utc", "realized_vol_5m", "news_window", "stress_mode"}
    field_names = {rec.field_name for rec in audit}
    assert field_names == expected
    assert len(audit) == 5
    # Each entry's source is one of the three allowed labels.
    for rec in audit:
        assert isinstance(rec, MarketStateReconstruction)
        assert rec.source in {"FROM_FILLRECORD", "COMPUTED", "DEFAULTED"}


# --------------------------------------------------------------------------- #
# Test 3: no silent defaults — DEFAULTED entries have non-empty detail
# --------------------------------------------------------------------------- #
def test_market_state_reconstruction_no_silent_default() -> None:
    """Every DEFAULTED / COMPUTED entry surfaces a non-empty source_detail."""
    row = _make_row(idx=0)
    _, audit = reconstruct_market_state(row, prior_same_symbol_prices=None)
    for rec in audit:
        if rec.source in {"DEFAULTED", "COMPUTED"}:
            assert rec.source_detail.strip(), (
                f"{rec.field_name} has source={rec.source} but empty detail — "
                f"this is the silent-default failure mode the harness must prevent"
            )


# --------------------------------------------------------------------------- #
# Test 4: residual sign is correct (live > sim → positive)
# --------------------------------------------------------------------------- #
def test_compute_residual_signed_correctly() -> None:
    """Live fill_price strictly above sim → positive fill_price_residual."""
    live = _make_row(idx=0)
    # Force live fill_price 1 pip above its requested price; sim will be
    # exactly the requested price (rng=None determinism + no engine slip
    # for low-vol baseline).
    live["fill_price"] = live["requested_price"] + 0.0001
    sim_result = simulate_fill(
        reconstruct_fill_request(live),
        MarketState(symbol="EURUSD", ts_utc=live["request_time_utc"]),
        execution_latency_ms=150.0,
        rng=None,
    )
    residual = compute_residual(live, sim_result)
    assert residual.fill_price_residual > 0.0
    # Specifically: live - sim is +1 pip iff sim hit the requested price.
    # The slippage model in deterministic-zero mode at typical vol can produce
    # a small positive slip for buys, so we assert sign and magnitude is
    # within an envelope rather than exact.
    assert residual.fill_price_residual == pytest.approx(live["fill_price"] - sim_result.fill_price)


# --------------------------------------------------------------------------- #
# Test 5: end-to-end on a synthetic capture
# --------------------------------------------------------------------------- #
def test_run_gate_2b_on_synthetic_capture(tmp_path: Path) -> None:
    """A 10-row clean synthetic capture compares cleanly."""
    rows = [_make_row(idx=i) for i in range(10)]
    capture = tmp_path / "synth.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture_parquet_path=capture)
    assert isinstance(report, Gate2BReport)
    assert report.n_rows_captured == 10
    # Comparisons happen on rows where both sides have non-NaN residuals and
    # retcodes match. Every row above is retcode=10009 so all 10 should
    # appear in the fill_price distribution.
    assert report.residuals_by_field["fill_price"].n == 10
    # Audit table is the five MarketState fields.
    audited = {rec.field_name for rec in report.market_state_reconstruction}
    assert audited == {"symbol", "ts_utc", "realized_vol_5m", "news_window", "stress_mode"}
    # Verdict is one of the allowed labels.
    assert report.verdict in {"pass", "fail", "investigate"}
    # Outputs persisted.
    assert (capture.parent / f"{report.run_id}_residuals.parquet").exists()
    assert (capture.parent / f"{report.run_id}_report.md").exists()


# --------------------------------------------------------------------------- #
# Test 6: biased capture is flagged
# --------------------------------------------------------------------------- #
def test_run_gate_2b_on_biased_synthetic_detects_systematic_bias(tmp_path: Path) -> None:
    """Live fill_price systematically 1 pip above sim → investigate or fail."""
    # Build a capture where every live fill is 1 pip *adverse* to the buy
    # (i.e. live filled 1 pip higher than the requested price). The sim
    # (under deterministic rng=None) will book the slippage-model's mean
    # adverse slip (~0.3 pip for EURUSD), so the residual is ~+0.7 pip per
    # row — statistically distinguishable from zero with n=30.
    rows = [
        _make_row(
            idx=i,
            fill_price_offset_pips=1.0,
            slippage_observed_pips=1.0,
        )
        for i in range(30)
    ]
    capture = tmp_path / "biased.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture_parquet_path=capture)
    # Either threshold-fail (p95 above 0.5 pip on EURUSD) OR
    # bias-investigate. Both indicate the sim is mis-calibrated.
    assert report.verdict in {"fail", "investigate"}
    # The failure_reasons must mention either systematic_bias or
    # fill_price_p95_exceeded — both are valid surfaces for this
    # adversarial input.
    combined = " ".join(report.failure_reasons)
    assert "systematic_bias" in combined or "fill_price_p95_exceeded" in combined


# --------------------------------------------------------------------------- #
# Test 7: retcode mismatch excluded from distribution
# --------------------------------------------------------------------------- #
def test_gate_2b_excludes_retcode_mismatch_rows(tmp_path: Path) -> None:
    """Row with live retcode != sim retcode is excluded from residual stats."""
    # Insert one row whose live retcode is 10018 (market closed) but the
    # request time is during market-open hours, so sim will produce 10009.
    # That row should NOT count in the residual distribution.
    rows = [_make_row(idx=i) for i in range(5)]
    mismatch = _make_row(idx=5, retcode=10018)
    rows.append(mismatch)
    rows.extend(_make_row(idx=i) for i in range(6, 10))
    capture = tmp_path / "mismatch.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 10
    # 9 of 10 retcodes match (the synth mismatch row is the 10th).
    assert report.n_retcode_matches == 9
    # Distribution n must exclude the mismatch row.
    assert report.residuals_by_field["fill_price"].n == 9


# --------------------------------------------------------------------------- #
# Test 8: per-symbol thresholds respected
# --------------------------------------------------------------------------- #
def test_gate_2b_per_symbol_thresholds() -> None:
    """The threshold table reflects the user-mandated per-symbol pips."""
    # EURUSD/GBPUSD/USDJPY/GER40/US100 → 0.5 pip; XAUUSD → 5.0 pip.
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["EURUSD"] == 0.5
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["GBPUSD"] == 0.5
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["USDJPY"] == 0.5
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["XAUUSD"] == 5.0
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["GER40"] == 0.5
    assert SYMBOL_FILL_PRICE_P95_THRESHOLD_PIPS["US100"] == 0.5
    assert SYMBOL_SPREAD_P95_THRESHOLD_PIPS == 1.0


# --------------------------------------------------------------------------- #
# Test 9: SHA256 pin in report
# --------------------------------------------------------------------------- #
def test_capture_parquet_sha256_pin(tmp_path: Path) -> None:
    """Report records the parquet's SHA256, stable across re-runs."""
    rows = [_make_row(idx=i) for i in range(5)]
    capture = tmp_path / "shacheck.parquet"
    _write_capture(rows, capture)
    report1 = run_gate_2b(capture_parquet_path=capture)
    report2 = run_gate_2b(capture_parquet_path=capture)
    assert report1.capture_parquet_sha256 == report2.capture_parquet_sha256
    # The SHA256 is 64 hex chars.
    assert len(report1.capture_parquet_sha256) == 64
    assert all(c in "0123456789abcdef" for c in report1.capture_parquet_sha256)


# --------------------------------------------------------------------------- #
# Test 10: schema-lock — missing FillRecord column raises ValueError
# --------------------------------------------------------------------------- #
def test_capture_schema_locked_to_FillRecord(tmp_path: Path) -> None:
    """Parquet missing a required column → ValueError at load time."""
    rows = [_make_row(idx=0)]
    df = pl.DataFrame(rows).with_columns(
        [
            pl.col("request_time_utc").cast(pl.Datetime(time_zone="UTC")),
            pl.col("broker_fill_time_utc").cast(pl.Datetime(time_zone="UTC")),
        ]
    )
    # Drop a required column.
    df_broken = df.drop("spread_at_request_pips")
    capture = tmp_path / "broken.parquet"
    capture.parent.mkdir(parents=True, exist_ok=True)
    df_broken.write_parquet(capture)
    with pytest.raises(ValueError, match="missing required FillRecord columns"):
        run_gate_2b(capture_parquet_path=capture)


# --------------------------------------------------------------------------- #
# Extra: determinism — same capture + same execution_latency → identical report
# --------------------------------------------------------------------------- #
def test_run_gate_2b_is_deterministic(tmp_path: Path) -> None:
    """Same inputs produce identical residual distributions."""
    rows = [_make_row(idx=i) for i in range(15)]
    capture = tmp_path / "determ.parquet"
    _write_capture(rows, capture)
    r1 = run_gate_2b(capture_parquet_path=capture, execution_latency_ms=150.0)
    r2 = run_gate_2b(capture_parquet_path=capture, execution_latency_ms=150.0)
    for field in r1.residuals_by_field:
        d1 = r1.residuals_by_field[field]
        d2 = r2.residuals_by_field[field]
        assert d1.n == d2.n
        assert d1.p50 == d2.p50
        assert d1.p95 == d2.p95
        assert d1.mean == d2.mean
        assert d1.t_stat == d2.t_stat if math.isfinite(d1.t_stat) else math.isnan(d2.t_stat)
    assert r1.verdict == r2.verdict
    assert r1.failure_reasons == r2.failure_reasons


# --------------------------------------------------------------------------- #
# Reviewer follow-ups (Gate 2B fresh-review pass)
# --------------------------------------------------------------------------- #
def test_required_columns_match_fill_record_fields() -> None:
    """Schema parity: `_REQUIRED_COLUMNS` must mirror `FillRecord.model_fields`.

    Reviewer flagged: `_REQUIRED_COLUMNS` is a hardcoded literal tuple.
    If `scripts/record_fills.FillRecord` ever gains a field, the gate's
    schema lock silently misses it. This test compares the two at test
    time so the drift is caught loudly without import-time runtime cost.
    """
    from propfarm.gates.gate_2b import _REQUIRED_COLUMNS, _load_fill_record_class

    fill_record_cls = _load_fill_record_class()
    fill_record_fields = set(fill_record_cls.model_fields.keys())
    required_set = set(_REQUIRED_COLUMNS)
    assert required_set == fill_record_fields, (
        f"_REQUIRED_COLUMNS drifted from FillRecord. "
        f"Only in code: {sorted(required_set - fill_record_fields)}, "
        f"only in FillRecord: {sorted(fill_record_fields - required_set)}"
    )


def test_audit_table_reports_per_field_source_distribution(tmp_path: Path) -> None:
    """The canonical audit must reflect per-row source counts, not just row 0.

    Reviewer flagged: previously the canonical audit was snapshotted off
    row 0, which always defaults `realized_vol_5m` (no prior history at
    idx=0). The operator would conclude every row defaulted — false when
    rows 5+ used COMPUTED. Fix: aggregate per-row audits, report dominant
    source + distribution counts in `source_detail`.
    """
    from propfarm.gates.gate_2b import run_gate_2b

    # _write_capture is the helper defined at top of this module.
    # Build a 20-row EURUSD capture so realized_vol_5m goes from DEFAULTED
    # (idx 0-4, insufficient history) to COMPUTED (idx 5+, has 5+ prior).
    rows = []
    base_ts = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    for i in range(20):
        rows.append(
            {
                "run_id": "run_audit_dist",
                "request_time_utc": base_ts + timedelta(minutes=i),
                "broker_fill_time_utc": base_ts + timedelta(minutes=i, milliseconds=150),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "buy",
                "volume_lots": 0.01,
                "requested_price": 1.10000 + i * 0.00001,
                "fill_price": 1.10000 + i * 0.00001 + 0.00003,
                "spread_at_request_pips": 0.3,
                "slippage_observed_pips": 0.3,
                "broker_latency_ms": 150.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    capture_path = tmp_path / "audit_dist.parquet"
    _write_capture(rows, capture_path)
    report = run_gate_2b(capture_path)

    # Find realized_vol_5m's audit entry.
    realized_vol_audit = next(
        rec for rec in report.market_state_reconstruction if rec.field_name == "realized_vol_5m"
    )
    # Distribution string should mention BOTH COMPUTED and DEFAULTED with counts.
    assert "COMPUTED" in realized_vol_audit.source_detail
    assert "DEFAULTED" in realized_vol_audit.source_detail
    assert "/20" in realized_vol_audit.source_detail
    # Dominant source should be COMPUTED (15 of 20 rows = 75%).
    assert realized_vol_audit.source == "COMPUTED"


def test_strict_biased_capture_produces_fail_with_both_failure_reasons(tmp_path: Path) -> None:
    """Stricter version of the biased-capture test.

    A 20-row EURUSD capture with live fill_price systematically +1.5 pips
    above sim must produce verdict='fail' (not 'investigate') AND both
    `fill_price_p95_exceeded` and `systematic_bias` in failure_reasons.

    Reviewer flagged the existing biased-synthetic test as permissive
    (accepts fail OR investigate, and accepts either failure reason).
    This test pins both surfaces together so a regression that disables
    one is caught immediately.
    """
    from propfarm.gates.gate_2b import run_gate_2b

    # _write_capture is the helper defined at top of this module.
    rows = []
    base_ts = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    pip = 0.0001
    for i in range(20):
        ts = base_ts + timedelta(minutes=i * 3)
        requested = 1.10000 + i * 0.00001
        # The "live" fill is systematically 1.5 pips worse than the sim
        # would predict. Sim slippage on EURUSD market is ~0.3 pips; live
        # records 0.3 + 1.5 = 1.8 pips. Resulting fill_price residual ≈
        # 1.5 pips, well over the 0.5 pip threshold.
        rows.append(
            {
                "run_id": "run_strict_bias",
                "request_time_utc": ts,
                "broker_fill_time_utc": ts + timedelta(milliseconds=150),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "buy",
                "volume_lots": 0.01,
                "requested_price": requested,
                "fill_price": requested + 1.8 * pip,  # live ≈ +1.8 pip slip
                "spread_at_request_pips": 0.3,
                "slippage_observed_pips": 1.8,
                "broker_latency_ms": 150.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    capture_path = tmp_path / "strict_bias.parquet"
    _write_capture(rows, capture_path)
    report = run_gate_2b(capture_path)

    assert report.verdict == "fail", (
        f"Expected verdict='fail' on +1.5 pip systematic bias; got {report.verdict!r} "
        f"with reasons {report.failure_reasons!r}"
    )
    reasons_blob = " | ".join(report.failure_reasons)
    assert "fill_price_p95_exceeded" in reasons_blob, (
        f"Expected fill_price_p95_exceeded in failure_reasons; got {report.failure_reasons!r}"
    )
    assert "systematic_bias" in reasons_blob, (
        f"Expected systematic_bias in failure_reasons; got {report.failure_reasons!r}"
    )


# --------------------------------------------------------------------------- #
# Manifest-status guard (2026-05-14 fix-up)
# --------------------------------------------------------------------------- #
def test_run_gate_2b_rejects_unusable_manifest_status(tmp_path: Path) -> None:
    """Sidecar manifest with ``status=fill_price-unusable`` → ValueError at load time.

    Regression for the 2026-05-13 capture (run_id 24e00278…) where every
    retcode=10009 row had fill_price=0.0 due to the OrderSendResult.price
    bug. The capture is preserved on disk (salvageable spread / latency
    columns) but must NOT be fed to Gate 2B; the manifest carries the
    ``status`` flag and the harness refuses to run.
    """
    import json

    from propfarm.gates.gate_2b import UNUSABLE_MANIFEST_STATUS, run_gate_2b

    rows = [_make_row(idx=i) for i in range(3)]
    capture = tmp_path / "unusable.parquet"
    _write_capture(rows, capture)
    # Write a manifest next to it with the unusable status.
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "unusable-test",
                "status": UNUSABLE_MANIFEST_STATUS,
                "unusable_reason": (
                    "OrderSendResult.price=0 bug, fixed at <commit>; "
                    "salvageable columns: retcode, requested_price, "
                    "spread_at_request_pips, broker_latency_ms"
                ),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fill_price-unusable"):
        run_gate_2b(capture_parquet_path=capture)


def test_run_gate_2b_accepts_manifest_without_status_key(tmp_path: Path) -> None:
    """Manifest without a ``status`` key is the normal Phase-0 case — not rejected.

    Forward-compat: existing manifests written by Phase-0 record_fills
    don't include a ``status`` key. Only an explicit
    ``fill_price-unusable`` status triggers the guard.
    """
    import json

    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(5)]
    capture = tmp_path / "normal.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps({"run_id": "normal-test", "n_attempted": 5, "n_filled": 5, "n_rejected": 0}),
        encoding="utf-8",
    )
    # Should run without raising.
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 5


def test_run_gate_2b_accepts_missing_manifest(tmp_path: Path) -> None:
    """Manifest absent → schema validation still runs but no manifest guard fires."""
    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(5)]
    capture = tmp_path / "no_manifest.parquet"
    _write_capture(rows, capture)
    # Deliberately do NOT write the manifest.
    assert not capture.with_suffix(".json").exists()
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 5
