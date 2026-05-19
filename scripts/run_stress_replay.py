"""Operator CLI for Wave 6d stress replay (Task 10.2).

Usage::

    python scripts/run_stress_replay.py
    python scripts/run_stress_replay.py --window snb_2015
    python scripts/run_stress_replay.py --json

Drives the calibrated cost-model + fill-engine pipeline through the five
mandated historical stress windows (Lehman 2008, SNB 2015, COVID 2020,
Gilt 2022, SVB 2023) and prints a per-window summary table.

Exit codes:

* 0 — every window's acceptance criteria met
  (fills_with_nan == 0, fills_with_negative_price == 0,
   fills_outside_bid_ask == 0).
* 1 — at least one window failed an acceptance criterion. The stderr
  message identifies which window(s) and which criterion. **STOP and
  escalate** per the Phase-0 gating spec — do not widen tolerance.
"""

from __future__ import annotations

import json
import sys
from typing import Final

import typer

from propfarm.sim.stress_replay import (
    STRESS_WINDOWS,
    StressReplayResult,
    run_all_stress_windows,
    run_stress_replay,
)

app = typer.Typer(add_completion=False, no_args_is_help=False)

_WINDOW_OPT: Final = typer.Option(
    None,
    "--window",
    help=(
        "Run only the named window (one of: lehman_2008, snb_2015, "
        "covid_2020, gilt_2022, svb_2023). Default: run all five."
    ),
)
_JSON_OPT: Final = typer.Option(
    False,
    "--json",
    help="Emit results as a JSON array instead of the operator table.",
)


def _format_row(r: StressReplayResult) -> str:
    """One-line per-window summary for the operator table."""
    return (
        f"  {r.window.name:14s} {r.window.symbol:6s} "
        f"attempted={r.n_fills_attempted:4d} clean={r.n_fills_clean:4d} "
        f"spread p50={r.spread_p50_pips:7.2f} p95={r.spread_p95_pips:7.2f} "
        f"p99={r.spread_p99_pips:7.2f}  slip p99={r.slippage_p99_pips:6.2f} "
        f"nan={r.fills_with_nan} neg={r.fills_with_negative_price} "
        f"out={r.fills_outside_bid_ask}"
    )


def _failures(results: tuple[StressReplayResult, ...]) -> list[str]:
    """Return a list of acceptance-criterion failure messages, empty if all pass."""
    out: list[str] = []
    for r in results:
        if r.fills_with_nan != 0:
            out.append(f"{r.window.name}: fills_with_nan={r.fills_with_nan}")
        if r.fills_with_negative_price != 0:
            out.append(f"{r.window.name}: fills_with_negative_price={r.fills_with_negative_price}")
        if r.fills_outside_bid_ask != 0:
            out.append(f"{r.window.name}: fills_outside_bid_ask={r.fills_outside_bid_ask}")
    return out


@app.command()
def main(
    window: str | None = _WINDOW_OPT,
    json_out: bool = _JSON_OPT,
) -> None:
    """Run the stress replay and print a per-window summary."""
    results: tuple[StressReplayResult, ...]
    if window is not None:
        matches = [w for w in STRESS_WINDOWS if w.name == window]
        if not matches:
            typer.secho(
                f"Unknown window {window!r}. Known: {[w.name for w in STRESS_WINDOWS]}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)
        results = (run_stress_replay(matches[0]),)
    else:
        results = run_all_stress_windows()

    if json_out:
        rows = [
            {
                "name": r.window.name,
                "symbol": r.window.symbol,
                "start_utc": r.window.start_utc.isoformat(),
                "end_utc": r.window.end_utc.isoformat(),
                "data_source": r.window.data_source,
                "n_fills_attempted": r.n_fills_attempted,
                "n_fills_clean": r.n_fills_clean,
                "spread_p50_pips": r.spread_p50_pips,
                "spread_p95_pips": r.spread_p95_pips,
                "spread_p99_pips": r.spread_p99_pips,
                "slippage_p50_pips": r.slippage_p50_pips,
                "slippage_p95_pips": r.slippage_p95_pips,
                "slippage_p99_pips": r.slippage_p99_pips,
                "fills_with_nan": r.fills_with_nan,
                "fills_with_negative_price": r.fills_with_negative_price,
                "fills_outside_bid_ask": r.fills_outside_bid_ask,
                "adversarial_findings": list(r.adversarial_findings),
            }
            for r in results
        ]
        sys.stdout.write(json.dumps(rows, indent=2))
        sys.stdout.write("\n")
    else:
        typer.echo("=== Wave 6d Stress Replay (Task 10.2) ===")
        for r in results:
            typer.echo(_format_row(r))
            for finding in r.adversarial_findings:
                typer.echo(f"      finding: {finding}")

    failures = _failures(results)
    if failures:
        typer.secho("\nACCEPTANCE FAILURE:", err=True, fg=typer.colors.RED)
        for msg in failures:
            typer.secho(f"  {msg}", err=True, fg=typer.colors.RED)
        typer.secho(
            "\nSTOP and escalate per Phase-0 spec — do not widen tolerance.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
