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


# --------------------------------------------------------------------------- #
# Market-lookup-failure ratio guard (2026-05-14 fix v2)
# --------------------------------------------------------------------------- #
def test_run_gate_2b_rejects_manifest_above_market_lookup_failure_threshold(
    tmp_path: Path,
) -> None:
    """Manifest with n_market_lookup_failures > 5% of n_filled → ValueError.

    Regression for the 2026-05-14 short-test (run_id ``a68b59a6…``)
    where every market fill returned NaN due to the broker / Python
    history-cache contract not engaging. The fix v2 manifest schema
    1.1 surfaces the count as ``n_market_lookup_failures`` and Gate 2B
    refuses captures whose ratio exceeds 5%.
    """
    import json

    from propfarm.gates.gate_2b import (
        MAX_MARKET_LOOKUP_FAILURE_RATIO,
        run_gate_2b,
    )

    rows = [_make_row(idx=i) for i in range(20)]
    capture = tmp_path / "lookup_fail_high.parquet"
    _write_capture(rows, capture)
    # 4 failures / 20 filled = 20% → far above 5%.
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "lookup-fail-high",
                "n_attempted": 20,
                "n_filled": 20,
                "n_rejected": 0,
                "n_market_lookup_failures": 4,
                "schema_version": "1.1",
            }
        ),
        encoding="utf-8",
    )
    # Threshold is 0.05 (5%); 4/20 = 0.20 > 0.05.
    assert MAX_MARKET_LOOKUP_FAILURE_RATIO < 4 / 20
    with pytest.raises(ValueError, match="n_market_lookup_failures"):
        run_gate_2b(capture_parquet_path=capture)


def test_run_gate_2b_accepts_manifest_below_market_lookup_failure_threshold(
    tmp_path: Path,
) -> None:
    """Manifest with n_market_lookup_failures <= 5% of n_filled → accepted.

    A capture with 1 failure out of 100 filled (1%) sits well below the
    5% threshold and the guard passes. Verifies the threshold is the
    inclusive tolerance ceiling, not a zero-tolerance reject.
    """
    import json

    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(100)]
    capture = tmp_path / "lookup_fail_low.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "lookup-fail-low",
                "n_attempted": 100,
                "n_filled": 100,
                "n_rejected": 0,
                "n_market_lookup_failures": 1,  # 1% — under the 5% ceiling
                "schema_version": "1.1",
            }
        ),
        encoding="utf-8",
    )
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 100


def test_run_gate_2b_accepts_manifest_at_market_lookup_failure_threshold(
    tmp_path: Path,
) -> None:
    """Exactly at 5% → accepted (strict-greater-than rejection).

    The threshold is the inclusive ceiling: a capture at exactly 5%
    passes; one above 5% rejects. Documented justification: the 5%
    headroom is itself the noise budget, so the boundary case should
    be tolerated (otherwise float comparison + rounding could
    spuriously reject a clean capture sitting at the line).
    """
    import json

    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(20)]
    capture = tmp_path / "lookup_fail_at.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "lookup-fail-at",
                "n_attempted": 20,
                "n_filled": 20,
                "n_rejected": 0,
                "n_market_lookup_failures": 1,  # 5% exactly
                "schema_version": "1.1",
            }
        ),
        encoding="utf-8",
    )
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 20


