# prop-farm

Personal algorithmic-trading system targeting FTMO / FundedNext / FundingPips prop-firm evaluations.

Current state: Phase 0 (Foundations). See [`STATUS.md`](./STATUS.md) for the live task DAG and the authoritative plan at [`docs/superpowers/plans/2026-05-12-phase-0-foundations.md`](./docs/superpowers/plans/2026-05-12-phase-0-foundations.md). No strategy work begins until both Phase 0 acceptance gates (placebo + MT5 hello-world) are green.

## Development setup

The pre-commit pipeline (ruff, ruff-format, **mypy --strict**, lookahead-bias linter, pytest pre-push) requires the project's `.[dev]` extras installed into an active venv. The mypy hook in particular uses `language: system` and invokes the venv's mypy directly (it needs `propfarm` itself importable to type-check the test suite, which the prior isolated-venv setup couldn't do — see STATUS.md `2026-05-19 #4` deferred-ledger entry for the history). New-contributor setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate           # PowerShell on Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install --install-hooks --hook-type pre-commit --hook-type pre-push
```

**Always activate `.venv` before running `git commit`.** Otherwise `mypy` won't be on `PATH` and the pre-commit hook will fail with `Executable mypy not found`. The same activation gates the pre-push pytest hook (it uses `language: system` for the same reason — the isolated-venv pattern couldn't see project deps like `polars`/`pyarrow`).
