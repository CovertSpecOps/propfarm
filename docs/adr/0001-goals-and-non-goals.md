# ADR-0001: Goals and non-goals

- **Status:** Accepted
- **Date:** 2026-05-12
- **Deciders:** Project owner (single-operator project)

## Context

This project is a personal algorithmic trading "prop farm." It exists to obtain
leverage on the operator's skill (Python, infrastructure, quant rigor) without
risking personal capital in the market. There is exactly one operator. There
are no clients, no investors, no subscribers. The codebase is single-tenant
from day one.

Two profit paths share the same codebase. The **primary path** (Phase 1
onward) is passing prop-firm evaluations on a $1000 evaluation budget — FTMO,
FundedNext, FundingPips — then trading funded capital for 80–95% profit splits.
The **long-term path** is running the same strategies on a personal
brokerage account once the strategies have a live track record on firm
capital. The personal-brokerage path is downstream; it does not drive any
Phase-0 or Phase-1 decision. Firm risk (~80–100 prop firms collapsed
2024–2025) is treated as a first-class risk variable equal to market risk —
this distinguishes the system from a naive "pass evaluation and compound"
playbook.

**Target firms (priority order):** FTMO is the primary firm. FundedNext is
the secondary firm, run in parallel for diversification. FundingPips is the
cheap-retry bucket for failed challenges. The capital deployment ladder
(how the $1000 evaluation budget splits across Phase A / Phase B / Phase C)
lives in the Phase-1 budget plan; this ADR pins the firm-priority order but
not the dollar splits.

## Decision: Objectives in priority order (do not invert)

The order is binding. A later objective is never optimized at the expense of
an earlier one. When two objectives appear to conflict, the lower-numbered
one wins by construction.

1. **Survive.** Risk-of-ruin < 1% over any rolling 250-day window in Monte
   Carlo (≥10k paths, stationary block bootstrap, 5th-percentile equity
   curve as the headline number). Respect the tightest prop-firm rule in
   scope: 5% daily drawdown and 10% max drawdown. A strategy that violates
   either threshold in any single simulated path is rejected, not
   re-tuned.
2. **Pass evaluations reliably.** Bootstrapped historical paths must show
   ≥ 60% pass rate on one-step challenges (single profit target, no
   verification leg) and ≥ 40% on two-step challenges (challenge phase
   + verification phase) at conservative risk. The "pass rate" is the
   **5th-percentile** of the bootstrap distribution of pass outcomes, not
   the point estimate from one backtest. "Reliable" is a property of the
   lower tail, not the headline number.
3. **Maximize risk-adjusted ROI.** Optimize for Sortino and Calmar, not
   raw return. Sharpe is reported but never optimized against directly
   (it punishes upside volatility identically to downside). Stretch
   targets on funded capital: Calmar ≥ 3, Sortino ≥ 2, MAR ≥ 0.5.
4. **Scale to N accounts in parallel without correlated blowups.**
   Account-level kill switches, portfolio-level exposure caps, and
   uncorrelated strategy sleeves are mandatory before adding a second
   funded account. Pairwise sleeve correlation > 0.6 over a rolling
   60-day window blocks scaling until a sleeve is replaced or
   de-weighted.

## Decision: Payout policy

Withdraw 100% of profits at every permitted payout window. Do not compound on
firm books. The firm balance is a firm liability, not a user asset.

Rationale: roughly 80–100 prop firms collapsed in 2024–2025 (payout halts,
ToS rugpulls, outright disappearance). Compounded balance left on a firm's
books is equity exposed to firm-risk default, not market-risk return.
Withdrawal converts firm liability into user asset.

Operational corollary: a funded account's sizing mode switches from
**AGGRESSIVE** to **PRESERVATION** the moment it becomes payout-eligible
and remains in PRESERVATION until the withdrawal clears. The switch is
implemented in the challenge state-machine module. PRESERVATION roughly
halves per-trade risk and tightens daily-loss predicates; the exact
magnitudes live with the state machine, not in this ADR, so the values
can be tuned without amending project goals.

## Decision: Hard constraints

These are not preferences; they are filter predicates applied at strategy
brainstorming, not at validation. A strategy that depends on a banned
technique is rejected in Phase 1, not after backtest.

- **No martingale, no grid-without-stop, no negative-skew systems.** A
  strategy whose worst historical week exceeds 2× its average week's PnL
  is rejected. Win-rate-95%-but-blows-up-the-account systems are the
  exact failure mode this filter targets.
- **Every strategy carries a full safety harness:** stop loss, max
  position size, max daily loss, max trades per day, news-blackout
  filter, consecutive-loss circuit breaker. Missing any one disqualifies
  the strategy.
- **Literal ToS compliance per firm.** No best-effort interpretation. ToS
  is fetched and snapshotted to `docs/firm-tos-snapshots/` per firm per
  retrieval date; predicates derive from that snapshot, not from
  paraphrase.
- **No same MT5 account number, VPS IP, or EA file hash across firms.**
  Prop firms share fraud-detection databases; collision triggers ban
  cascades across all accounts at all firms simultaneously. This is
  enforced operationally (separate VPS per firm cluster, distinct EA
  builds with distinct hashes) not just by policy.
