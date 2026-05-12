# FundingPips Commission Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FundingPips
**Account type targeted:** **Raw Assessment One-Step / Two-Step / Two-Step Pro
MT5 Account** (raw spread + per-lot commission). FundingPips' "FP Zero
Instant" / Zero Account carries a higher commission ($7/lot/RT for forex +
metals); we target the standard Raw Assessment products at **$5/lot/RT**.

## Source URLs

| URL | Result |
| --- | --- |
| https://fundingpips.com/ | HTTP 403 Forbidden on 2026-05-12 (Cloudflare bot challenge). |
| https://www.fxempire.com/prop-firms/fundingpips | Reachable. Detailed review with commission breakdown; secondary but FXEmpire is a recognised broker-review house. |
| https://proptradingvibes.com/blog/fundingpips-gold-strategy | Reachable, updated late 2025. Corroborates $5/RT forex + metals on Raw Assessment. |
| https://allproptradingfirms.com/what-are-the-spreads-and-commissions-at-funding-pips/ | Reachable, dated 2026 review. Same numbers. |

The canonical `https://fundingpips.com/` URL returned **HTTP 403 Forbidden**
on the retrieval date — Cloudflare blocks unauthenticated automated fetches.
Numbers come from independent broker-review houses corroborating the same
schedule across multiple sources.

## Verbatim commission schedule

From FXEmpire review (latest update visible on 2026-05-12):

> The Raw Assessment applies a $5 per round lot commission on forex and metals
> within the One Step, Two Step, and Two Step Pro accounts, while the Zero
> Instant account carries a $7 per lot commission. No commission is charged
> on indices and oil.

From the Prop Trading Vibes 2026 review:

> Forex and Metals: FP Zero: $7 per lot | 1-Step, 2-Step & 2-Step Pro: $5 per
> lot. For Indices: No commission. For Energy: No commission.

## Per-symbol commission table (USD per round-trip per lot)

We target the standard Raw Assessment (1-Step / 2-Step / 2-Step Pro) at
$5/lot/RT for forex and metals, zero for indices.

| Symbol | Class    | Source                                              | Round-trip ($/lot) |
| ------ | -------- | --------------------------------------------------- | ------------------ |
| EURUSD | Forex    | Raw Assessment $5/RT                                | $5.00              |
| GBPUSD | Forex    | Raw Assessment $5/RT                                | $5.00              |
| USDJPY | Forex    | Raw Assessment $5/RT                                | $5.00              |
| XAUUSD | Metals   | Raw Assessment $5/RT on metals                      | $5.00              |
| GER40  | Indices  | "No commission is charged on indices" (FundingPips) | $0.00              |
| US100  | Indices  | "No commission is charged on indices" (FundingPips) | $0.00              |

## Notes and uncertainties

* **Primary URL blocked.** All three FundingPips numbers come from secondary
  review houses (FXEmpire, Prop Trading Vibes, AllPropTradingFirms). The
  numbers are consistent across the three sources, which is the strongest
  evidence available short of a live MT5 symbol-spec readout.
* **Metals commission is flat $5/RT, not a formula.** Unlike FTMO and
  FundedNext which charge a percentage of notional on metals, FundingPips
  publishes a flat per-lot rate that matches their forex rate. No price-peg
  caveat needed.
* **No "as of" date** is published by FundingPips themselves on a canonical
  page (because the canonical page is blocked). Effective date is inferred
  from the most recent reviewer-published date: late-2025 / early-2026.

## Drift check

If FundingPips reopens their public commission page or changes the Raw
Assessment fee, replace this file (new dated snapshot) and bump
`FUNDINGPIPS_MT5_COMMISSION.snapshot_date` in
`src/propfarm/sim/commission.py`.
