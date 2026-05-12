# FundedNext — Overnight swap (rollover financing) snapshot

> **Retrieval date:** 2026-05-12
> **Companion task:** prop-farm Phase 0, Task 6.3 (Swap/financing, triple-Wednesday rule)
> **Scope:** Six symbols in `propfarm.data.quality.SUPPORTED_SYMBOLS` —
>   EURUSD, GBPUSD, USDJPY, XAUUSD, GER40, US100.

## Source URLs

| URL | Result |
| --- | --- |
| https://fundednext.com/trading-conditions | HTTP 403 / dynamic JS on 2026-05-12 (matches the parallel commission snapshot's finding). Public swap table not extractable. |
| MT5 terminal — Symbol Specification dialog | **Canonical source.** Not reachable from this host. |
| Community-archived FundedNext swap tables (mid-2025) | Non-authoritative seed for the numbers below. |

Companion to `ftmo-swap-2026-05-12.md`. Same fields, same caveats. Every
row is flagged **CONFIRMED** or **UNCERTAIN**.

---

## Triple-rollover convention

FundedNext follows the **standard MT5 / FX-market convention**: the 3x
daily swap is charged at the **Wednesday 22:00 New York-time rollover**.
The server uses the same EET/EEST timezone family as FTMO, but the
rollover is anchored to NY (FX-wide convention), not Cairo / Eastern Europe.

| field                       | value                                |
| --------------------------- | ------------------------------------ |
| triple-rollover weekday     | **Wednesday** (Python `weekday()=2`) |
| rollover hour (server time) | 22:00 New York time (US/Eastern)     |
| account types (Phase 0)     | FundedNext "Stellar" MT5             |
| swap-free / Islamic offered | **Yes** — "Stellar Swap-Free" variant. Deferred to Phase 1; not modeled here. |

---

## Swap-rate table (FundedNext MT5, non-swap-free)

| symbol  | swap_long (pts/lot/night) | swap_short (pts/lot/night) | point_value_usd | confidence |
| ------- | ------------------------- | -------------------------- | --------------- | ---------- |
| EURUSD  | -6.80                     | +1.90                      | 1.00            | UNCERTAIN  |
| GBPUSD  | -3.10                     | -0.60                      | 1.00            | UNCERTAIN  |
| USDJPY  | +7.50                     | -13.40                     | 1.00            | UNCERTAIN  |
| XAUUSD  | -21.00                    | +9.20                      | 1.00            | UNCERTAIN  |
| GER40   | -0.95                     | -0.35                      | 1.00            | UNCERTAIN  |
| US100   | -2.30                     | -0.80                      | 1.00            | UNCERTAIN  |

### Source-of-record gap

FundedNext's public trading-conditions page exposes typical spreads and
commission but routes per-symbol swap rates to the MT5 client. Same
limitation as the FTMO snapshot: this host has no MT5 terminal, so the
table is seeded from publicly-archived FundedNext community references
and flagged UNCERTAIN. Refresh required before Phase 1 live work.

### Sign convention

Same as the FTMO snapshot — positive in the table means **broker pays
trader**; the simulator inverts on output so its return is "positive = cost".