def test_run_gate_2b_uses_n_filled_market_denominator_when_present(
    tmp_path: Path,
) -> None:
    """v1.2 manifest: ratio uses ``n_filled_market`` (market-only), not ``n_filled``.

    Reviewer-flagged MEDIUM (2026-05-14 fix v2 follow-up): the v1.1
    ``n_filled`` denominator mixed market + pending fills, which
    DILUTED the market-only failure rate. For a typical 60% market /
    40% pending capture mix, the v1.1 guard tolerated ~2x the
    market-only failure rate it documented. v1.2 publishes
    ``n_filled_market`` and the guard prefers it.

    Scenario: 4 lookup failures across 100 filled trades total, but
    only 20 of those are market (the rest are limit/stop fills).
    v1.1 would compute 4/100 = 4% PASS; v1.2 computes 4/20 = 20% REJECT.
    """
    import json

    from propfarm.gates.gate_2b import MAX_MARKET_LOOKUP_FAILURE_RATIO, run_gate_2b

    rows = [_make_row(idx=i) for i in range(100)]
    capture = tmp_path / "v12_market_denom.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "v12-market-denom",
                "n_attempted": 110,
                "n_filled": 100,  # diluted v1.1 denominator: 4/100=4% would PASS
                "n_filled_market": 20,  # strict v1.2 denominator: 4/20=20% REJECTS
                "n_rejected": 10,
                "n_market_lookup_failures": 4,
                "schema_version": "1.2",
            }
        ),
        encoding="utf-8",
    )
    # Sanity-check the numerics so the test is self-documenting.
    assert MAX_MARKET_LOOKUP_FAILURE_RATIO < 4 / 20  # 20% > 5% → reject expected
    assert MAX_MARKET_LOOKUP_FAILURE_RATIO > 4 / 100  # 4% would have passed under v1.1
    with pytest.raises(ValueError, match="n_filled_market=20"):
        run_gate_2b(capture_parquet_path=capture)


def test_run_gate_2b_falls_back_to_n_filled_denominator_for_v11_manifests(
    tmp_path: Path,
) -> None:
    """v1.1 manifest (no ``n_filled_market`` key) falls back to ``n_filled``.

    Forward-compat: v1.1 captures predate the market-only denominator
    and have no way to surface it without re-reading the parquet. The
    guard falls back to ``n_filled`` and the rejection message labels
    the denominator as ``v1.0/1.1 fallback — lenient`` so an auditor
    can trace why an old capture passed where the same numbers in v1.2
    would fail.
    """
    import json

    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(20)]
    capture = tmp_path / "v11_fallback.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "v11-fallback",
                "n_attempted": 25,
                "n_filled": 20,
                "n_rejected": 5,
                "n_market_lookup_failures": 4,  # 4/20=20% > 5% → reject
                "schema_version": "1.1",
                # NO n_filled_market key — pre-v1.2 manifest.
            }
        ),
        encoding="utf-8",
    )
    # The rejection message must surface the lenient-fallback denominator
    # so an auditor knows the v1.1 numbers were used (and that re-recording
    # under v1.2 would produce a stricter ratio for typical mixes).
    with pytest.raises(ValueError, match=r"n_filled .v1\.0/1\.1 fallback"):
        run_gate_2b(capture_parquet_path=capture)


def test_run_gate_2b_accepts_manifest_without_n_market_lookup_failures_key(
    tmp_path: Path,
) -> None:
    """Forward-compat: pre-v1.1 manifests lack the key → treated as 0.

    Existing Phase-0 manifests written under schema_version=1.0 do not
    include ``n_market_lookup_failures``. The loader treats missing as
    ``0`` so legacy captures pass the guard without re-recording.
    """
    import json

    from propfarm.gates.gate_2b import run_gate_2b

    rows = [_make_row(idx=i) for i in range(10)]
    capture = tmp_path / "v1_0_manifest.parquet"
    _write_capture(rows, capture)
    manifest_path = capture.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "legacy",
                "n_attempted": 10,
                "n_filled": 10,
                "n_rejected": 0,
                "schema_version": "1.0",
                # NO n_market_lookup_failures key — pre-v1.1 manifest.
            }
        ),
        encoding="utf-8",
    )
    report = run_gate_2b(capture_parquet_path=capture)
    assert report.n_rows_captured == 10