- **No copy-trading across accounts at the same firm.** Banned by FTMO
  ToS and likely by others; even where formally allowed it concentrates
  risk on a single signal source defeating the diversification rationale
  for running multiple accounts.
- **Every backtest is re-runnable from a single config file** with a
  fixed seed and a content-pinned data snapshot. A result that cannot be
  reproduced is treated as if it does not exist.

## Decision: Non-goals (explicit)

Each item below was considered and explicitly rejected. Future agents and
future-self should treat scope creep into any of these as an ADR amendment,
not a code change.

- **No external clients, no signal-selling, no managed accounts, no
  social-trading copy followers.** The system is single-operator. There
  is no compliance overhead, no marketing surface, no SLA. A pivot
  toward selling signals would invalidate the threat model, the
  architecture, and the legal posture; it requires a new ADR.
- **No futures prop firms in Phase 1** (Apex, TopStep, etc.). Trailing
  drawdown rules punish equity-curve volatility too harshly during the
  learning phase. Revisit only after the FTMO/FundedNext/FundingPips
  path is profitable on funded capital.
- **No compounding on firm books.** See payout policy above.
- **No reuse of MT5 account numbers, VPS IPs, or EA file hashes across
  firms.** See hard constraints above.
- **No HFT, no latency arbitrage, no tick-scalping with sub-second
  holds, no grid or martingale on funded accounts.** Banned by FTMO ToS
  and likely by other firms in scope. Strategies depending on these
  techniques are rejected at Phase 1 brainstorming, not at Phase 3
  validation — earlier is cheaper.
- **No live deployment of any strategy without Phase 3 evidence on
  disk.** Required artifacts: CPCV pass, DSR > 0.95, PBO < 0.5, Monte
  Carlo 5th-percentile equity curve survives, stress-replay library
  survives (Lehman, SNB, GBP flash, COVID, UK gilts, SVB). Missing any
  one blocks deploy. "Looks good" is not evidence.
- **No reliance on a single firm.** Diversify firm exposure from day
  one. Concentration on one firm is a single point of failure regardless
  of that firm's current reputation.
- **No copy-trading across accounts at the same firm.** (Mirrors the
  hard-constraint above; flagged here so scope-creep audits skimming only
  the Non-goals list still catch it. The hard-constraint version is the
  binding one; this entry is redundancy on purpose.)
- **No silent relaxation of validation-gate thresholds.** DSR > 0.95,
  PBO < 0.5, and the MC 5th-percentile equity-curve gate are floors, not
  targets. A future validation-gates ADR may tighten them but never relax
  them without amending this ADR explicitly.

## Consequences

- **Forecloses any future pivot toward selling signals, managing
  external capital, or building a SaaS product.** The codebase, the
  threat model, and the operational stance are all single-operator. A
  multi-tenant pivot would require a ground-up redesign and a new ADR.
- **Enables aggressive kill criteria** during validation and live
  operation: there is no client to disappoint, no expectation to meet
  beyond the operator's own, no marketing-driven incentive to keep a
  marginal strategy alive. Strategies die fast and cheap.
- **Sets the deploy bar explicitly, before any temptation arises.** The
  Phase 3 evidence list is fixed in this ADR. Future-self cannot
  rationalize "good enough" without amending this document, which
  forces the rationalization to be written down and reviewed.
- **Constrains strategy search space at the brainstorming stage.**
  Banned-technique filters apply before Phase 1 work begins, not after.
  This rules out otherwise-attractive ideas (e.g., latency-arb,
  HFT-style scalping, grid-with-stop variants that drift into
  martingale) that would waste validation cycles.
- **Treats firm risk as first-class.** Diversification of firms is a
  build-time requirement, not a runtime optimization. The codebase
  must support N firms with isolated account numbers, VPS IPs, and EA
  hashes from Phase 0 onward.

## Revisit triggers

This ADR is reopened — not silently violated — when any of the following
occur:

- **A target prop firm's payout reliability degrades.** Pause new
  challenges at that firm and rebalance to the remaining firms. This is
  a policy reaction within the existing objectives, not an objective
  change, but it is flagged here so the response is mechanical, not
  discretionary.
- **Risk-of-ruin gate is breached in Phase 3 validation.** Tighten
  sizing or kill the strategy. Do not relax the gate. If the gate
  itself is wrong, reopen this ADR.
- **Quarterly ToS re-read.** Re-read each firm's ToS every quarter and
  regenerate the derived predicates. A material ToS change at any firm
  reopens this ADR for review of the non-goals list (futures-firm
  posture, banned-technique list, copy-trade prohibition).
- **The personal-brokerage path becomes the primary path.** If the
  funded-capital path is shut down (firm collapses across the
  industry, regulatory change), the priority order may need to be
  revisited — survival still wins, but pass-rate ceases to be
  objective 2 and ROI moves up.
- **A strategy passes everything except DSR/PBO** (or the MC 5th-pct
  gate). Do not relax the threshold silently to ship. Either tighten
  sizing, kill the strategy, or reopen this ADR with a written
  justification. The whole point of pinning the deploy bar here is to
  make this decision visible.
