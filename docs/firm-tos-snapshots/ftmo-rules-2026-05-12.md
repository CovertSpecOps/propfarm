# FTMO Rule Predicates Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FTMO
**Scope:** Rule predicates (drawdown, profit target, banned techniques, news,
consistency, time limits) — **not** commission or swap; those are covered by
`ftmo-commission-2026-05-12.md` and `ftmo-swap-2026-05-12.md`.

## Source URLs

| URL | Result |
| --- | --- |
| `https://ftmo.com/en/trading-conditions/` | **HTTP 404** on 2026-05-12 (page restructured into "Simulated Assets" hub). |
| `https://help.ftmo.com/` | Reachable (200). Help-center root; rule pages indexed below. |
| `https://help.ftmo.com/en/articles/9722943-how-much-can-i-lose-in-one-day` | Reachable. Daily loss limit definition. |
| `https://help.ftmo.com/en/articles/9722942-what-is-the-maximum-loss` | Reachable. Maximum loss (overall) definition. |
| `https://help.ftmo.com/en/articles/9722944-what-is-the-profit-target` | Reachable. Profit target per challenge type. |
| `https://ftmo.com/en/forbidden-trading-practices/` | **HTTP 403** on 2026-05-12 (Cloudflare bot challenge). Substitute used. |
| `https://help.ftmo.com/en/articles/9722950-what-are-the-forbidden-trading-practices` | Reachable substitute for forbidden practices. |
| `https://ftmo.com/en/account-mt-server-time/` | Reachable. FTMO server timezone (GMT+2/+3 DST). |

The canonical `https://ftmo.com/en/forbidden-trading-practices/` URL returned
**HTTP 403** (Cloudflare bot challenge) on the retrieval date; the help-center
mirror at `help.ftmo.com/.../what-are-the-forbidden-trading-practices` was used
as the substitute. The `https://ftmo.com/en/trading-conditions/` hub returned
**404** — same pattern observed during W3 commission/swap snapshotting; the
help-center articles are now the only stable primary source for rule text.

## Server time / DST

From `account-mt-server-time`:

> The FTMO MetaTrader platform server time is GMT+2 (Eastern European Time,
> EET) during winter and GMT+3 (Eastern European Summer Time, EEST) during
> summer, following the standard European DST schedule. Daily reset (the
> "trading day") begins and ends at midnight server time (00:00 EET / EEST).

**UTC mapping:**
- Winter (EET = UTC+2): server midnight = **22:00 UTC on the previous calendar day**.
- Summer (EEST = UTC+3): server midnight = **21:00 UTC on the previous calendar day**.
- EU DST transitions: last Sunday of March (forward), last Sunday of October (back).

The Daily Drawdown predicate's `daily_start_equity` field MUST be the equity
captured at the most recent server-midnight crossing, computed via
`zoneinfo.ZoneInfo("Europe/Athens")` — the canonical IANA zone for EET/EEST.
**Important:** FTMO's company office is in Prague, which observes CET/CEST
(UTC+1/+2), but FTMO's MT5 server clock is intentionally set to EET/EEST
(UTC+2/+3) — one hour off from the office clock. Using `Europe/Prague`
would be wrong year-round. DST is handled by the system tz database, not by
hard-coded UTC offsets.

## Rule 1 — Maximum Daily Loss (Daily Drawdown)

**Source:** help.ftmo.com/en/articles/9722943-how-much-can-i-lose-in-one-day

> The Maximum Daily Loss is equal to 5% of the initial account balance. The
> Maximum Daily Loss rule says that, in any given calendar day (CET/CEST
> server time), the result of all closed positions in sum together with the
> currently open floating profits/losses on your account must not hit the
> determined Maximum Daily Loss value. The day starts at 00:00:00 server time
> and ends at 23:59:59 server time. The daily loss limit resets at midnight
> server time.

**Numeric:** 5% of starting balance. Evaluated on **equity** (closed PnL +
unrealized floating PnL), not balance.

**Confidence: high.** Numeric threshold, published, unambiguous.

**Predicate name:** `ftmo_daily_drawdown`.

## Rule 2 — Maximum Loss (Overall / Max DD)

**Source:** help.ftmo.com/en/articles/9722942-what-is-the-maximum-loss

> The Maximum Loss rule says that the result of all closed positions in sum
> together with the currently open floating profits/losses on your account
> must not hit the determined Maximum Loss value at any time during the
> challenge or verification. The Maximum Loss is equal to 10% of the initial
> account balance. This loss limit is calculated from the initial account
> balance, not from the highest balance reached.

**Critical detail:** As of the help-center revision retrieved 2026-05-12, FTMO
calculates Max Loss against the **initial account balance** (i.e. NOT
trailing). This is a change from the pre-2023 "trailing from highest balance"
rule that the previous Maximum Loss formula used. The predicate encodes the
**non-trailing 10% from starting balance** rule consistent with the current
help-center text.

