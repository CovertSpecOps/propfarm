# FundingPips Rule Predicates Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FundingPips
**Scope:** Rule predicates (drawdown, profit target, banned techniques, news,
consistency, time limits) — **not** commission or swap; those are covered by
`fundingpips-commission-2026-05-12.md` and `fundingpips-swap-2026-05-12.md`.

## Source URLs

| URL | Result |
| --- | --- |
| `https://fundingpips.com/trading-conditions/` | **HTTP 403** on 2026-05-12 (Cloudflare bot challenge on www host). |
| `https://fundingpips.com/` | **HTTP 403** on 2026-05-12 (same Cloudflare gate). |
| `https://help.fundingpips.com/` | Reachable (200). Help-center root. |
| `https://help.fundingpips.com/en/articles/9279468-1-step-model` | Reachable. 1-Step rules. |
| `https://help.fundingpips.com/en/articles/9279477-2-step-model` | Reachable. 2-Step rules. |
| `https://help.fundingpips.com/en/articles/10516343-2-step-pro-model` | Reachable. 2-Step Pro rules. |
| `https://help.fundingpips.com/en/articles/8536000-what-are-the-forbidden-strategies` | Reachable. Forbidden strategies catalog. |
| `https://help.fundingpips.com/en/articles/8535998-can-i-hold-trades-during-the-news-and-over-the-weekend` | Reachable. News rule per model. |
| `https://help.fundingpips.com/en/articles/11321494-copy-trading-policy` | Reachable. Copy trading policy. |
| `https://help.fundingpips.com/en/articles/8535992-what-happens-if-i-breach-a-trading-objective` | Reachable. Breach mechanics. |

The marketing `fundingpips.com/` and `fundingpips.com/trading-conditions/` URLs
returned **HTTP 403** (Cloudflare bot challenge) on the retrieval date — same
behavior as the W3 commission/swap snapshotting attempt. The Intercom-hosted
help center at `help.fundingpips.com` is the only stable primary source for
rule text; the per-model articles (`9279468`, `9279477`, `10516343`) are the
canonical rule definitions.

## Model lineup

As of 2026-05-12, FundingPips's official help-center documents three primary
challenge models within scope for the prop-farm project:

* **1-Step** — single-phase Student evaluation, tight drawdown.
* **2-Step** — Student + Practitioner phases, FTMO-comparable drawdown.
* **2-Step Pro** — tighter drawdown but lower targets and 1-day minimum.

Two further products exist but are out of scope:

* **FundingPips Zero** — different rule profile (no overnight, no weekend, 10-min news window each side). Not selected by this snapshot's predicates.
* **(Giveaway) 1k Instant Account** — promotional, not used by the prop-farm system.

## Server time / DST

From `9279468-1-step-model` and `9279477-2-step-model`:

> The Daily Loss Limit resets at 00:00 Platform Time (UTC+3).