# --------------------------------------------------------------------------- #
# Gate-2B round 1 reviewer follow-ups (B1, B2, B3, B4)
# --------------------------------------------------------------------------- #
def test_gate_2b_per_symbol_p95_uses_market_only(tmp_path: Path) -> None:
    """B1: per-symbol fill_price p95 verdict filter must exclude non-market rows.

    Build a synthetic GBPUSD capture with three rows that all retcode-match:
    1 market row (small residual), 1 limit row, 1 stop row. The non-market
    rows carry a large fill_price_residual; if the verdict's p95 computation
    included them, it would trip the FAIL band even though only the market
    behavior is what the cost model is supposed to track.

    Reviewer note: the original gate filtered on ``sim_retcode==10009``
    only, which let limit/stop rows leak into the p95 because both order
    types also return 10009 on success. Fix: add ``order_type=='market'``
    to the filter (see ``_evaluate_verdict`` in gate_2b.py).
    """
    from propfarm.gates.gate_2b import ResidualDistribution, ResidualRow, _evaluate_verdict

    # Construct 3 ResidualRow objects directly so the test can probe the
    # verdict filter in isolation. The market row has a 0.1 pip residual
    # (well under the 0.5 pip threshold); the limit and stop rows carry
    # +5 pip residuals (would trip the 0.5x1.4=0.7 FAIL band if included).
    pip = 0.0001
    rows = [
        ResidualRow(
            run_id="r",
            request_time_utc=_START,
            symbol="GBPUSD",
            order_type="market",
            side="buy",
            fill_price_residual=0.1 * pip,
            slippage_residual_pips=0.0,
            spread_residual_pips=0.0,
            broker_latency_residual_ms=0.0,
            retcode_match=True,
            sim_retcode=10009,
            live_retcode=10009,
        ),
        ResidualRow(
            run_id="r",
            request_time_utc=_START,
            symbol="GBPUSD",
            order_type="limit",
            side="buy",
            fill_price_residual=5.0 * pip,  # would trip p95 if included
            slippage_residual_pips=0.0,
            spread_residual_pips=0.0,
            broker_latency_residual_ms=0.0,
            retcode_match=True,
            sim_retcode=10009,
            live_retcode=10009,
        ),
        ResidualRow(
            run_id="r",
            request_time_utc=_START,
            symbol="GBPUSD",
            order_type="stop",
            side="buy",
            fill_price_residual=5.0 * pip,  # would trip p95 if included
            slippage_residual_pips=0.0,
            spread_residual_pips=0.0,
            broker_latency_residual_ms=0.0,
            retcode_match=True,
            sim_retcode=10009,
            live_retcode=10009,
        ),
    ]
    empty_dist = ResidualDistribution(
        field_name="x",
        n=0,
        p50=math.nan,
        p95=math.nan,
        p99=math.nan,
        mean=math.nan,
        std=math.nan,
        t_stat=math.nan,
        p_value=math.nan,
        has_systematic_bias=False,
    )
    verdict, reasons = _evaluate_verdict(rows, {"fill_price": empty_dist})
    # If the limit + stop residuals had leaked in, p95 of [0.1, 5.0, 5.0] = 5.0
    # → > 0.5*1.4 = 0.7 fail_band → verdict=FAIL. The market-only filter
    # excludes them: p95 of [0.1] = 0.1 → PASS.
    combined = " | ".join(reasons)
    assert "fill_price_p95_exceeded" not in combined, (
        f"per-symbol p95 leaked non-market rows: reasons={reasons!r}"
    )
    assert "fill_price_p95_investigate" not in combined
    assert verdict == "pass", (
        f"expected verdict=pass with only the small market residual; "
        f"got {verdict!r}, reasons={reasons!r}"
    )

    # End-to-end smoke: build a full capture parquet with the same shape
    # and confirm the harness's run_gate_2b uses the same filter.
    parquet_rows = [
        _make_row(
            idx=0,
            symbol="GBPUSD",
            order_type="market",
            side="buy",
            requested_price=1.3,
            fill_price_offset_pips=0.1,
            spread_at_request_pips=0.3,
            slippage_observed_pips=0.1,
            retcode=10009,
        ),
        _make_row(
            idx=1,
            symbol="GBPUSD",
            order_type="limit",
            side="buy",
            requested_price=1.3,
            fill_price_offset_pips=5.0,
            spread_at_request_pips=0.3,
            slippage_observed_pips=5.0,
            retcode=10009,
        ),
        _make_row(
            idx=2,
            symbol="GBPUSD",
            order_type="stop",
            side="buy",
            requested_price=1.3,
            fill_price_offset_pips=5.0,
            spread_at_request_pips=0.3,
            slippage_observed_pips=5.0,
            retcode=10009,
        ),
    ]
    capture = tmp_path / "b1_market_only.parquet"
    _write_capture(parquet_rows, capture)
    report = run_gate_2b(capture)
    reasons_blob = " | ".join(report.failure_reasons)
    assert "fill_price_p95_exceeded:GBPUSD" not in reasons_blob, (
        f"end-to-end: market-only filter did not exclude limit/stop; "
        f"reasons={report.failure_reasons!r}"
    )


