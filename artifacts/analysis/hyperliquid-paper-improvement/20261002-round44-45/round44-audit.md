# Hyperliquid Paper Improvement — Round 44 closeout

Completed 2026-10-02T10:59:30.864277+00:00 UTC. Native cadence result: **UNSCORED (`late_unscored`)**. Round 44's one-hour endpoint was due 2026-10-02T08:59:32.930325+00:00; the committed Paper observation was 2026-10-02T10:12:57.121991+00:00 (4404.191666 seconds late), and the actual account reads finished 2026-10-02T10:13:34.527913+00:00. The observation skew was 37.405922 seconds, under the native 120-second skew limit. Both endpoints missed the five-minute lateness allowance, so the native assessor withheld a score. The negative comparative result below is descriptive only; it does not count as a LOSS.

## Comparative result

- Opening pooled equity: $44,702.273546.
- Common-mark Paper equity: $44,699.004021; actual equity: $44,799.808518; Paper edge: $-100.804498.
- Paper return: -0.007314%; actual return: 0.218188%; after-cost excess return: -0.225502 percentage points.
- The native mirror-baseline, performance and complete-zero-external-flow gates passed. Zero transfers and no external cash flows were recorded. Timing alone made the round unscored.

| Account | Opening equity | Paper common mark | Actual common mark | Paper P&L | Fees | Funding | Turnover | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Alex | $6,036.64 | $5,986.55 | $5,922.94 | -$50.08 | $8.96 | +$0.23 | $19,910.78 | $50.44 (0.8355%) |
| Clear Pond | $33,399.06 | $33,423.84 | $33,523.12 | +$24.78 | $21.25 | $0.00 | $30,363.32 | $19.59 (0.0586%) |
| Jeremy | $5,266.57 | $5,288.62 | $5,353.75 | +$21.98 | $7.24 | -$0.17 | $16,095.57 | $7.12 (0.1344%) |
| **Pooled** | **$44,702.27** | **$44,699.00** | **$44,799.81** | **-$3.32** | **$37.46** | **+$0.05** | **$66,369.67** | **$39.43 (0.0881%)** |

Pooled activity was 401 committed cycles, 94 fills, 411 decisions, 16 funding records, and zero transfers. The $37.457180 fees and +$0.052635 funding are retained in the result. The initial inventory adjustment is shown separately below and remains included in all totals.

## Forecast attribution and unfilled decisions

All 94 fills carried a valid in-round forecast witness: 57 Qualified and 37 Research. The archive had 108 valid fresh forecasts (75 Qualified, 33 Research) and zero invalid forecasts. Fill reasons were 93 signal rebalances and one risk-cap reduction.

- Holds: 251 — rebalance threshold 98, forecast unavailable 87, opposite account direction 62, gross capacity exhausted 4.
- Skips: 66 — below quantity precision 60, below the executable $10 minimum 6.
- By market: BTC 27 fills / $22,798.21 turnover / $15.67 fees / +$30.56 realized; ETH 32 / $5,203.77 / $3.05 / +$0.52; HYPE 7 / $18,986.92 / $9.67 / -$170.59; ZEC 28 / $19,380.77 / $9.07 / -$235.46.

The first eight inventory-adjustment fills began at 08:02:07.952308 UTC: $61,511.4975 turnover, $35.24166675 fees and -$373.133443 realized P&L. Those startup costs remain in the round totals.

## Decision and retained recipe

Cadence assessment 2026-10-02T10:14:17.574469+00:00 recorded `late_unscored`; cadence advance consumed it once and kept the next window at one hour. No model or strategy retune was justified by an unscored endpoint. The five positive model families, 70/15/15 purged recipe, 0.50 shrinkage, five-minute candles, next-five-minute forecasts and 300-second source-progress refits remain. Paper keeps policy `88cd1a6a008df9b9` with Research admission and all quantity, cost and exposure safeguards. Powder remains inactive under its strict separate policy.

The documented follow-up hypothesis is scheduling/observation timing: capture both endpoint reads within the native five-minute allowance while retaining complete flow evidence. Do not reinterpret this round as a loss or change model weights from its descriptive negative edge.

## Next accepted round

Round 45 was independently mirrored and accepted at opening equity $44,791.216198, seeded 2026-10-02T10:22:17.434258699+00:00, with a one-hour due time of 2026-10-02T11:22:17.434258Z. Opening verification passed with zero opening fills, decisions, transfers, funding or P&L. The stopped Round 44 Paper/Models archive retained 510 files / 553,777,952 bytes and manifest SHA-256 `fba52d40b1b495905728d131960f05bade0dbe656e40e99c02baf319332704f8`; its checksums and SQLite integrity passed.

## Verification

Native comparison and cadence assessment passed their schema/comparability checks; archive preservation verified at 10:18:54.771362Z; Round 45 opening verifier passed at 10:22:17Z; first-cycle verifier passed at 10:25:34.818Z; native maintenance completed at 10:27:38.225Z; fresh trading health passed at 10:51:48.673440Z with Paper, models and coordinator alive, zero warnings, and Powder off. No application source or shared configuration changed, so no software tests or source-queue item applies.
