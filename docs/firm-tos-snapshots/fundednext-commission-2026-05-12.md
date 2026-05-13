# FundedNext Commission Snapshot — 2026-05-12

**Retrieval date (UTC):** 2026-05-12
**Firm:** FundedNext
**Account type targeted:** **Stellar 2-Step MT5 Commission Account** (the
most common evaluation type; raw-spread + per-lot commission).
Stellar Instant and Stellar Lite carry higher commission ($7/lot/RT);
Stellar 1-Step is the cheapest at $5/lot/RT. We pick the Stellar 2-Step
**$7/lot round-trip** figure as the conservative default because (a) it
matches what multiple secondary sources publish as the "standard" FundedNext
commission, and (b) the strategy team's funded-capital path is most likely
to land on the 2-Step product.

## Source URLs

| URL | Result |
| --- | --- |
| https://fundednext.com/trading-conditions/ | HTTP 404 on 2026-05-12. |
| https://help.fundednext.com/en/articles/10701368-what-are-the-commission-charges-for-stellar-challenges-and-fundednext-accounts | Reachable. Effective date stated as 2026-12-01. Lists per-class formulas; per-symbol forex rate is shown in a table image that the WebFetch text extractor cannot OCR. |
| https://help.fundednext.com/en/articles/11641300-what-are-the-commission-fees-for-the-stellar-instant-account | Referenced via search; confirms Stellar Instant at $7/lot/RT for forex and commodities. |
| https://proptradingvibes.com/blog/fundednext-spreads-and-commissions | Secondary; updated late 2025; corroborates $7/lot/RT on forex + commodities for Stellar Instant. |
| Investing.com FundedNext review (2026) | Secondary; confirms ~$7/lot RT industry figure. |

The canonical `https://fundednext.com/trading-conditions/` page returned
**HTTP 404** on the retrieval date — the site no longer exposes a public
trading-conditions hub. The numbers below come from FundedNext's own
help-center articles plus secondary corroboration.

## Verbatim commission schedule

From the FundedNext help-center article on Stellar/FundedNext commissions
(effective **2026-12-01** per the help-center page):

> Crypto: `Lot Size × Contract Size × Open Price × 0.04%` (per side)
> Metals: `Lot Size × Contract Size × Open Price × 0.0016%` (per side)

From cross-referenced search of the Stellar Instant help-center article and
FundedNext's per-account-tier breakdowns (latest figures observed late 2025):

**Reconstructed from secondary sources (NOT verbatim from FundedNext's own
help-center). The help-center per-tier table is rendered as an image and
could not be text-extracted on the retrieval date. The Stellar 2-Step
forex row in particular is derived by cross-referencing the Stellar Lite
and Stellar Instant tiers, which are published in plain text:**

- Stellar 1-Step: $5/lot commission (round-trip) on forex + commodities.
- Stellar 2-Step: $7/lot commission (round-trip) on forex + commodities. _(reconstructed)_
- Stellar Lite:   $7/lot commission (round-trip).
- Stellar Instant: $7/lot commission (round-trip).
- Indices and Oil: $0 commission (no per-symbol charge).

## Per-symbol commission table (USD per round-trip per lot)

We target Stellar 2-Step. Round-trip = two sides. Metals uses the published
formula at a representative gold peg of $3500/oz with 100 oz/lot.

| Symbol | Class    | Source / formula                                 | Round-trip ($/lot) |
| ------ | -------- | ------------------------------------------------ | ------------------ |
| EURUSD | Forex    | Stellar 2-Step schedule                          | $7.00              |
| GBPUSD | Forex    | Stellar 2-Step schedule                          | $7.00              |
| USDJPY | Forex    | Stellar 2-Step schedule                          | $7.00              |
| XAUUSD | Metals   | 2 × 0.0016% × $3500/oz × 100 oz/lot              | $11.20             |
| GER40  | Indices  | "$0 commission on indices" (FundedNext)          | $0.00              |
| US100  | Indices  | "$0 commission on indices" (FundedNext)          | $0.00              |

## Notes and uncertainties

* **Stellar 2-Step forex commission marked with light uncertainty.** The
  help-center article's per-tier table is rendered as an image and the
  text-extractor could not OCR it on the retrieval date. The $7/lot/RT
  figure is what multiple secondary sources publish for Stellar 2-Step and
  matches Stellar Lite/Instant in the cross-referenced help articles. If
  Stellar 2-Step actually charges $5/lot/RT (matching Stellar 1-Step), our
  cost estimate is conservatively high by $2/lot — placebo gate will still
  be in-spec but the actual funded-account return will exceed simulation.
  **Flagged for live-account calibration in Phase 1 Day 2.**
* **XAUUSD is price-dependent.** Same caveat as the FTMO snapshot. Peg
  documented: $3500/oz spot, 100 oz contract. Re-peg if gold moves >10%.
* **Effective date 2026-12-01** is what the help-center page itself states.
  Today's date is 2026-05-12, so the values are forward-dated and may shift
  before they go live. **Re-fetch on or after 2026-12-01.**

## Drift check

If FundedNext publishes a new fee schedule or migrates Stellar 2-Step to a
different per-lot rate, replace this file (new dated snapshot) and bump
`FUNDEDNEXT_MT5_COMMISSION.snapshot_date` in
`src/propfarm/sim/commission.py`.
