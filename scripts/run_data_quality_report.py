"""Generate the vendor-reconciliation data-quality report (Task 5.3).

Runs :func:`propfarm.data.reconcile.reconcile_snapshots` for every Phase-0
symbol, writes a Markdown report under ``docs/``, and exits non-zero if
any symbol has at least one finding above the 5-bps threshold.

Usage::

    python scripts/run_data_quality_report.py
    python scripts/run_data_quality_report.py --threshold-bps 3.0
    python scripts/run_data_quality_report.py --output docs/data-quality-2026-05.md

The report contains:

* A snapshot source table (which snapshot names + roots fed each symbol).
* A per-symbol summary table (n_compared / n_findings / p50 / p95 / p99 / max).
* For each symbol with findings, the top-50 disagreements sorted by
  ``abs_diff_bps`` descending.

Exit code:

* 0 — every symbol returned ``n_findings == 0``.
* 1 — at least one symbol returned at least one finding.

The script is offline-safe: snapshots are read locally via
:func:`propfarm.data.snapshot.load_snapshot`, which hash-verifies the
parquet bytes. If a snapshot is missing or its hash drifted, the script
fails loudly with the underlying error rather than silently producing a
report on incomplete data.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from pathlib import Path

import typer

from propfarm.data.quality import SUPPORTED_SYMBOLS
from propfarm.data.reconcile import (
    ReconciliationFinding,
    ReconciliationReport,
    reconcile_snapshots,
)

app = typer.Typer(add_completion=False, help=__doc__)


# Module-level Typer option singletons (ruff B008 / typer convention).
_OUTPUT_OPT = typer.Option(
    Path("docs/data-quality-report-2026-05.md"),
    help="Markdown output path; parents are created if missing.",
)
_THRESHOLD_OPT = typer.Option(
    5.0,
    help="Per-field bps threshold above which a minute is flagged.",
)
_MAX_FINDINGS_OPT = typer.Option(
    1000,
    help="Hard cap on findings retained per symbol (sorted by bps desc).",
)
_SYMBOLS_OPT = typer.Option(
    None,
    help=(
        "Comma-separated subset of Phase-0 symbols to reconcile. "
        "Defaults to every symbol in SUPPORTED_SYMBOLS."
    ),
)
_SNAPSHOT_ROOT_OPT = typer.Option(
    None,
    help="Optional snapshot root override (forwards to load_snapshot).",
)


# --------------------------------------------------------------------------- #
# Markdown formatting helpers
# --------------------------------------------------------------------------- #
def _format_summary_table(reports: list[ReconciliationReport]) -> str:
    """Per-symbol summary table."""
    lines = [
        "| Symbol | n_minutes_compared | n_minutes_market_open | n_findings | "
        "p50 bps | p95 bps | p99 bps | max bps |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in reports:
        lines.append(
            f"| {r.symbol} | {r.n_minutes_compared} | {r.n_minutes_market_open} | "
            f"{r.n_findings} | {r.p50_diff_bps:.3f} | {r.p95_diff_bps:.3f} | "
            f"{r.p99_diff_bps:.3f} | {r.max_diff_bps:.3f} |"
        )
    return "\n".join(lines)


def _format_findings_table(findings: tuple[ReconciliationFinding, ...]) -> str:
    """Top-50 findings table for one symbol."""
    if not findings:
        return "_No findings above threshold._"
    lines = [
        "| ts_utc | field | dukascopy | histdata | abs_diff_bps |",
        "|---|---|---:|---:|---:|",
    ]
    for f in findings[:50]:
        lines.append(
            f"| {f.ts_utc.isoformat()} | {f.field} | "
            f"{f.dukascopy_value:.6f} | {f.histdata_value:.6f} | "
            f"{f.abs_diff_bps:.3f} |"
        )
    return "\n".join(lines)


def _format_source_table(
    symbols: list[str],
    snapshot_root: Path | None,
) -> str:
    """Snapshot-source table — which (name, root) tuple fed each symbol."""
    root_str = str(snapshot_root) if snapshot_root is not None else "(repo root)"
    lines = [
        "| Symbol | Dukascopy snapshot | HistData snapshot | snapshot root |",
        "|---|---|---|---|",
    ]
    for s in symbols:
        duka = f"dukascopy_{s.lower()}_ticks"
        hd = f"histdata_{s.lower()}_1m"
        lines.append(f"| {s} | `{duka}` | `{hd}` | `{root_str}` |")
    return "\n".join(lines)


def _format_report(
    reports: list[ReconciliationReport],
    errors: list[tuple[str, str]],
    symbols: list[str],
    snapshot_root: Path | None,
    threshold_bps: float,
) -> str:
    """Assemble the full Markdown report body."""
    now = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
    chunks: list[str] = [
        "# Vendor Reconciliation Report (Task 5.3)",
        "",
        f"_Generated: {now}_  ",
        f"_Threshold: {threshold_bps:.2f} bps (per-field, OHLC)_",
        "",
        "Dukascopy ticks are aggregated to 1-minute OHLC on mid price "
        "(`mid = (bid + ask) / 2`) and inner-joined against HistData's "
        "1-minute OHLC. The bps formula is `|a - b| / mid * 10_000` where "
        "`mid = (a + b) / 2`. Market-closed minutes are filtered via "
        "`propfarm.data.quality.is_market_open` before comparison.",
        "",
        "## Snapshot sources",
        "",
        _format_source_table(symbols, snapshot_root),
        "",
        "## Per-symbol summary",
        "",
        _format_summary_table(reports),
        "",
    ]

    if errors:
        chunks.extend(
            [
                "## Symbols that failed to reconcile",
                "",
                "| Symbol | Error |",
                "|---|---|",
            ]
        )
        chunks.extend(f"| {sym} | `{err}` |" for sym, err in errors)
        chunks.append("")

    chunks.append("## Findings (top 50 by abs_diff_bps desc)")
    chunks.append("")
    for r in reports:
        chunks.append(f"### {r.symbol}")
        chunks.append("")
        chunks.append(_format_findings_table(r.findings))
        chunks.append("")
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
@app.command()
def run(
    output: Path = _OUTPUT_OPT,
    threshold_bps: float = _THRESHOLD_OPT,
    max_findings: int = _MAX_FINDINGS_OPT,
    symbols: str | None = _SYMBOLS_OPT,
    snapshot_root: Path | None = _SNAPSHOT_ROOT_OPT,
) -> None:
    """Reconcile every Phase-0 symbol and write a Markdown report."""
    symbol_list: list[str]
    if symbols is None or symbols.strip() == "":
        symbol_list = list(SUPPORTED_SYMBOLS)
    else:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        unknown = [s for s in symbol_list if s not in SUPPORTED_SYMBOLS]
        if unknown:
            raise typer.BadParameter(
                f"unknown symbols: {unknown}; supported: {list(SUPPORTED_SYMBOLS)}"
            )

    reports: list[ReconciliationReport] = []
    errors: list[tuple[str, str]] = []

    for sym in symbol_list:
        typer.echo(f"Reconciling {sym}...")
        try:
            report = reconcile_snapshots(
                symbol=sym,
                snapshot_root=snapshot_root,
                threshold_bps=threshold_bps,
                max_findings=max_findings,
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            # Snapshot missing / manifest missing / unknown symbol — record
            # and keep going so a partial report still ships.
            errors.append((sym, f"{type(exc).__name__}: {exc}"))
            typer.echo(f"  FAILED: {exc}", err=True)
            continue
        except Exception as exc:
            # Anything else (e.g. SnapshotIntegrityError) is recorded but
            # also surfaces in stderr with a traceback for fast diagnosis.
            errors.append((sym, f"{type(exc).__name__}: {exc}"))
            typer.echo(f"  FAILED: {exc}", err=True)
            traceback.print_exc()
            continue
        reports.append(report)
        typer.echo(
            f"  n_compared={report.n_minutes_compared} "
            f"n_findings={report.n_findings} "
            f"p95={report.p95_diff_bps:.3f} bps "
            f"max={report.max_diff_bps:.3f} bps"
        )

    body = _format_report(reports, errors, symbol_list, snapshot_root, threshold_bps)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    typer.echo(f"Wrote {output}")

    # Exit 1 if ANY symbol has findings OR any errored out — both indicate
    # a non-green data-quality state that should block downstream backtests.
    any_findings = any(r.n_findings > 0 for r in reports)
    any_errors = bool(errors)
    if any_findings or any_errors:
        raise typer.Exit(code=1)


# Re-export so tests / external callers can hit the formatter without
# spinning up the CLI machinery.
__all__ = [
    "_format_report",
    "app",
    "run",
]


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    app()