def test_gate_2b_latency_bias_is_advisory_not_fail(tmp_path: Path) -> None:
    """B2: latency_ms bias is reported but never adds to failure_reasons.

    Build a capture whose broker_latency_ms is strongly biased away from
    the sim's derived execution_latency_ms (median). The residual t-test
    on latency_ms WILL trip a low p-value (the residual mean is far from
    zero by construction), but the verdict must NOT FAIL solely because
    of it.

    Reviewer note: the sim derives execution_latency_ms from the median
    of the captured broker_latency_ms, so the residual distribution
    reflects the right-tail of the live latency distribution by
    construction — not a calibration target.
    """
    rows = []
    base_ts = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    # All rows have the same broker_latency_ms=150 except every 5th row
    # spikes to 300ms. Median = 150, residual on the 4-of-5 normal rows is
    # 0, and on the spike rows is +150ms. Mean of residuals: skewed positive.
    # The t-test will flag bias.
    for i in range(50):
        latency = 300.0 if i % 5 == 0 else 150.0
        rows.append(
            {
                "run_id": "b2-latency-advisory",
                "request_time_utc": base_ts + timedelta(minutes=i),
                "broker_fill_time_utc": base_ts + timedelta(minutes=i, milliseconds=latency),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "buy",
                "volume_lots": 0.01,
                "requested_price": 1.10000 + i * 0.00001,
                # Clean fill_price/spread so only latency is biased.
                "fill_price": 1.10000 + i * 0.00001,
                "spread_at_request_pips": 0.3,
                "slippage_observed_pips": 0.0,
                "broker_latency_ms": latency,
                "retcode": 10009,
                "comment": "",
            }
        )
    capture = tmp_path / "b2_latency_bias.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture)

    # The latency_ms distribution must be present.
    lat = report.residuals_by_field.get("latency_ms")
    assert lat is not None and lat.n > 0
    # No failure_reasons string mentions latency_ms.
    for reason in report.failure_reasons:
        assert "systematic_bias:latency_ms" not in reason, (
            f"latency_ms bias must not appear in failure_reasons (advisory-only): reason={reason!r}"
        )
        assert "systematic_bias_investigate:latency_ms" not in reason, (
            f"latency_ms bias must not appear in failure_reasons (advisory-only): reason={reason!r}"
        )


def test_gate_2b_emits_per_side_bias_pane(tmp_path: Path) -> None:
    """B3: report.per_side_bias is populated and surfaced in the markdown.

    Build a capture with ≥10 market rows on BOTH sides for a single
    symbol; assert (a) the per_side_bias tuple has the expected entries
    and (b) the markdown report carries the ``[per_side_bias:SYM:side ...]``
    tagged lines.
    """
    rows = []
    # 12 buys then 12 sells — both above the n>=10 inclusion threshold.
    for i in range(12):
        rows.append(_make_row(idx=i, side="buy", fill_price_offset_pips=0.05))
    for i in range(12, 24):
        rows.append(_make_row(idx=i, side="sell", fill_price_offset_pips=0.05))
    capture = tmp_path / "b3_per_side_bias.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture)

    # Pane has two rows: EURUSD buy and EURUSD sell.
    pane = {(r.symbol, r.side): r for r in report.per_side_bias}
    assert ("EURUSD", "buy") in pane
    assert ("EURUSD", "sell") in pane
    assert pane[("EURUSD", "buy")].n == 12
    assert pane[("EURUSD", "sell")].n == 12

    # Markdown report carries the tagged single-line representation.
    md = (capture.parent / f"{report.run_id}_report.md").read_text(encoding="utf-8")
    assert "[per_side_bias:EURUSD:buy" in md
    assert "[per_side_bias:EURUSD:sell" in md

    # Reporting-only: per-side bias does not contribute to failure_reasons.
    for reason in report.failure_reasons:
        assert "per_side_bias" not in reason, (
            f"per_side_bias should not appear in failure_reasons "
            f"(reporting-only in round 1): {reason!r}"
        )


