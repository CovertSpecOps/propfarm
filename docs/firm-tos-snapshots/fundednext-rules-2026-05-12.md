# FundedNext Rule Predicates Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FundedNext
**Scope:** Rule predicates (drawdown, profit target, banned techniques, news,
consistency, time limits) — **not** commission or swap; those are covered by
`fundednext-commission-2026-05-12.md` and `fundednext-swap-2026-05-12.md`.

## Source URLs

| URL | Result |
| --- | --- |
| `https://fundednext.com/trading-conditions/` | **HTTP 404** on 2026-05-12 (page restructured / removed). |
| `https://fundednext.com/` | Reachable (200). Homepage advertises model lineup with summary numbers (Stellar 2-Step, Stellar 1-Step, Stellar Lite, Stellar Instant). |
| `https://fundednext.com/stellar-2-step`, `…stellar-lite`, `…stellar-1-step` | **HTTP 404** — direct model pages have been retired. |
| `https://fundednext.com/compare-challenges` | **HTTP 404** — comparison page retired. |
| `https://help.fundednext.com/` | Reachable. Help-center root. |
| `https://help.fundednext.com/en/articles/8019811-how-can-i-calculate-the-daily-loss-limit` | Reachable. Daily loss formula and reset time. |
| `https://help.fundednext.com/en/articles/8019803-what-are-the-minimum-trading-days-in-fundednext-challenges` | Reachable. Cross-model minimum trading days. |
| `https://help.fundednext.com/en/articles/8021076-what-rules-do-i-need-to-follow-in-the-stellar-2-step-challenge` | Reachable. Stellar 2-Step rules. |
| `https://help.fundednext.com/en/articles/8021071-what-is-the-profit-target-of-the-stellar-2-step-challenge` | Reachable. Stellar 2-Step profit targets per phase. |
| `https://help.fundednext.com/en/articles/8021073-how-many-days-will-i-get-to-complete-phase-1-2-of-the-stellar-2-step-challenge` | Reachable. Stellar 2-Step time limit / min days. |
| `https://help.fundednext.com/en/articles/8021061-what-are-the-rules-for-the-stellar-1-step-challenge-at-fundednext` | Reachable. Stellar 1-Step rules. |
| `https://help.fundednext.com/en/articles/8030875-what-is-the-profit-target-of-the-stellar-1-step-challenge` | Reachable. Stellar 1-Step profit target. |
| `https://help.fundednext.com/en/articles/8030880-is-there-a-minimum-trading-day-or-time-limit-to-complete-the-stellar-1-step-challenge` | Reachable. Stellar 1-Step min days / time limit. |
| `https://help.fundednext.com/en/articles/9094072-what-rules-do-i-need-to-follow-in-the-stellar-lite-challenge` | Reachable. Stellar Lite rules. |
| `https://help.fundednext.com/en/articles/9094074-how-many-days-will-i-get-to-complete-phases-1-2-of-the-stellar-lite-challenge` | Reachable. Stellar Lite phase / min days. |
| `https://help.fundednext.com/en/articles/8020351-what-are-the-restricted-prohibited-trading-strategies` | Reachable. Banned-technique catalog. |
| `https://help.fundednext.com/en/articles/11982271-does-fundednext-allow-hft-high-frequency-trading` | Reachable. HFT explanation (no numeric threshold). |
| `https://help.fundednext.com/en/articles/8019805-what-is-the-copy-trading-rule-at-fundednext` | Reachable. Copy-trading rule (USD 300,000 combined for same-owner). |
| `https://help.fundednext.com/en/articles/10701447-is-news-trading-allowed-at-fundednext` | Reachable. News rule on FundedNext (funded) stage. |
| `https://help.fundednext.com/en/articles/12700357-what-is-the-disciplined-trader-program-and-how-does-it-help-traders-build-consistency` | Reachable. Disciplined Trader Program — no single-day profit-share threshold published. |
| `https://help.fundednext.com/en/articles/10256545-what-is-the-1-risk-limit-rule-who-and-when-will-it-be-implemented` | Reachable. 1% risk-per-trade rule (post-warning, not unconditional). |

