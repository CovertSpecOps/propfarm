# FundingPips — Overnight swap (rollover financing) snapshot

> **Retrieval date:** 2026-05-12
> **Companion task:** prop-farm Phase 0, Task 6.3 (Swap/financing, triple-Wednesday rule)
> **Scope:** Six symbols in `propfarm.data.quality.SUPPORTED_SYMBOLS` —
>   EURUSD, GBPUSD, USDJPY, XAUUSD, GER40, US100.

## Source URLs

| URL | Result |
| --- | --- |
| https://fundingpips.com/trading | Narrative trading-conditions page; no per-symbol swap table published (matches the parallel commission snapshot's finding). |
| MT5 terminal — Symbol Specification dialog | **Canonical source.** Not reachable from this host. |
| Community-archived FundingPips swap tables (mid-2025) | Non-authoritative seed for the numbers below. |

Companion to `ftmo-swap-2026-05-12.md` and `fundednext-swap-2026-05-12.md`.

---

## Triple-rollover convention

FundingPips runs MT5 with the standard FX convention — the **3x** daily
swap is charged on the **Wednesday rollover at 22:00 New York time**.

| field                       | value                                |
| --------------------------- | ------------------------------------ |
| triple-rollover weekday     | **Wednesday** (Python `weekday()=2`) |
| rollover hour (server time) | 22:00 New York time (US/Eastern)     |
| account types (Phase 0)     | FundingPips "Student" / "Practitioner" MT5 |
| swap-free / Islamic offered | **Yes** — opt-in flag on signup; modeled separately if used. |

---

## Swap-rate table (FundingPips MT5, non-swap-free)

| symbol  | swap_long (pts/lot/night) | swap_short (pts/lot/night) | point_value_usd | confidence |
| ------- | ------------------------- | -------------------------- | --------------- | ---------- |
| EURUSD  | -7.50                     | +2.10                      | 1.00            | UNCERTAIN  |
| GBPUSD  | -3.60                     | -0.90                      | 1.00            | UNCERTAIN  |
| USDJPY  | +8.40                     | -14.80                     | 1.00            | UNCERTAIN  |
| XAUUSD  | -23.00                    | +10.00                     | 1.00            | UNCERTAIN  |
| GER40   | -1.20                     | -0.45                      | 1.00            | UNCERTAIN  |
| US100   | -2.70                     | -1.00                      | 1.00            | UNCERTAIN  |

### Source-of-record gap

FundingPips' public page describes trading conditions in narrative form
and does not publish a per-symbol swap table. The canonical source is the
broker MT5 terminal's Symbol Specification dialog, unreachable from this
implementation host. Values seeded from publicly-archived FundingPips
community references; all rows flagged **UNCERTAIN**.

### Sign convention

Same as the other firms — positive = broker pays trader; the simulator
inverts on output. See module docstring.