**Numeric:** 10% of starting balance. Non-trailing.

**Confidence: high.**

**Predicate name:** `ftmo_max_drawdown`.

## Rule 3 — Profit Target (Challenge / Verification)

**Source:** help.ftmo.com/en/articles/9722944-what-is-the-profit-target

> The Profit Target is a minimum required profit you need to reach to fulfill
> the trading objectives. The Profit Target on the FTMO Challenge is 10% of
> the initial account balance. The Profit Target on the Verification is 5%
> of the initial account balance. There is no Profit Target on the FTMO
> Account (funded stage).

**Numeric:** One-step (FTMO Challenge alone): **10%**. Two-step
(Challenge + Verification): **10% + 5%**. Funded: no target.

**Confidence: high.**

**Predicate names:** `ftmo_profit_target_one_step`, `ftmo_profit_target_two_step_challenge`,
`ftmo_profit_target_two_step_verification`.

**Semantics note:** Hitting the profit target is a **completion** event, not a
**failure** event. The predicate returns a `Violation` with `severity="warn"`
(see ABC convention) carrying the message "profit target reached", and the
state machine (Task 12.1) reads this as a phase transition trigger. This is
why the ABC's `Violation` carries both `severity` and `predicate_name` — the
consumer dispatches on `predicate_name`, not just severity. See ABC docstring.

## Rule 4 — Forbidden Trading Practices

**Source:** help.ftmo.com/en/articles/9722950-what-are-the-forbidden-trading-practices

> The following trading practices are forbidden and may lead to immediate
> termination of the account: (a) the opening of trades using high-frequency
> trading strategies that do not reflect realistic market behavior; (b) the
> exploitation of inefficiencies in our simulated trading environment,
> including but not limited to latency arbitrage, hedge arbitrage between
> accounts, reverse arbitrage, and tick scalping; (c) the operation of
> identical trading strategies across multiple accounts whose combined
> capital exceeds USD 400,000 or equivalent; (d) the use of copy-trading
> services and the mirroring of trades between separate accounts; (e) the
> use of grid strategies, martingale strategies, or any other strategy that
> increases position size after a loss to chase a recovery.

**Per-practice classification:**

### 4a. HFT (high-frequency trading)

> "trading strategies that do not reflect realistic market behavior"

**No numeric threshold published.** Working interpretation: > 5 orders per
minute sustained over a 10-minute window on the same account is a smoke-test
proxy. This is **not** what FTMO publishes — they reserve the right to define
HFT after the fact.

**Confidence: uncertain.**

**Predicate name:** `ftmo_hft_check`.

**Interpretation:** "More than 5 orders submitted in any 60-second window,
sustained over 10 consecutive minutes, on a single account." Sustained-window
heuristic chosen to avoid tripping on a single news-driven cluster.

### 4b. Latency arbitrage

> "the exploitation of inefficiencies in our simulated trading environment,
> including but not limited to latency arbitrage"

**No numeric threshold.** Working smoke-test: average round-trip latency
between order submission and the corresponding price tick on FTMO's MT5 feed
< 50 ms over a 20-trade sample suggests the trader has a faster price feed
than FTMO does, which is the structural prerequisite for latency arb.

**Confidence: uncertain.**

**Predicate name:** `ftmo_latency_arb_check`.

**Interpretation:** "Average submit-to-fill RTT < 50 ms across a 20-trade
rolling window suggests an external faster price source is being arbitraged."
Conservative; FTMO has terminated accounts for this without publishing a
number.

### 4c. Same EA across > USD 400,000 combined capital

> "the operation of identical trading strategies across multiple accounts
> whose combined capital exceeds USD 400,000 or equivalent"

**Numeric:** USD 400,000 combined. FTMO publishes this threshold explicitly.

**Confidence: high.**

**Predicate name:** `ftmo_same_ea_check`.

### 4d. Copy-trading

> "the use of copy-trading services and the mirroring of trades between
> separate accounts"

Explicit categorical prohibition. No numeric ambiguity.

**Confidence: high.**

**Predicate name:** `ftmo_copy_trading_check`.

### 4e. Grid / martingale

> "the use of grid strategies, martingale strategies, or any other strategy
> that increases position size after a loss to chase a recovery"

Explicit categorical prohibition. The predicate flags any sizing function
that monotonically scales up after consecutive losses. The categorical
prohibition itself is high-confidence; the **detection heuristic** (which
sizing patterns count as martingale) is interpretive. Predicate is split:

- `ftmo_martingale_check` — confidence **uncertain** (heuristic detection).

**Predicate name:** `ftmo_martingale_check`.

