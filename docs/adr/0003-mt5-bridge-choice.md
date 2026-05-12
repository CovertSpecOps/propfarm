# ADR-0003: MT5 bridge implementation — direct `MetaTrader5` Python package

- **Status:** Accepted
- **Date:** 2026-05-12
- **Deciders:** Project owner (single-operator project)
- **Related:** [ADR-0002](0002-stack-lock-vectorbt-nautilus.md) (stack-lock; this ADR is the bridge slot of that decision), [ADR-0001](0001-goals-and-non-goals.md)

## Context

ADR-0002 locks the overall stack but defers the concrete bridge mechanism
between nautilus-trader (production framework) and the FTMO MT5 demo /
live account. Three candidates were on the table at Phase 0 kickoff:

1. **Direct `MetaTrader5` Python package.** Official MetaQuotes wire
   protocol over a Python ↔ MT5-terminal local IPC. Single process,
   Windows-only.
2. **MQL5 Expert Advisor + ZeroMQ.** MQL5 EA running inside the terminal
   exposes a ZMQ REQ/REP socket; Python client speaks JSON over that.
   Cross-platform on the Python side. Design preserved at
   `scripts/spike_mt5_fallback_zmq.md`. Lineage: Darwinex
   `dwx-zeromq-connector`.
3. **nautilus-trader's own MT5 adapter.** Promised in upstream but not
   stable as of this ADR's date.

Day-1 risk spike for option 1 ran on 2026-05-12. Run-1 surfaced a script
bug (close-by-ticket request inherited the open leg's SL/TP via a dict
spread); fix applied; Run-2 was a clean PASS.

## Decision

**Adopt the direct `MetaTrader5` Python package** as the production bridge.

```
nautilus-trader  ──▶  src/propfarm/bridge/MT5Client  ──▶  MetaTrader5 pkg
                              │                                  │
                              │                                  ▼
                              │                          MT5 terminal (Win VPS)
                              ▼                                  │
                       Protocol/ABC                              ▼
                  (swap-able if needed)                  FTMO-Demo cluster
```

The MT5 terminal runs on a dedicated Windows VPS per prop firm (per
ADR-0001 hard constraints: no shared VPS IPs across firms). Python and
nautilus-trader run on the same VPS for the production deploy; localhost
IPC keeps the bridge round-trip dominated by broker network latency.

## Justification (cites the spike, not pre-decided)

Per `docs/runbooks/mt5-spike-result.md`:

- **Run-2 (2026-05-12):** end-to-end open + close on FTMO-Demo $10k Free
  Trial. Both legs returned `TRADE_RETCODE_DONE` (retcode 10009). Send
  RTT 167.5 ms from Vultr Amsterdam.
- **Latency budget headroom:** 150–170 ms typical RTT vs spike's 2 s
  gate (10× margin) and Phase-0 Gate 2's p95 < 500 ms requirement
  (3× margin on a single sample; Gate 2 still needs the 10-cycle
  distribution to confirm p95).
- **FTMO ToS compliance:** the spike's order shape (single 0.01-lot
  market BUY with attached SL/TP, ~2 s hold, close-by-ticket SELL with
  zero stops) is not flagged by any clause in FTMO's current Forbidden
  Trading Practices page. No HFT, no latency arbitrage, no tick-
  scalping, no copy-trading.

## Status of the ZMQ-MQL5 fallback

**CLOSED — NOT PURSUED.**

The fallback architecture remains documented at
`scripts/spike_mt5_fallback_zmq.md` as historical reference. It is **not**
implemented and **not** wired into the production deploy. Its design
preserves enough detail (REQ/REP socket pattern, OnTimer polling, JSON
wire schema, Python client surface, "hard parts" annotations) that we
can re-instantiate it in days, not weeks, if the trigger conditions in
ADR-0002 fire.

The file stays in `scripts/` rather than being moved to `docs/historical/`
because (a) it's terse, (b) it sits next to the script it would replace,
and (c) version control captures the "CLOSED-NOT-PURSUED" status via this
ADR. Moving it would just cost a `git mv` for no information gain.

## Status of nautilus-trader's native MT5 adapter

**DEFERRED.** Upstream nautilus-trader has not stabilized its MT5 adapter
as of this ADR's date. If it ships and is stable in a later Phase, we
may revisit (this ADR would be reopened, not silently bypassed).
Switching to the native adapter would reduce the `src/propfarm/bridge/`
surface area but offers no latency or ToS-compliance gain over the
direct pkg, so the migration is low-priority.

## Consequences

- **Bridge interfaces stay abstract.** Even though the direct pkg is now
  locked in, `src/propfarm/bridge/` (built during Tasks 14.1–14.3) exposes
  a Protocol-shaped surface so:
  - Tests can inject stubs without touching MT5.
  - The ZMQ fallback can be slotted in without touching nautilus-side
    code, if and when the ADR-0002 revisit triggers fire.
- **Single supply-chain dependency.** Re-stated from ADR-0002: MetaQuotes
  is the single point of failure for the chosen path. Re-read this ADR
  if pkg behavior changes.
- **Spike artifacts retained.** Both `scripts/spike_mt5.py` (now hardened
  against the SL/TP inheritance bug with a regression test) and the
  fallback design doc stay in the repo as reference. Plan Task 1.3's
  "delete after ADR-0003 closes" instruction is **overruled** here:
  the spike script is the smallest possible end-to-end verification of
  the bridge and remains useful as a smoke test for future MT5 / FTMO
  pkg / cluster changes.

## Reactivation triggers (would reopen this ADR, not the parent ADR-0002)

- MetaTrader5 pkg behavior change that breaks the spike (re-run before
  blaming our code).
- FTMO ToS update that flags the direct-pkg path specifically (e.g. a
  rule requiring brokers' own EAs).
- Migration to a non-MT5 prop firm (currently a non-goal).
