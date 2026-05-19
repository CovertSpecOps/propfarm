"""Ad-hoc CLI for Gate 2B (Phase 0, Task 14.3 Part B).

Usage::

    python scripts/run_gate_2b.py --capture-parquet data/raw/fill_recordings/{run_id}.parquet
    python scripts/run_gate_2b.py --capture-parquet ... --execution-latency-ms 170

Exit codes (Gate-2B round 1 — reviewer follow-up B4):

* 0 - verdict == "pass" (cost models well-calibrated against live; Wave 6d unblocks)
* 1 - verdict == "fail" (at least one FAIL-band condition tripped)
* 2 - verdict == "investigate" (a metric is in the INVESTIGATE band — no FAIL)
* 3 - error before the gate could run (missing parquet, schema mismatch, etc.)

Round-1 changes vs the original CLI:

* Previously INVESTIGATE and FAIL both mapped to exit 1. Now INVESTIGATE has
  its own dedicated exit code (2) so CI can route INVESTIGATE differently
  from FAIL (e.g. INVESTIGATE → soft-fail merge, FAIL → hard-block).
* The startup-error exit code moved from 2 to 3 to make room.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from propfarm.gates.gate_2b import Gate2BReport, run_gate_2b

app = typer.Typer(add_completion=False, no_args_is_help=True)


# Module-level Typer option singletons (ruff B008 / typer convention; same
# pattern as scripts/run_data_quality_report.py and scripts/ingest_to_snapshot.py).
_CAPTURE_OPT = typer.Option(
    ...,
    "--capture-parquet",
    help="Path to the parquet produced by scripts/record_fills.py.",
)
_OUTPUT_DIR_OPT = typer.Option(
    None,
    "--output-dir",
    help=(
        "Override directory for the residuals parquet + markdown. "
        "Defaults to the capture parquet's directory."
    ),
)
_LATENCY_OPT = typer.Option(
    None,
    "--execution-latency-ms",
    help=(
        "Override the fill engine's execution latency (ms). Defaults to the "
        "median of the captured broker_latency_ms on successful rows."
    ),
)


def _print_report(report: Gate2BReport) -> None:
    """Print a human-readable summary; the full report lives in the markdown file."""
    lines: list[str] = [
        "=== Gate 2B (Task 14.3 Part B) sim-vs-live fill comparison ===",
        f"verdict:                {report.verdict.upper()}",
        f"run_id:                 {report.run_id}",
        f"capture parquet:        {report.capture_parquet_path}",
        f"capture SHA256:         {report.capture_parquet_sha256}",
        f"rows captured:          {report.n_rows_captured}",
        f"rows compared:          {report.n_rows_compared}",
        f"retcode matches:        {report.n_retcode_matches}",
        "",
        "Residual distributions:",
    ]
    for field_name, dist in report.residuals_by_field.items():
        bias = "BIAS" if dist.has_systematic_bias else "ok"
        lines.append(
            f"  {field_name:14s} n={dist.n:4d} "
            # p50/p95/p99 come from abs(residual) in _residual_distribution
            # and are always non-negative, so don't print a misleading "+".
            f"p50={dist.p50:.6f} p95={dist.p95:.6f} p99={dist.p99:.6f} "
            f"mean={dist.mean:+.6f} t={dist.t_stat:+.3f} p={dist.p_value:.4f} [{bias}]"
        )
    if report.failure_reasons:
        lines.append("")
        lines.append("Failure reasons:")
        for reason in report.failure_reasons:
            lines.append(f"  - {reason}")
    for line in lines:
        typer.echo(line)


#: CLI verdict → exit-code map. Round-1 broke INVESTIGATE out from FAIL so
#: CI can route the two outcomes differently (INVESTIGATE → soft-fail merge,
#: FAIL → hard-block).
_VERDICT_EXIT_CODE: dict[str, int] = {
    "pass": 0,
    "fail": 1,
    "investigate": 2,
}


@app.command()
def main(
    capture_parquet: Path = _CAPTURE_OPT,
    output_dir: Path | None = _OUTPUT_DIR_OPT,
    execution_latency_ms: float | None = _LATENCY_OPT,
) -> None:
    """Run Gate 2B against a captured parquet.

    Exit codes: 0=PASS, 1=FAIL, 2=INVESTIGATE, 3=startup error.
    """
    try:
        capture = capture_parquet.resolve()
        if not capture.exists():
            typer.echo(f"ERROR: capture parquet not found: {capture}", err=True)
            raise typer.Exit(code=3)
        output_parquet_path: Path | None
        output_markdown_path: Path | None
        if output_dir is not None:
            resolved_output_dir = output_dir.resolve()
            resolved_output_dir.mkdir(parents=True, exist_ok=True)
            output_parquet_path = resolved_output_dir / f"{capture.stem}_residuals.parquet"
            output_markdown_path = resolved_output_dir / f"{capture.stem}_report.md"
        else:
            output_parquet_path = None
            output_markdown_path = None
        report = run_gate_2b(
            capture_parquet_path=capture,
            output_parquet_path=output_parquet_path,
            output_markdown_path=output_markdown_path,
            execution_latency_ms=execution_latency_ms,
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"ERROR: gate execution raised: {exc!r}", err=True)
        raise typer.Exit(code=3) from exc

    _print_report(report)
    sys.exit(_VERDICT_EXIT_CODE[report.verdict])


__all__ = ["app", "main"]


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    app()
