# FTMO Commission Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FTMO
**Account type targeted:** **MT5 Commission Account** (lower spread, commission per lot).
The MT5 "Standard" / swap-free account has $0 commission but wider spreads;
we target the commission account because the strategies we plan (scalping,
intraday FX) are more sensitive to spread than to commission.

## Source URLs

| URL | Result |
| --- | --- |
| https://ftmo.com/en/trading-conditions/ | HTTP 404 on 2026-05-12 (page restructured). |
| https://ftmo.com/en/blog/trading-updates/trading-update-25-sep-2025/ | Reachable. Provides per-side commission by asset class effective 2025-09-29. |
| https://ftmo.com/en/blog/zero-commissions-on-indices/ | Reachable, dated 2025-08-01. Confirms zero commission on indices. |
| https://thepayoutreport.com/commission-cost-planner-for-ftmo-us-mt5/ | Secondary; dated 2025-08-27. Shows ~$5/round-trip on EURUSD/US100 worked examples. |

The canonical `https://ftmo.com/en/trading-conditions/` URL returned **HTTP 404** on
the retrieval date — the trading-conditions hub has been folded into the broader
"Simulated Assets" pages on the OANDA-backed platform. The numbers below come
from FTMO's own blog "trading update" series, which is the primary channel
FTMO uses to announce commission schedule changes, plus secondary corroboration
from The Payout Report.

## Verbatim commission schedule (effective 2025-09-29)

From `trading-update-25-sep-2025`:

> Forex & Exotics: Commission increases from "$1.50 per lot per side" to
> "$2.50 per lot per side". Metals CFD, Cash III CFD, Commodities: Commission
> increases from "0.0005% per volume per side" to "0.0007% per volume per side".

From `zero-commissions-on-indices` (2025-08-01):

> Trade indices with absolutely ZERO Commission ... trading simulated indices
> on the FTMO platform is completely commission-free ... applies to all index
> symbols on the FTMO platform.

## Per-symbol commission table (USD per round-trip per lot)

Round-trip = two sides. For % volume formulas we peg to a representative price
documented below; round-trip = `2 × price × contract_size × percentage`.

| Symbol | Class    | Formula / spec                          | Round-trip ($/lot) |
| ------ | -------- | --------------------------------------- | ------------------ |
| EURUSD | Forex    | 2 × $2.50/side                          | $5.00              |
| GBPUSD | Forex    | 2 × $2.50/side                          | $5.00              |
| USDJPY | Forex    | 2 × $2.50/side                          | $5.00              |
| XAUUSD | Metals   | 2 × 0.0007% × $3500/oz × 100 oz/lot     | $4.90              |
| GER40  | Indices  | Zero commission (per 2025-08-01 update) | $0.00              |
| US100  | Indices  | Zero commission (per 2025-08-01 update) | $0.00              |

## Notes and uncertainties

* **XAUUSD is price-dependent.** FTMO charges 0.0007% per side on notional, not
  a fixed per-lot amount. We bake in a fixed-USD-per-lot figure ($4.90) using a
  $3500/oz gold peg consistent with mid-2025–mid-2026 spot. When gold moves
  >10% from that peg the table needs to be re-pegged or the model upgraded to
  compute commission from a live price input. **Flagged for Task 7.x revisit.**
* **Indices are quoted zero** per FTMO's own marketing — verified verbatim from
  the August 2025 blog post. If FTMO reintroduces an index commission in the
  future, the snapshot date here will be stale and the table must be rebuilt.
* **No "as of" date is published directly on a single canonical commission
  page** because that page does not currently exist. Effective dates are
  inferred from the trading-update blog series: 2025-09-29 for the forex/metals
  numbers, 2025-08-01 for the index zero-commission confirmation.

## Drift check

If FTMO publishes a future trading-update blog post that changes any forex,
metals, or index commission, this file MUST be replaced (do not edit in place
— create a new dated snapshot) and the `FTMO_MT5_COMMISSION.snapshot_date`
constant in `src/propfarm/sim/commission.py` bumped to match.