## Rule 5 — News blackout (funded stage only)

**Source:** help.ftmo.com (news-restriction article, retrieved 2026-05-12)

> On the FTMO Account, you must not open or close any position within 2
> minutes before or after a high-impact news release as listed on the
> Forex Factory calendar.

**Numeric on time window:** 2 minutes pre / 2 minutes post. **High
confidence** on the window itself. **Uncertain** on which events count as
"high-impact" (FTMO does not publish the calendar; Forex Factory's
high-impact tagging is a third-party classification).

**Predicate split:**
- `ftmo_news_blackout_window` — confidence **high** (the 2-minute window is published).
- The news-list filtering is deferred (out of scope for W4a; the rule
  consumes whatever news list the caller provides).

**Predicate name:** `ftmo_news_blackout_window`.

**Note:** News blackout applies on the **FTMO Account** (funded) stage only;
the Challenge and Verification phases do not have a news rule. The predicate
exposes a `funded_only=True` marker so the rules engine can skip evaluation
during Challenge/Verification.

## Rule 6 — Consistency rule

**Source:** help.ftmo.com — "consistency" article (retrieved 2026-05-12)

> We may review trading consistency on a case-by-case basis. We do not
> publish a single-day-profit-share threshold; however, accounts where more
> than 50% of total profit was earned on a single day may be flagged for
> manual review prior to payout.

**No hard numeric rule** but a working threshold of 50% single-day share is
indicated in support correspondence. Predicate classification:

- `ftmo_consistency_check` — confidence **uncertain** (FTMO reviews
  case-by-case; 50% is a working heuristic, not a published rule).

**Predicate name:** `ftmo_consistency_check`.

## Rule 7 — Minimum trading days

**Source:** help.ftmo.com/en/articles/9722944-what-is-the-profit-target (same article)

> You must trade on at least 4 different days during the FTMO Challenge and
> on at least 4 different days during the Verification.

**Numeric:** 4 trading days each phase.

**Confidence: high.**

**Predicate name:** `ftmo_min_trading_days`.

**Semantics:** Like the profit-target predicate, this is a **completion-gate**
rule, not a kill rule. Failure to reach 4 days at end-of-phase means the
phase cannot complete; it does not terminate the account mid-phase. Returns
`severity="warn"` and the state machine handles the gating.

## Rule 8 — Time limits (Challenge / Verification)

**Source:** help.ftmo.com (challenge-duration article, retrieved 2026-05-12)

> The FTMO Challenge has no maximum duration. The Verification also has no
> maximum duration. Previously these phases had 30-day and 60-day limits;
> the limits were removed in 2023.

**As of 2026-05-12: no time limit.** A `ftmo_time_limit` predicate is
implemented but returns `None` unconditionally for any FTMO account at
current ToS; it is kept for symmetry with other firms (FundedNext /
FundingPips still publish time limits per W4b scope) and to make a future
re-introduction of a time limit a one-line predicate update.

**Confidence: high** (the "no time limit" status is itself explicit).

**Predicate name:** `ftmo_time_limit`.

## Summary classification table

| Predicate | Confidence | Rule type |
| --- | --- | --- |
| `ftmo_daily_drawdown` | high | Numeric: 5% / starting balance, on equity |
| `ftmo_max_drawdown` | high | Numeric: 10% / starting balance, non-trailing |
| `ftmo_profit_target_one_step` | high | Numeric: 10% / starting balance |
| `ftmo_profit_target_two_step_challenge` | high | Numeric: 10% / starting balance |
| `ftmo_profit_target_two_step_verification` | high | Numeric: 5% / starting balance |
| `ftmo_min_trading_days` | high | Numeric: 4 days |
| `ftmo_time_limit` | high | "No time limit" is itself explicit |
| `ftmo_news_blackout_window` | high | Numeric: 2 min pre/post (funded only) |
| `ftmo_same_ea_check` | high | Numeric: USD 400,000 combined |
| `ftmo_copy_trading_check` | high | Categorical prohibition |
| `ftmo_hft_check` | uncertain | No published numeric threshold |
| `ftmo_latency_arb_check` | uncertain | No published numeric threshold |
| `ftmo_martingale_check` | uncertain | Categorical rule, heuristic detection |
| `ftmo_consistency_check` | uncertain | "Case-by-case" review |

## Drift check

If FTMO updates any help-center article changing a numeric threshold, this
file MUST be replaced (do not edit in place — create a new dated snapshot)
and the matching predicates' `tos_quote` fields in `src/propfarm/rules/ftmo.py`
bumped to match. The `test_ftmo_predicate_tos_quotes_appear_in_snapshot` test
will fail on the next CI run after the predicate quote changes if the
snapshot file has not been updated, closing the silent-drift window.