def test_gate_2b_per_side_bias_skips_under_min_n(tmp_path: Path) -> None:
    """B3: (symbol, side) pairs with n < min_n are omitted from the pane.

    A capture with only 5 rows per side on a symbol must not surface that
    symbol's per-side bias — the sample is too thin for a meaningful
    t-test.
    """
    from propfarm.gates.gate_2b import _PER_SIDE_BIAS_MIN_N

    rows = []
    # 5 buys + 5 sells — below the n>=10 threshold.
    for i in range(5):
        rows.append(_make_row(idx=i, side="buy"))
    for i in range(5, 10):
        rows.append(_make_row(idx=i, side="sell"))
    capture = tmp_path / "b3_min_n.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture)
    for r in report.per_side_bias:
        assert r.n >= _PER_SIDE_BIAS_MIN_N
    # Specifically, neither (EURUSD,buy) nor (EURUSD,sell) should appear.
    pane = {(r.symbol, r.side) for r in report.per_side_bias}
    assert pane == set()


def test_gate_2b_investigate_band_threshold_logic(tmp_path: Path) -> None:
    """B4: INVESTIGATE band — p95 between threshold and fail_band → INVESTIGATE.

    Construct a synthetic capture where the per-symbol fill_price p95
    lands strictly in the INVESTIGATE band (threshold < p95 ≤ threshold
    x 1.4) AND the per-field t-tests do NOT trip systematic_bias FAIL
    (mean residual ≈ 0). Assert the verdict is INVESTIGATE and the
    failure_reasons surface an ``_investigate`` tag.

    Construction: 10 BUY rows + 10 SELL rows, each with a sign-symmetric
    ±0.6 pip fill_price residual. The mean fill_price residual cancels to
    ~0 (no t-test FAIL), but the magnitude p95 sits at 0.6 pip in the
    (0.5, 0.7] INVESTIGATE band on EURUSD. Slippage and spread are
    pinned to the sim's deterministic values (post Gate-2B round 1
    calibration) so those channels produce zero-residual rows and don't
    interfere with the verdict.
    """

    from propfarm.sim.fill_engine import _bps_to_pips
    from propfarm.sim.market import MarketState as _MS
    from propfarm.sim.slippage import SlippageRequest as _SR
    from propfarm.sim.slippage import evaluate as _eval_slip
    from propfarm.sim.spread import evaluate as _eval_spread

    # Compute sim slip + sim spread for the canonical synthetic regime so
    # the live rows can mirror them (zero residual on those channels).
    ts0 = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    _state = _MS(symbol="EURUSD", ts_utc=ts0, realized_vol_5m=0.10)
    sim_slip_pips = _eval_slip(
        _state, _SR(side="buy", order_type="market", size_lots=0.01), rng=None
    ).slippage_pips
    sim_spread_pips = _bps_to_pips(_eval_spread(_state).spread_bps, 1.10000, 0.0001)

    rows = []
    pip = 0.0001
    n_buys = 10
    n_sells = 10
    # Use a fixed requested price across all rows so the sim spread (which
    # converts bps via the requested price) is invariant — residual = 0.
    fixed_requested = 1.10000
    for i in range(n_buys):
        ts = ts0 + timedelta(minutes=i)
        rows.append(
            {
                "run_id": "b4-investigate-band",
                "request_time_utc": ts,
                "broker_fill_time_utc": ts + timedelta(milliseconds=20),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "buy",
                "volume_lots": 0.01,
                "requested_price": fixed_requested,
                # BUY: live fill 0.6 pip ABOVE sim → fill_price_residual = +0.6 pip.
                "fill_price": fixed_requested + (sim_slip_pips + 0.6) * pip,
                # Live spread = sim spread → spread residual = 0.
                "spread_at_request_pips": sim_spread_pips,
                # Live slip = sim slip → slip residual = 0. Decoupling
                # slip from fill_price keeps the slip channel quiet so
                # the t-test doesn't trip on aligned BUY/SELL slip means.
                "slippage_observed_pips": sim_slip_pips,
                "broker_latency_ms": 20.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    for j in range(n_sells):
        i = n_buys + j
        ts = ts0 + timedelta(minutes=i)
        rows.append(
            {
                "run_id": "b4-investigate-band",
                "request_time_utc": ts,
                "broker_fill_time_utc": ts + timedelta(milliseconds=20),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "sell",
                "volume_lots": 0.01,
                "requested_price": fixed_requested,
                # SELL: live fill 0.6 pip BELOW sim → fill_price_residual = -0.6 pip.
                "fill_price": fixed_requested - (sim_slip_pips + 0.6) * pip,
                "spread_at_request_pips": sim_spread_pips,
                "slippage_observed_pips": sim_slip_pips,
                "broker_latency_ms": 20.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    capture = tmp_path / "b4_investigate_band.parquet"
    _write_capture(rows, capture)
    report = run_gate_2b(capture)
    assert report.verdict == "investigate", (
        f"expected verdict=investigate with p95 in the (0.5, 0.7] band; "
        f"got {report.verdict!r}, reasons={report.failure_reasons!r}"
    )
    reasons_blob = " | ".join(report.failure_reasons)
    assert "fill_price_p95_investigate:EURUSD" in reasons_blob, (
        f"expected fill_price_p95_investigate tag in reasons; got {report.failure_reasons!r}"
    )
    # No FAIL-level reasons should be present.
    assert not any(
        ("fill_price_p95_exceeded" in r and "investigate" not in r)
        or (r.startswith("systematic_bias:") and "investigate" not in r)
        for r in report.failure_reasons
    ), f"INVESTIGATE verdict should not carry FAIL reasons: {report.failure_reasons!r}"


def test_gate_2b_investigate_verdict_cli_exit_code_2(tmp_path: Path) -> None:
    """B4: CLI exit code is 2 on INVESTIGATE (distinct from 1 for FAIL).

    Reuses the same INVESTIGATE-band construction as
    ``test_gate_2b_investigate_band_threshold_logic`` and runs the CLI
    via subprocess.
    """
    import subprocess
    import sys as _sys

    from propfarm.sim.fill_engine import _bps_to_pips
    from propfarm.sim.market import MarketState as _MS
    from propfarm.sim.slippage import SlippageRequest as _SR
    from propfarm.sim.slippage import evaluate as _eval_slip
    from propfarm.sim.spread import evaluate as _eval_spread

    ts0 = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    _state = _MS(symbol="EURUSD", ts_utc=ts0, realized_vol_5m=0.10)
    sim_slip_pips = _eval_slip(
        _state, _SR(side="buy", order_type="market", size_lots=0.01), rng=None
    ).slippage_pips
    sim_spread_pips = _bps_to_pips(_eval_spread(_state).spread_bps, 1.10000, 0.0001)

    rows = []
    pip = 0.0001
    fixed_requested = 1.10000
    for i in range(10):
        ts = ts0 + timedelta(minutes=i)
        rows.append(
            {
                "run_id": "b4-cli-exit",
                "request_time_utc": ts,
                "broker_fill_time_utc": ts + timedelta(milliseconds=20),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "buy",
                "volume_lots": 0.01,
                "requested_price": fixed_requested,
                "fill_price": fixed_requested + (sim_slip_pips + 0.6) * pip,
                "spread_at_request_pips": sim_spread_pips,
                "slippage_observed_pips": sim_slip_pips,
                "broker_latency_ms": 20.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    for j in range(10):
        i = 10 + j
        ts = ts0 + timedelta(minutes=i)
        rows.append(
            {
                "run_id": "b4-cli-exit",
                "request_time_utc": ts,
                "broker_fill_time_utc": ts + timedelta(milliseconds=20),
                "symbol": "EURUSD",
                "order_type": "market",
                "side": "sell",
                "volume_lots": 0.01,
                "requested_price": fixed_requested,
                "fill_price": fixed_requested - (sim_slip_pips + 0.6) * pip,
                "spread_at_request_pips": sim_spread_pips,
                "slippage_observed_pips": sim_slip_pips,
                "broker_latency_ms": 20.0,
                "retcode": 10009,
                "comment": "",
            }
        )
    capture = tmp_path / "b4_cli_exit.parquet"
    _write_capture(rows, capture)
    cli_path = Path(__file__).resolve().parents[2] / "scripts" / "run_gate_2b.py"
    result = subprocess.run(
        [_sys.executable, str(cli_path), "--capture-parquet", str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, (
        f"expected CLI exit code 2 on INVESTIGATE; got {result.returncode}. "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_slippage_calibration_eurusd_post_2026_05_18_capture() -> None:
    """Round-1 calibration cross-check: EURUSD post-cal sim slip ≈ live mean.

    Per the dispatcher's directive, "the calibrated values land within
    ±0.1 pip of the live observed slippage". Live mean slip on EURUSD
    markets is ~+0.016 pips; the new calibration with base_pips=0 and
    vol_coef=0 produces a sim slip of ~size_coef * log(1.01) ≈ 0.005 pips
    at 0.01 lot — well within the ±0.1 pip tolerance.
    """
    from propfarm.sim.slippage import CALIBRATIONS as S
    from propfarm.sim.slippage import SlippageRequest, evaluate

    cal = S["EURUSD"]
    state = MarketState(
        symbol="EURUSD",
        ts_utc=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        realized_vol_5m=0.10,
        news_window=False,
        stress_mode=False,
    )
    req = SlippageRequest(side="buy", order_type="market", size_lots=0.01)
    result = evaluate(state, req, calibration=cal, rng=None)
    # Empirical live mean was 0.016 pips. Tolerance ±0.1 pip per dispatcher.
    assert abs(result.slippage_pips - 0.016) <= 0.1, (
        f"EURUSD post-calibration sim slip {result.slippage_pips} pips "
        f"deviates from live mean 0.016 pips by more than ±0.1 pip"
    )


def test_spread_calibration_eurusd_post_2026_05_18_capture() -> None:
    """Round-1 calibration cross-check: EURUSD post-cal sim spread ≈ live mean.

    Live mean spread on EURUSD was ~0.384 pips (typically 0.2-0.4 in
    mid-session); the new baseline of 0.29 bps at a 1.16 mid converts
    to ~0.336 pips of sim spread — within ±0.1 pip of the live mean.
    """
    from propfarm.sim.spread import CALIBRATIONS as SP
    from propfarm.sim.spread import evaluate as evaluate_sp

    cal = SP["EURUSD"]
    state = MarketState(
        symbol="EURUSD",
        ts_utc=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        realized_vol_5m=0.10,
        news_window=False,
        stress_mode=False,
    )
    result = evaluate_sp(state, calibration=cal)
    # Convert bps to pips at the reference price (1.16 mid; pip=0.0001).
    sim_spread_pips = result.spread_bps * 1.16 * 1e-4 / 0.0001
    live_mean_pips = 0.384
    assert abs(sim_spread_pips - live_mean_pips) <= 0.1, (
        f"EURUSD post-calibration sim spread {sim_spread_pips} pips "
        f"deviates from live mean {live_mean_pips} pips by more than ±0.1 pip"
    )
