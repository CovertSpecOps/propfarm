"""Ad-hoc CLI to run Gate 1 (placebo acceptance gate, Task 13.1).

Usage::

    python scripts/run_placebo_gate.py
    python scripts/run_placebo_gate.py --regime trending --n-trades 1000

Exit codes:
* 0 — verdict == "pass"
* 1 — verdict == "fail"
* 2 — error before the gate could run (fixture missing, sha mismatch, etc.)
"""

from __future__ import annotations

import sys
from typing import Literal

import typer

from propfarm.placebo.gate import PlaceboGateResult, run_placebo_gate

app = typer.Typer(add_completion=False, no_args_is_help=False)


def _print_report(result: PlaceboGateResult) -> None:
    """Print the gate result in a human-readable table."""
    lines = [
        "=== Gate 1 (Task 13.1) placebo acceptance gate ===",
        f"verdict:                       {result.verdict.upper()}",
        f"n_trades:                      {result.n_trades}",
        f"mean_pnl_usd:                  {result.mean_pnl_usd:.4f}",
        f"expected_pnl_usd (per-trade):  {result.expected_pnl_usd:.4f}",
        f"residual_usd:                  {result.residual_usd:.4f}",
        f"empirical_noise_floor_usd:     {result.empirical_noise_floor_usd:.4f}",
        f"epsilon_usd:                   {result.epsilon_usd:.4f}",
        f"epsilon_ratio:                 {result.epsilon_ratio:.4f}",
        "",
        "Per-leg totals:",
        f"  total_spread_cost_usd:       {result.total_spread_cost_usd:.4f}",
        f"  total_slippage_cost_usd:     {result.total_slippage_cost_usd:.4f}",
        f"  total_commission_usd:        {result.total_commission_usd:.4f}",
        f"  total_swap_cost_usd:         {result.total_swap_cost_usd:.4f}",
    ]
    if result.failure_reason:
        lines.append("")
        lines.append("Failure attribution:")
        lines.append(f"  {result.failure_reason}")
    for line in lines:
        typer.echo(line)


@app.command()
def main(
    regime: str = typer.Option("choppy", help="Substrate regime"),
    n_trades: int = typer.Option(2000, "--n-trades", help="Trades to generate"),
    n_bootstrap_paths: int = typer.Option(
        10_000, "--n-bootstrap-paths", help="Bootstrap path count for noise floor"
    ),
    seed: int = typer.Option(20260513, "--seed", help="Master RNG seed"),
    hold_bars: int = typer.Option(5, help="Bars to hold each trade"),
    target_dollar_vol: float = typer.Option(
        100.0, "--target-dollar-vol", help="Per-trade target dollar vol"
    ),
) -> None:
    """Run the placebo gate and print the verdict.

    Exits 0 on PASS, 1 on FAIL, 2 on error.
    """
    valid_regimes = ("choppy", "trending", "mean_reverting", "fat_tailed")
    if regime not in valid_regimes:
        typer.echo(f"ERROR: --regime must be one of {valid_regimes}, got {regime!r}", err=True)
        raise typer.Exit(code=2)
    regime_typed: Literal["choppy", "trending", "mean_reverting", "fat_tailed"] = regime  # type: ignore[assignment]
    try:
        result = run_placebo_gate(
            regime=regime_typed,
            n_trades=n_trades,
            n_bootstrap_paths=n_bootstrap_paths,
            rng_seed=seed,
            hold_bars=hold_bars,
            target_dollar_vol_per_trade=target_dollar_vol,
        )
    except Exception as exc:
        typer.echo(f"ERROR: gate execution raised: {exc!r}", err=True)
        raise typer.Exit(code=2) from exc
    _print_report(result)
    sys.exit(0 if result.verdict == "pass" else 1)


if __name__ == "__main__":
    app()