FundingPips publishes its platform time as **UTC+3 year-round** — i.e. **fixed
offset, no DST**. This is the standard MT5-server convention for Middle-East /
GMT+3 brokers (FundingPips's parent is Dubai-based). **Confirmed across all
three model articles.**

**UTC mapping:** server midnight = **21:00 UTC** on the previous calendar day,
year-round.

We compute the server-midnight crossings via `zoneinfo.ZoneInfo("Etc/GMT-3")`
— the canonical IANA zone for a fixed UTC+3 offset with no DST. (Note: in IANA,
`Etc/GMT-3` is `UTC+3`; the sign is intentionally inverted in the Etc family.)
**Confidence: high** on the timezone (numerically and unambiguously stated in
the ToS).

## Daily-loss reference base — common to all FundingPips models

From all three model articles:

> Daily Loss Limit: X% (of the higher value between your daily starting balance or equity)

**Practical effect:** The daily-loss reference base is `max(daily_start_balance,
daily_start_equity)` — i.e. the higher of the equity at server midnight and the
balance ledger at server midnight. In the simulator, daily_start_equity is
already this max for closed-PnL-zero-floating cases; for live state, the predicate
references `max(daily_start_equity, current_balance)` as a conservative proxy
(matching the FTMO/FundedNext pattern).

## Rule 1 — Daily Loss Limit

### 1a. 1-Step

**Source:** help.fundingpips.com/en/articles/9279468-1-step-model

> Daily Loss Limit: 3% (of the higher value between your daily starting balance or equity)

**Numeric:** 3% of starting balance, against daily-start max(equity, balance).
**Confidence: high.**
**Predicate name:** `fundingpips_1step_daily_drawdown`.

### 1b. 2-Step

**Source:** help.fundingpips.com/en/articles/9279477-2-step-model

> Daily Loss Limit: 5% (of the higher value between your daily starting balance or equity)

**Numeric:** 5% of starting balance, against daily-start max(equity, balance).
**Confidence: high.**
**Predicate name:** `fundingpips_2step_daily_drawdown`.

### 1c. 2-Step Pro

**Source:** help.fundingpips.com/en/articles/10516343-2-step-pro-model

> Daily Loss Limit: 3% (of the higher value between your daily starting balance or equity)

**Numeric:** 3% of starting balance, against daily-start max(equity, balance).
**Confidence: high.**
**Predicate name:** `fundingpips_2step_pro_daily_drawdown`.

## Rule 2 — Maximum Loss (Overall Drawdown)

All FundingPips models use a **static** (non-trailing) max-loss rule against
the **initial account size**, identical structure to FTMO.

### 2a. 1-Step

> Maximum Loss Limit: 6% (of the initial account size)

**Numeric:** 6% of starting balance, non-trailing.
**Confidence: high.**
**Predicate name:** `fundingpips_1step_max_drawdown`.

### 2b. 2-Step

> Maximum Loss Limit: 10% (of the initial account size)

**Numeric:** 10% of starting balance, non-trailing.
**Confidence: high.**
**Predicate name:** `fundingpips_2step_max_drawdown`.

### 2c. 2-Step Pro

> Maximum Loss Limit: 6% (of the initial account size)

**Numeric:** 6% of starting balance, non-trailing.
**Confidence: high.**
**Predicate name:** `fundingpips_2step_pro_max_drawdown`.

## Rule 3 — Profit Target

### 3a. 1-Step

> Achieve a 10% profit target during the Student Phase.

**Numeric:** 10%.
**Confidence: high.**
**Predicate name:** `fundingpips_1step_profit_target`.
**Semantics:** Hitting the target emits an :class:`Achievement` with
`achievement_kind="profit_target"`. Kill switch never invoked.

### 3b. 2-Step (per phase)

> Option One: Achieve an 8% profit target during the Student Phase.
> Option Two: Achieve a 10% profit target during the Student Phase.
> Achieve a 5% profit target during the Practitioner Phase.

FundingPips offers a binary 8% / 10% Phase-I choice at signup. For this snapshot
we encode **both** Phase-I targets as separate predicates (the loader selects
one based on the trader's account configuration). Phase-II is unambiguously 5%.

**Predicate names:**
* `fundingpips_2step_profit_target_phase1_8pct` (high, 8% Phase-I option).
* `fundingpips_2step_profit_target_phase1_10pct` (high, 10% Phase-I option).
* `fundingpips_2step_profit_target_phase2` (high, 5% Phase-II).

**Default model selection** uses the 8% Phase-I variant — easier to reach,
matching the standard FundingPips marketing pitch.

### 3c. 2-Step Pro (per phase)

> Achieve a 6% profit target during the Student Phase. Achieve a 6% profit target during the Practitioner Phase.

**Numeric:** Phase 1: **6%**. Phase 2: **6%**.
**Confidence: high.**
**Predicate names:** `fundingpips_2step_pro_profit_target_phase1`, `fundingpips_2step_pro_profit_target_phase2`.

## Rule 4 — Minimum Trading Days

### 4a. 1-Step

> Complete a minimum of 3 trading days to pass the evaluation.

**Numeric:** 3 days.
**Confidence: high.**
**Predicate name:** `fundingpips_1step_min_trading_days`.

### 4b. 2-Step

> Complete a minimum of 3 trading days to pass the evaluation (each phase).

**Numeric:** 3 days per phase.
**Confidence: high.**
**Predicate name:** `fundingpips_2step_min_trading_days`.

### 4c. 2-Step Pro

> Complete a minimum of 1 trading day to pass the evaluation.

**Numeric:** 1 day per phase.
**Confidence: high.**
**Predicate name:** `fundingpips_2step_pro_min_trading_days`.

All three are completion-gates: predicate emits :class:`Achievement` with
`achievement_kind="min_trading_days"`. Kill switch never invoked.

## Rule 5 — Time Limits

The model articles do not establish overall time limits for phase completion
on any of 1-Step / 2-Step / 2-Step Pro. **No time limit** is the documented
status for all three.

**Confidence: high** (absence is explicit in the model articles).
**Predicate name:** `fundingpips_time_limit`. Permanent no-op until ToS changes.

## Rule 6 — Consistency Rule

**Source:** help.fundingpips.com/en/articles/9279477-2-step-model and similar for 2-Step Pro:

> A 35% consistency score must be achieved, meaning no single trading day can account for more than 35% of the total profit.

**Critical scope detail:** This rule applies to **On Demand Rewards on the
Master Account** (i.e. the funded stage, payout-cycle), **NOT** the Student or
Practitioner evaluation phases. For 2-Step Pro, the rule additionally applies
during Student/Practitioner phases **only when the trader selects the Daily
Reward cycle option** at signup.

**Numeric:** **35%** single-day-profit-share threshold.
**Confidence: high** — numerically and unambiguously published.

**Predicate name:** `fundingpips_consistency_check`.

**Predicate semantics:** flags when any single trading day's realized profit
exceeds 35% of cumulative realized profit. The runtime layer (Task 12) routes
the violation to the payout/reward flow, NOT to account termination.

## Rule 7 — News Trading

**Source:** help.fundingpips.com/en/articles/8535998-can-i-hold-trades-during-the-news-and-over-the-weekend

> 1 Step, 2 Step & 2 Step Pro (Master Account): cannot open or close positions within a 10-minute window surrounding a high-impact news event (5 minutes before and 5 minutes after).

**Numeric:** 5 minutes pre / 5 minutes post (10-minute total).
**Confidence: high** on the time window. The "high-impact" event list is
delegated to the caller (out of W4b scope).

**Predicate name:** `fundingpips_news_blackout_window`.

**Note:** On 1-Step / 2-Step / 2-Step Pro at the Master (funded) stage,
violation forfeits profit on the affected trade — not account termination.
Phase 0 predicate is no-op; runtime (Task 12) will route the event.
FundingPips Zero is **out of scope** for this predicate (stricter 10/10 rule
not encoded here).

## Rule 8 — Banned Trading Practices

**Source:** help.fundingpips.com/en/articles/8536000-what-are-the-forbidden-strategies

Catalog of explicitly forbidden strategies:

> gap trading, high-frequency trading, server spamming, latency arbitrage, toxic trading flow, hedging, long-short arbitrage, reverse arbitrage, tick scalping, server execution, opposite account trading, and churning and burning

Plus: copy trading with other users, third-party account management, third-party
EAs (except trade/risk managers).

No numeric thresholds published for any of the listed practices.

### 8a. HFT

> high-frequency trading

Categorical prohibition, no numeric threshold.
**Confidence: uncertain.**
**Predicate name:** `fundingpips_hft_check`.

### 8b. Latency Arbitrage

> latency arbitrage

Categorical prohibition, no numeric threshold.
**Confidence: uncertain.**
**Predicate name:** `fundingpips_latency_arb_check`.

### 8c. Copy Trading

**Source:** help.fundingpips.com/en/articles/11321494-copy-trading-policy

> You are allowed to copy trades between your own FundingPips accounts (i.e., accounts registered under the same individual). Copying trades between FundingPips accounts owned by different users is prohibited.

Categorical prohibition on different-owner copy-trading. Same-owner copy-trading
**is allowed** with no combined-capital threshold published. The predicate
captures the categorical different-owner prohibition.
**Confidence: high.**
**Predicate name:** `fundingpips_copy_trading_check`.

### 8d. Martingale

FundingPips's forbidden-strategies catalog does not list martingale by name.
Predicate carried for cross-firm symmetry and flagged uncertain.
Listed instead in the forbidden catalog: gap trading, server spamming,
churning and burning.

**Confidence: uncertain.**
**Predicate name:** `fundingpips_martingale_check`.

### 8e. Tick Scalping / Hedging Composite

Tick scalping, hedging, long-short arbitrage, reverse arbitrage, opposite account
trading are also categorical bans. For Phase 0 these are bundled into the same
`fundingpips_hft_check` heuristic (sub-second trade duration is the smoke-test).
Surfaced explicitly in the banned-techniques composite's `interpretation` field
on the predicate instance.

## Summary classification table

| Predicate | Confidence | Rule type |
| --- | --- | --- |
| `fundingpips_1step_daily_drawdown` | high | Numeric: 3% / starting balance |
| `fundingpips_1step_max_drawdown` | high | Numeric: 6% / starting balance, non-trailing |
| `fundingpips_1step_profit_target` | high | Numeric: 10% / starting balance |
| `fundingpips_1step_min_trading_days` | high | Numeric: 3 days |
| `fundingpips_2step_daily_drawdown` | high | Numeric: 5% / starting balance |
| `fundingpips_2step_max_drawdown` | high | Numeric: 10% / starting balance, non-trailing |
| `fundingpips_2step_profit_target_phase1_8pct` | high | Numeric: 8% / starting balance |
| `fundingpips_2step_profit_target_phase1_10pct` | high | Numeric: 10% / starting balance |
| `fundingpips_2step_profit_target_phase2` | high | Numeric: 5% / starting balance |
| `fundingpips_2step_min_trading_days` | high | Numeric: 3 days |
| `fundingpips_2step_pro_daily_drawdown` | high | Numeric: 3% / starting balance |
| `fundingpips_2step_pro_max_drawdown` | high | Numeric: 6% / starting balance, non-trailing |
| `fundingpips_2step_pro_profit_target_phase1` | high | Numeric: 6% / starting balance |
| `fundingpips_2step_pro_profit_target_phase2` | high | Numeric: 6% / starting balance |
| `fundingpips_2step_pro_min_trading_days` | high | Numeric: 1 day |
| `fundingpips_time_limit` | high | "No time limit" is itself explicit |
| `fundingpips_news_blackout_window` | high | Numeric: 5 min pre/post (Master only) |
| `fundingpips_copy_trading_check` | high | Categorical: prohibited between different owners |
| `fundingpips_consistency_check` | high | Numeric: 35% single-day-profit-share (Master) |
| `fundingpips_hft_check` | uncertain | No published numeric threshold |
| `fundingpips_latency_arb_check` | uncertain | No published numeric threshold |
| `fundingpips_martingale_check` | uncertain | Not in FundingPips catalog; carried for symmetry |

## Drift check

If FundingPips updates any help-center article changing a numeric threshold, this
file MUST be replaced (do not edit in place — create a new dated snapshot)
and the matching predicates' `tos_quote` fields in
`src/propfarm/rules/fundingpips.py` bumped to match.
`test_fundingpips_predicate_tos_quotes_appear_in_snapshot` will fail on the
next CI run after the predicate quote changes if the snapshot file has not been
updated, closing the silent-drift window.
