"""CLI: ingest raw Dukascopy ``.bi5`` archives into content-hashed snapshots.

Walks ``data/raw/dukascopy/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5``,
groups every hour by ``(symbol, calendar-year, calendar-month)``, and writes
one Parquet snapshot per partition via
:func:`propfarm.data.snapshot.write_snapshot`. The 0-indexed-month quirk is
absorbed in :func:`propfarm.data.ingest.discover_plans`; this CLI never sees it.

Examples
--------
Discover what would be ingested without writing anything::

    python scripts/ingest_to_snapshot.py --dry-run

Ingest just EURUSD for 2024::

    python scripts/ingest_to_snapshot.py --symbol EURUSD --year-min 2024 --year-max 2024

Force a re-ingest (overwrites existing snapshots and manifest entries)::

    python scripts/ingest_to_snapshot.py --force
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from propfarm.data.ingest import (
    PHASE0_SYMBOLS,
    IngestResult,
    discover_plans,
    ingest_plan,
)

app = typer.Typer(add_completion=False, help=__doc__)
_console = Console()

# Ruff B008 + Typer's recommended-default pattern: hoist sentinels to module scope.
_RAW_ROOT_OPT = typer.Option(
    Path("data/raw/dukascopy"),
    "--raw-root",
    help="Root of the Dukascopy raw cache; default 'data/raw/dukascopy'.",
)
_SNAPSHOT_ROOT_OPT = typer.Option(
    None,
    "--snapshot-root",
    help="Override the snapshot/manifest root; default is the repo root.",
)
_SYMBOL_OPT = typer.Option(
    None,
    "--symbol",
    help=(
        "Symbol to ingest. Repeat for multiple symbols. "
        f"Default: all six Phase-0 symbols ({', '.join(PHASE0_SYMBOLS)})."
    ),
)
_YEAR_MIN_OPT = typer.Option(
    None,
    "--year-min",
    help="Lowest calendar year to ingest (inclusive). Default: no lower bound.",
)
_YEAR_MAX_OPT = typer.Option(
    None,
    "--year-max",
    help="Highest calendar year to ingest (inclusive). Default: no upper bound.",
)
_DRY_RUN_OPT = typer.Option(
    False, "--dry-run", help="Print the IngestPlan list without writing snapshots."
)
_FORCE_OPT = typer.Option(
    False,
    "--force",
    help="Re-ingest every partition even if the manifest already has an entry.",
)


def _resolve_year_range(year_min: int | None, year_max: int | None) -> tuple[int, int] | None:
    """Combine optional year bounds into a single (min, max) tuple or ``None``."""
    if year_min is None and year_max is None:
        return None
    lo = year_min if year_min is not None else 0
    hi = year_max if year_max is not None else 9999
    if lo > hi:
        raise typer.BadParameter(f"--year-min ({lo}) must be <= --year-max ({hi}).")
    return (lo, hi)


def _resolve_symbols(symbols: list[str] | None) -> tuple[str, ...]:
    """Return the symbol allowlist; default to ``PHASE0_SYMBOLS`` if empty/None."""
    if not symbols:
        return PHASE0_SYMBOLS
    return tuple(symbols)


def _render_results(results: list[IngestResult], *, dry_run: bool) -> None:
    """Print a rich table summarizing each plan/result."""
    title = "Ingest plans (dry-run)" if dry_run else "Ingest results"
    table = Table(title=title, show_lines=False)
    table.add_column("symbol")
    table.add_column("year-month")
    table.add_column("files", justify="right")
    table.add_column("rows", justify="right")
    table.add_column("sha256")
    table.add_column("status")
    for r in results:
        ym = f"{r.plan.year:04d}-{r.plan.month:02d}"
        sha_short = r.snapshot_sha256[:12] if r.snapshot_sha256 else "-"
        if dry_run:
            status = "planned"
            rows_cell = "-"
        else:
            status = "skipped" if r.skipped else "written"
            rows_cell = str(r.row_count)
        table.add_row(
            r.plan.symbol,
            ym,
            str(len(r.plan.bi5_files)),
            rows_cell,
            sha_short,
            status,
        )
    _console.print(table)


@app.command()
def ingest(
    raw_root: Path = _RAW_ROOT_OPT,
    snapshot_root: Path | None = _SNAPSHOT_ROOT_OPT,
    symbol: list[str] | None = _SYMBOL_OPT,
    year_min: int | None = _YEAR_MIN_OPT,
    year_max: int | None = _YEAR_MAX_OPT,
    dry_run: bool = _DRY_RUN_OPT,
    force: bool = _FORCE_OPT,
) -> None:
    """Ingest raw Dukascopy ``.bi5`` files into content-hashed snapshots."""
    symbols = _resolve_symbols(symbol)
    year_range = _resolve_year_range(year_min, year_max)

    plans = discover_plans(raw_root, symbols=symbols, year_range=year_range)
    if not plans:
        typer.echo(
            f"No ingest plans discovered under {raw_root} for symbols={list(symbols)} "
            f"year_range={year_range}."
        )
        return

    if dry_run:
        planned = [
            IngestResult(
                plan=p,
                row_count=0,
                snapshot_sha256="",
                snapshot_path=(f"data/snapshots/{p.snapshot_name}/{p.partition}.parquet"),
                skipped=False,
            )
            for p in plans
        ]
        _render_results(planned, dry_run=True)
        return

    results: list[IngestResult] = []
    for p in plans:
        result = ingest_plan(p, snapshot_root=snapshot_root, skip_if_exists=not force)
        results.append(result)
    _render_results(results, dry_run=False)


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    app()
