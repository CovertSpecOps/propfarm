"""Ad-hoc CLI wrapper around :func:`propfarm.data.vendors.dukascopy.fetch_ticks`.

Intended for one-off pulls (a few minutes/hours of one symbol). The bulk
historical fetch (six symbols by 11 years) lives in Task 3.3 and runs
overnight from a different script.

Example::

    python scripts/download_dukascopy.py \\
        --symbol EURUSD \\
        --start 2024-01-02T10:00:00Z \\
        --end   2024-01-02T11:00:00Z \\
        --output /tmp/eurusd_one_hour.parquet
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from propfarm.data.vendors.dukascopy import fetch_ticks

app = typer.Typer(add_completion=False, help=__doc__)

# Typer recommends per-arg `typer.Option(...)` defaults; ruff's B008 wants
# module-level singletons. Define the singletons here to keep both happy.
_SYMBOL_OPT = typer.Option(..., help="Dukascopy instrument code, e.g. EURUSD.")
_START_OPT = typer.Option(..., help="UTC start, ISO-8601 (e.g. 2024-01-02T10:00:00Z).")
_END_OPT = typer.Option(..., help="UTC end, ISO-8601 (exclusive).")
_OUTPUT_OPT = typer.Option(..., help="Output Parquet path.")


def _parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp and coerce it to timezone-aware UTC."""
    # ``fromisoformat`` accepts trailing ``Z`` only on 3.11+; we're on 3.12.
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise typer.BadParameter(
            f"{raw!r} is a naive datetime; provide a UTC offset (e.g. 'Z' or '+00:00')"
        )
    return ts.astimezone(UTC)


@app.command()
def download(
    symbol: str = _SYMBOL_OPT,
    start: str = _START_OPT,
    end: str = _END_OPT,
    output: Path = _OUTPUT_OPT,
) -> None:
    """Fetch ticks for [start, end) and write a Parquet file."""
    start_utc = _parse_iso_utc(start)
    end_utc = _parse_iso_utc(end)
    if end_utc <= start_utc:
        raise typer.BadParameter("--end must be strictly after --start")

    typer.echo(f"Fetching {symbol} ticks for [{start_utc.isoformat()}, {end_utc.isoformat()})...")
    df = fetch_ticks(symbol, start_utc, end_utc)
    output.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(output)
    typer.echo(f"Wrote {df.height} ticks to {output}")


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    app()
