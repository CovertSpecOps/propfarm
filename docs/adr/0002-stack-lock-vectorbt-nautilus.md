# ADR-0002: Stack lock — vectorbt (research) + nautilus-trader (production) + MetaTrader5 pkg (bridge)

- **Status:** Accepted
- **Date:** 2026-05-12
- **Deciders:** Project owner (single-operator project)
- **Supersedes:** none
- **Superseded by:** none
- **Related:** [ADR-0001](0001-goals-and-non-goals.md) (project goals), [ADR-0003](0003-mt5-bridge-choice.md) (concrete bridge implementation)

## Context

Phase 0 needs three concrete tooling decisions to make any other work
non-speculative:

1. **Research framework.** Vectorized parameter sweeps over historical tick
   data, CPCV-friendly, fast enough for ≥10k Monte Carlo paths per strategy
   per parameter combination.
2. **Production framework.** Event-driven, deterministic backtest↔live
   parity, multi-account orchestration with per-account kill switches.
3. **Broker bridge.** Routes signals from the production framework into a
   prop-firm MT5 demo / live account with acceptable latency and no
   ToS-banned techniques.

The first two were tentatively locked at project kick-off pending only the
bridge's empirical verification. The third (bridge) was the single highest
risk in Phase 0 — if neither candidate worked, the entire stack assumption
would need re-evaluation before any further work made sense. That risk was
spiked on Day 1 with `scripts/spike_mt5.py` and resolved on 2026-05-12 by
two live runs on an FTMO Free Trial demo from a Vultr Amsterdam Windows VPS.

## Decision

Lock the Phase 0 / Phase 1+ stack as follows. Each component is replaceable
only via a successor ADR.

### Research framework — **vectorbt**

- Vectorized over numpy/pandas/polars; CPCV-compatible.
- Phase-0 dependency: pinned via `pyproject.toml` (`vectorbt>=0.27`).
- Used for: parameter sweeps, walk-forward optimization, ad-hoc strategy
  R&D inside Jupyter / scripts.

### Production framework — **nautilus-trader**

- Event-driven engine with strict backtest↔live parity guarantees.
- Phase-0 dependency: pinned via `pyproject.toml` (`nautilus-trader>=1.190`).
- Used for: the live trading process that consumes signals from research,
  applies the risk layer, and routes orders through the bridge.

### Bridge — direct **`MetaTrader5` Python package** to FTMO MT5

- Decided in detail in ADR-0003.
- Pinned via `pyproject.toml`'s `[mt5]` extra (`MetaTrader5>=5.0.45`).
- Used for: production order routing, position queries, account state
  monitoring against FTMO (and any future MT5-only prop firm).

## Empirical justification — spike result

Two live runs on a $10k FTMO Free Trial demo account confirmed the bridge
is operationally viable. Full per-run details in
[`docs/runbooks/mt5-spike-result.md`](../runbooks/mt5-spike-result.md).

| Field | Value |
|---|---|
| Spike host | Vultr Amsterdam, `voc-c-2c-4gb-50s`, Win Server 2022 Std |
| Python | 3.14 on VPS; dev `.venv` is 3.12 (both supported per MT5 wheels) |
| MT5 pkg version | 5.0.5735 |
| MT5 server | `FTMO-Demo` |
| FTMO Algo Trading | enabled |
| Run-1 (2026-05-12) | open: retcode 10009, RTT 151.4 ms; close: retcode 10016 (script bug, fixed) |
| Run-2 (2026-05-12) | **open + close: retcode 10009, RTT 167.5 ms** — clean PASS |
| **Acceptable-latency band** | **150–170 ms** Amsterdam → FTMO, comfortably under spike's 2 s gate and Phase-0 Gate 2's required p95 < 500 ms over 10 cycles |
| FTMO ToS compliance | single 0.01-lot BUY with inline SL/TP and ~2 s hold is not flagged as HFT, latency arb, or tick-scalping by any clause in the current Forbidden Trading Practices page |

### Why this stack and not alternatives

- **Vectorbt vs. backtrader / bt:** vectorbt's vectorized model is materially
  faster for the parameter-sweep workloads CPCV demands. Backtrader's
  event-driven model overlaps nautilus-trader's role.
- **Nautilus-trader vs. zipline / lean:** nautilus offers tighter
  backtest↔live parity (same engine, not two engines with documented
  divergences) and explicit support for multi-venue / multi-account
  orchestration which we need for parallel prop accounts.
- **MetaTrader5 pkg vs. ZMQ-MQL5 EA fallback:** the direct pkg is simpler
  (one process, official MetaQuotes wire protocol, no MQL5 compile step)
  and the Run-2 PASS confirms it works against FTMO's demo cluster. The
  ZMQ fallback design remains as `scripts/spike_mt5_fallback_zmq.md` for
  historical reference; ADR-0003 marks it CLOSED-NOT-PURSUED.

## Consequences

- **Block lifted.** The MT5-stack-assumption block policy in STATUS.md (in
  effect since the dispatch of W1) is hereby lifted. Subsequent agents may
  reference the direct-pkg path by name (in `src/propfarm/bridge/` and
  derived nautilus adapters). Bridge interfaces are still designed as
  ABC/Protocol-shaped for swap-ability — the abstraction stays useful for
  testing and for the (unlikely) future case where this ADR is reopened.
- **Single supply-chain dependency on MetaQuotes.** The `MetaTrader5` PyPI
  package is published by MetaQuotes Software Corp. and is not open
  source. A pull or a Windows-compatibility regression (there was a brief
  cp311 + Win11-24H2 incident in late 2024) is the single failure mode
  that would reopen this ADR. The ZMQ fallback design exists precisely
  for this scenario and can be instantiated in days, not weeks.
- **Latency budget set.** Phase-0 Gate 2 requires p95 < 500 ms over 10
  cycles. Run-2's single-sample 167.5 ms is well within budget but not
  yet a 10-cycle distribution. Gate 2 acceptance still needs to verify
  the p95 holds, not just one ping.
- **Production VPS is NOT this spike host.** Per project memory, separate
  VPS per prop firm (separate IPs, separate EA hashes) is mandatory.
  The Amsterdam spike host is for the spike only; production VPSes get
  provisioned during Phase 4 deployment.

## Stack revisit triggers

This ADR is reopened — not silently violated — when any of the following
occur:

- **MetaTrader5 pkg pull or major-version compatibility break.** Pin to
  the last working version while ADR-0003's ZMQ fallback is instantiated.
- **Backtest↔live parity divergence > 5 bps** on the same trade sequence
  (nautilus backtest vs the live FTMO demo execution). This is the
  failure mode Phase-0 Gate 2 explicitly catches — see also
  [the simulator-vs-live fill-comparison gate](../../STATUS.md).
- **Any FTMO ToS amendment that flags an aspect of the chosen bridge.**
  Quarterly ToS re-read per ADR-0001 includes a "does the bridge still
  pass?" check.
- **A target prop firm that we move to is not MT5.** Only relevant if
  futures firms (Apex, TopStep) re-enter scope in some future phase —
  currently a non-goal per ADR-0001.