The marketing URLs at `fundednext.com/trading-conditions/` and the per-model
pages (`stellar-2-step`, `stellar-lite`, `stellar-1-step`) all returned **HTTP 404**
on the retrieval date — FundedNext consolidated its public rule prose into the
Intercom-hosted help center. The help-center articles above are the only stable
primary source; the homepage at `fundednext.com/` is corroborative for the
numeric summary numbers.

## Model lineup

As of 2026-05-12, FundedNext's official help-center documents four challenge models:

* **Stellar 2-Step** — flagship two-step evaluation; default for this project (matches the brief's $50k FTMO + $50k FundedNext Phase-B parallel run).
* **Stellar 1-Step** — single-phase evaluation, tighter drawdown.
* **Stellar Lite** — lower-cost two-step variant, also tighter drawdown.
* **Stellar Instant** — Listed on the homepage and elsewhere ("no challenge phase needed") but has **no dedicated help-center rule article** as of 2026-05-12. **Excluded from this snapshot's rule encoding** — predicates for Instant should be added once the firm publishes a rule article. Loader should not select `"stellar_instant"` as a model key in this snapshot.

The legacy **Express** model is no longer in the FundedNext lineup as of 2026-05-12
(deprecated during the 2024 product consolidation; the help center contains no
Express-specific article). **Not in scope for this snapshot.**

## Server time / DST

From `8019811-how-can-i-calculate-the-daily-loss-limit`:

> Your daily loss limit will reset every day at 00:00 (server time). The server
> operates on GMT+3 during Daylight Saving Time (summer) and GMT +2 otherwise.

**UTC mapping:**
- Winter (GMT+2 = UTC+2): server midnight = **22:00 UTC on the previous calendar day**.
- Summer (GMT+3 = UTC+3): server midnight = **21:00 UTC on the previous calendar day**.

This is the **same EET/EEST timezone family as FTMO**. The Daily Drawdown
predicate's `daily_start_equity` field MUST be the equity captured at the most
recent server-midnight crossing, computed via
`zoneinfo.ZoneInfo("Europe/Athens")` — the canonical IANA zone for EET/EEST,
identical to FTMO's. **Confidence: high** on the timezone (explicit in ToS).

## Daily-loss reference base — model-specific

FundedNext's daily-loss formula uses a different reference base than FTMO's.

From `8019811-how-can-i-calculate-the-daily-loss-limit`:

> Daily Loss Limit = Initial Balance × Daily Loss Limit Percentage of Your
> Enrolled Challenge Account.

> Profits earned during a trading day increase the daily loss allowance, but
> this benefit resets at midnight — previous day gains don't carry forward.

**Practical effect:** The daily-loss floor at any intraday moment is
`max(daily_start_equity, current_balance_with_intraday_profits) - account_size × loss_pct`.
i.e. **the higher of (a) daily_start_equity, (b) intraday peak balance** is the
reference. Once equity drops to within 5% (Stellar 2-Step) of starting balance
plus any intraday profits accumulated today, breach.

For Phase-0 predicate purposes we approximate this as
`max(daily_start_equity, current_balance) - threshold_usd > current_equity`.
The :class:`AccountState` carries `daily_start_equity` already (capture at
server-midnight) and `current_balance` (closed-trade book) — both pre-existing
fields on the W4a ABC; no ABC change required.

## Rule 1 — Daily Loss Limit (Daily Drawdown)

### 1a. Stellar 2-Step

**Source:** help.fundednext.com/en/articles/8021076-what-rules-do-i-need-to-follow-in-the-stellar-2-step-challenge

> Your account must not lose more than 5% of the initial balance in a single day.

**Numeric:** 5% of starting balance.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_2step_daily_drawdown`.

### 1b. Stellar 1-Step

**Source:** help.fundednext.com/en/articles/8021061-what-are-the-rules-for-the-stellar-1-step-challenge-at-fundednext

> Your account must not lose more than 3% of the initial balance in a single day.

**Numeric:** 3% of starting balance.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_1step_daily_drawdown`.

### 1c. Stellar Lite

**Source:** help.fundednext.com/en/articles/9094072-what-rules-do-i-need-to-follow-in-the-stellar-lite-challenge

> You may not lose more than 4% of your initial balance in a single day.

**Numeric:** 4% of starting balance.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_lite_daily_drawdown`.

## Rule 2 — Maximum Loss (Overall Drawdown)

All three documented models use a **static** (non-trailing) max-loss rule against
the **initial balance**.

### 2a. Stellar 2-Step

> Your account must not drop below 90% of its initial balance — meaning the total loss cannot exceed 10% overall.

**Numeric:** 10% of starting balance. Non-trailing.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_2step_max_drawdown`.

### 2b. Stellar 1-Step

> Your account must not drop below 94% of its initial balance, meaning the total loss cannot exceed 6% overall.

**Numeric:** 6% of starting balance. Non-trailing.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_1step_max_drawdown`.

### 2c. Stellar Lite

> Your account balance or equity cannot fall below 92% of your initial balance.

**Numeric:** 8% of starting balance. Non-trailing.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_lite_max_drawdown`.

## Rule 3 — Profit Target

### 3a. Stellar 2-Step (per phase)

**Source:** help.fundednext.com/en/articles/8021071-what-is-the-profit-target-of-the-stellar-2-step-challenge

> Phase 1: You must achieve 8% growth on your starting balance. Phase 2: After completing Phase 1, you must achieve 5% growth in Phase 2.

**Numeric:** Phase 1: **8%**. Phase 2: **5%**.
**Confidence: high.**
**Predicate names:** `fundednext_stellar_2step_profit_target_phase1`, `fundednext_stellar_2step_profit_target_phase2`.
**Semantics:** Hitting the target emits an :class:`Achievement` (not a Violation) with
`achievement_kind="profit_target"`. Kill switch never invoked.

### 3b. Stellar 1-Step

**Source:** help.fundednext.com/en/articles/8030875-what-is-the-profit-target-of-the-stellar-1-step-challenge

> The Stellar 1-Step Challenge requires traders to reach a 10% profit target to pass the Challenge Phase.

**Numeric:** 10% of starting balance.
**Confidence: high.**
**Predicate name:** `fundednext_stellar_1step_profit_target`.

### 3c. Stellar Lite (per phase)

**Source:** help.fundednext.com/en/articles/9094074-how-many-days-will-i-get-to-complete-phases-1-2-of-the-stellar-lite-challenge

> Phase 1: 8% profit target. Phase 2: 4% profit target.

**Numeric:** Phase 1: **8%**. Phase 2: **4%**.
**Confidence: high.**
**Predicate names:** `fundednext_stellar_lite_profit_target_phase1`, `fundednext_stellar_lite_profit_target_phase2`.

## Rule 4 — Minimum Trading Days

**Source:** help.fundednext.com/en/articles/8019803-what-are-the-minimum-trading-days-in-fundednext-challenges

> Stellar 1-Step: a minimum of 2 trading days. Stellar 2-Step: a minimum of 5 trading days. Stellar Lite: a minimum of 5 trading days.

**Numeric:** Stellar 1-Step: **2 days**; Stellar 2-Step and Stellar Lite: **5 days** per phase.
**Confidence: high.**
**Predicate names:** `fundednext_stellar_1step_min_trading_days`, `fundednext_stellar_2step_min_trading_days`, `fundednext_stellar_lite_min_trading_days`.
**Semantics:** Completion-gate. Emits :class:`Achievement` with
`achievement_kind="min_trading_days"`. Kill switch never invoked.

## Rule 5 — Time Limits

**Source:** Each model's "how many days" article above.

> There is no time limit for completing Phase 1 or Phase 2 of the Stellar 2-Step Challenge.
> There is no time restriction to complete the Stellar 1-Step Challenge.
> There is no time limit for completing Phase 1 or Phase 2 of the Stellar Lite Challenge.

**No time limit for any model.** Predicate `fundednext_time_limit` is a permanent
no-op until ToS changes. **Confidence: high** (the "no time limit" status is itself
explicit in the help center).

## Rule 6 — Consistency Rule

**Source:** help.fundednext.com/en/articles/12700357-what-is-the-disciplined-trader-program-and-how-does-it-help-traders-build-consistency

FundedNext does NOT publish a numeric single-day-profit-share threshold for
challenge-phase or funded-stage consistency. The Disciplined Trader Program
references five (5) consecutive successful Performance Reward cycles — a
cycle-level consistency, not a daily profit-share.

The Quick Strike rule (banned-technique catalog) penalizes "positions closed
within 30 seconds of opening" when those constitute 30% or higher of total
recorded profit. This is the closest analogue to a consistency rule but
addresses trade-duration distribution, not single-day profit share.

**Predicate name:** `fundednext_consistency_check`.
**Numeric working threshold:** any single trading day with realized profit > 50%
of cumulative realized profit is flagged for human review. **Same heuristic as the
FTMO consistency check** since FundedNext, like FTMO, reviews on a case-by-case
basis without a numeric public threshold.
**Confidence: uncertain** — no published numeric threshold; working interpretation only.

## Rule 7 — News Trading (Funded Stage Only)

**Source:** help.fundednext.com/en/articles/10701447-is-news-trading-allowed-at-fundednext

> Trades executed 5 minutes before and 5 minutes after a listed high-impact news event (a total 10-minute window) are subject to the News Reward Share Rule. 40% of the profit from these profitable trade(s) will be counted toward the trader's account balance.

**Numeric:** 5 minutes pre / 5 minutes post (10-minute total). Profit-share
penalty (not account termination) on the funded stage; rule applies to **Stellar 1-Step,
Stellar 2-Step, and Stellar Lite FundedNext Accounts**.

**Confidence: high** on the time window (numerically published). The list of
"high-impact" events is delegated to the caller (out of W4b scope).

**Predicate name:** `fundednext_news_blackout_window`.

**Note:** Unlike FTMO's news rule, FundedNext **does not terminate** the account
for trading during the window — it forfeits 60% of the profit. Phase 0 predicate
returns a `Violation` with `severity="kill"` (high confidence) on detection;
runtime layer (Task 12) can downgrade the action to a profit-clawback when the
state machine routes the event. The predicate itself is no-op in Phase 0 (news
list pipeline lands later).

## Rule 8 — Banned Trading Practices

**Source:** help.fundednext.com/en/articles/8020351-what-are-the-restricted-prohibited-trading-strategies

Catalog of explicitly forbidden strategies:

> Gambling Behavior (excessive margin usage 70% or more), Quick Strike Method
> (positions closed within 30 seconds of opening at 30% or higher of total
> recorded profit), High-Frequency Trading (HFT), Copy Trading Across Accounts,
> Group Hedging / Hedging Across Accounts, Arbitrage Trading, Tick Scalping,
> Grid Trading, Latency Trading, Account Rolling, One-Sided Betting,
> Hyperactivity (200 trades or 2,000 server messages in a single day),
> Account / Device Sharing.

**Predicates:** `fundednext_hft_check`, `fundednext_latency_arb_check`,
`fundednext_copy_trading_check`, `fundednext_martingale_check` (no martingale
mentioned by FundedNext; predicate kept for cross-firm symmetry and flagged
uncertain), `fundednext_hyperactivity_check`.

### 8a. HFT

**Source:** help.fundednext.com/en/articles/11982271-does-fundednext-allow-hft-high-frequency-trading

> No, FundedNext does not allow High-Frequency Trading (HFT).

No numeric threshold published; FundedNext describes HFT qualitatively as
"hundreds or thousands of trades in seconds."
**Confidence: uncertain.**
**Predicate name:** `fundednext_hft_check`.

### 8b. Latency Arbitrage

> Latency Trading — exploiting delayed market data or delays in execution.

Categorical prohibition, no numeric threshold.
**Confidence: uncertain.**
**Predicate name:** `fundednext_latency_arb_check`.

### 8c. Copy Trading

**Source:** help.fundednext.com/en/articles/8019805-what-is-the-copy-trading-rule-at-fundednext

> Copy trading between multiple FundedNext Challenge Accounts owned by the same individual is permitted, provided the combined capital does not exceed USD 300,000. One account must be designated as the Master Account with others functioning as Slave Accounts.

> Copy trading is strictly prohibited between a FundedNext Account and any other FundedNext Account or FundedNext Challenge Account, whether it belongs to the same individual or another entity.

**Numeric:** **USD 300,000** combined-capital cap on same-owner challenge accounts.
**Confidence: high** — published numerically. Categorical prohibition on
different-owner copy-trading.
**Predicate name:** `fundednext_copy_trading_check`.

### 8d. Hyperactivity

> 200 trades or 2,000 server messages in a single day.

**Numeric:** 200 trades / 2000 server messages per day.
**Confidence: high** — published numerically.
**Predicate name:** `fundednext_hyperactivity_check`.

### 8e. Martingale

FundedNext's banned-technique catalog does not list martingale by name;
predicate carried for cross-firm symmetry and flagged uncertain.
Reviewer note: kept to avoid kill-switching on a rule FundedNext doesn't
enforce.

**Confidence: uncertain.**
**Predicate name:** `fundednext_martingale_check`.

## Summary classification table

| Predicate | Confidence | Rule type |
| --- | --- | --- |
| `fundednext_stellar_2step_daily_drawdown` | high | Numeric: 5% / starting balance |
| `fundednext_stellar_2step_max_drawdown` | high | Numeric: 10% / starting balance, non-trailing |
| `fundednext_stellar_2step_profit_target_phase1` | high | Numeric: 8% / starting balance |
| `fundednext_stellar_2step_profit_target_phase2` | high | Numeric: 5% / starting balance |
| `fundednext_stellar_2step_min_trading_days` | high | Numeric: 5 days |
| `fundednext_stellar_1step_daily_drawdown` | high | Numeric: 3% / starting balance |
| `fundednext_stellar_1step_max_drawdown` | high | Numeric: 6% / starting balance, non-trailing |
| `fundednext_stellar_1step_profit_target` | high | Numeric: 10% / starting balance |
| `fundednext_stellar_1step_min_trading_days` | high | Numeric: 2 days |
| `fundednext_stellar_lite_daily_drawdown` | high | Numeric: 4% / starting balance |
| `fundednext_stellar_lite_max_drawdown` | high | Numeric: 8% / starting balance, non-trailing |
| `fundednext_stellar_lite_profit_target_phase1` | high | Numeric: 8% / starting balance |
| `fundednext_stellar_lite_profit_target_phase2` | high | Numeric: 4% / starting balance |
| `fundednext_stellar_lite_min_trading_days` | high | Numeric: 5 days |
| `fundednext_time_limit` | high | "No time limit" is itself explicit |
| `fundednext_news_blackout_window` | high | Numeric: 5 min pre/post (funded only) |
| `fundednext_copy_trading_check` | high | Numeric: USD 300,000 combined / categorical for diff-owner |
| `fundednext_hyperactivity_check` | high | Numeric: 200 trades / 2000 msgs per day |
| `fundednext_hft_check` | uncertain | No published numeric threshold |
| `fundednext_latency_arb_check` | uncertain | No published numeric threshold |
| `fundednext_martingale_check` | uncertain | Not in FundedNext catalog; carried for symmetry |
| `fundednext_consistency_check` | uncertain | No published single-day-profit-share threshold |

## Drift check

If FundedNext updates any help-center article changing a numeric threshold, this
file MUST be replaced (do not edit in place — create a new dated snapshot)
and the matching predicates' `tos_quote` fields in
`src/propfarm/rules/fundednext.py` bumped to match.
`test_fundednext_predicate_tos_quotes_appear_in_snapshot` will fail on the next
CI run after the predicate quote changes if the snapshot file has not been
updated, closing the silent-drift window.
