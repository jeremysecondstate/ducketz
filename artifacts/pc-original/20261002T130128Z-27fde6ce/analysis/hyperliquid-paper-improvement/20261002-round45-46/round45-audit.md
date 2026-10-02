# Hyperliquid Paper round 45 closeout and round 46 handoff

Audit written: 2026-10-02T12:53:16.865222+00:00

## Outcome

Round 45 was a one-hour challenge, seed 2026-10-02T10:22:17.434258699+00:00, due 2026-10-02T11:22:17.434258+00:00. Its committed Paper endpoint was 2026-10-02T12:24:47.460563+00:00, **3,750.026 seconds late**, beyond the five-minute window. Native cadence recorded **UNSCORED (`late_unscored`)** and retained one hour; the assessment was consumed once after round 46 was accepted.

## Endpoint comparison (descriptive; not a scored LOSS)

| Measure | Paper | Actual | Edge / result |
| --- | ---: | ---: | ---: |
| Common-mark equity | $44,713.77 | $44,865.47 | $-151.70 Paper minus actual |
| Return since opening | -0.1729% | 0.1658% | -0.3387 percentage points |
| Duckets displayed equity | $44,713.80 | $44,860.64 | $-146.84 raw display gap |
| Paper costs through endpoint | fees $36.821858; funding $-0.009767; turnover $64,926.84 | — | Return is after these recorded costs. |

All public-account flow histories were complete and empty; mirror baseline and performance comparability passed. Observation skew was 14.116s of 120s. The timing gate failed because the endpoint was late, so the negative edge is descriptive only. Account reads were sequential, not atomic.

| Account | Paper common-mark equity | Paper P&L | Actual common-mark equity | Actual P&L | Paper minus actual | Paper fees / funding |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Alex | $5,885.89 | $35.77 | $5,948.95 | $98.83 | $-63.07 | $8.27 / $0.000319 |
| Clearpond | $33,461.63 | $-70.50 | $33,581.39 | $49.26 | $-119.76 | $21.29 / $0.000000 |
| Jeremy | $5,366.25 | $-42.71 | $5,335.12 | $-73.84 | $31.12 | $7.26 / $-0.010086 |

## Stopped round 45 ledger through 12:26:22 UTC

The stopped ledger contains 384 decisions and 92 fills over 368 cycles. All 92 fills were forecast-linked: 50 Qualified and 42 Research. Turnover was $65,097.31, fees $36.902067, funding $-0.009767, realized P&L $-493.97, transfers 0; maximum pooled drawdown was $78.54 (0.1754%). SQLite integrity: ok.

Final stopped-ledger account balances: Alex $5,885.89, Clearpond $33,461.63, Jeremy $5,366.42, Pooled $44,713.94.

Decision outcomes: fill signal_rebalance: 91, hold gross_capacity_exhausted: 4, fill risk_cap: 1, hold opposite_account_direction: 79, hold below_rebalance_threshold: 114, hold forecast_unavailable: 72, skip below_size_precision: 11, skip below_min_notional: 12.

Initial inventory adjustment (first eight fills): $61,802.73 turnover, $35.383068 fees, $-494.05 realized P&L. Included in round totals. The remaining 84 fills added $3,294.58 turnover and $1.518998 fees.

Endpoint attribution by coin: BTC: 23 fills, turnover $22,461.54, fees $15.52, post-fee/pre-funding P&L $-51.26; ETH: 26 fills, turnover $3,727.89, fees $2.37, post-fee/pre-funding P&L $-7.26; HYPE: 14 fills, turnover $19,314.59, fees $9.82, post-fee/pre-funding P&L $-11.00; ZEC: 26 fills, turnover $19,422.82, fees $9.10, post-fee/pre-funding P&L $-7.90.

## Accepted round 46 opening and runtime

Round 46 was accepted with a fresh one-for-one opening at 2026-10-02T12:31:05.198404074+00:00; pooled opening equity $45,009.58 across Alex, Clear Pond, and Jeremy. Independent opening verification passed for ten inherited positions, reconciled signed quantities/collateral/cash/basis, zero opening fills/fees/funding/transfers/P&L, and zero source open orders. The exploratory Paper policy remains `88cd1a6a008df9b9`.

The first trading verification passed (7 cycles, 8 fills, 12 decisions; zero transfers/funding at that check). First eight inventory-adjustment fills cost $35.499713 on $61,968.70 turnover and realized $-534.40; these costs remain in the running ledger.

Closeout health passed at 2026-10-02T12:41:58.066665+00:00 (portfolio valuation 2026-10-02T12:41:28.393811+00:00): Paper PID 72748, models PID 61436, coordinator PID 56964 alive; no warnings; Powder inactive. Current round 46 due time is 2026-10-02T13:31:05.198404+00:00 UTC.

The five-family bundle and 70/15/15 recipe were retained. Fresh BTC/ZEC forecasts were Qualified; ETH/HYPE remained Research. Each slot reports logistic, extra trees, histogram gradient boosting, MLP, and random forest. No model retune, application-source/configuration change, or source queue item. No software tests ran because no implementation changed. The archive has 461 files / 499,875,773 bytes; manifest SHA-256 `bdec38dd36c948963a812168bca4dd8438ab2acf86618bd96d53e42ba03e5c91`.

## Recovery notes and evidence

The first hidden model launch failed because the post-archive `_models/_runtime` log directory was absent; the corrected launch wrote logs under the cycle evidence directory. One early health attempt saw stale forecasts before the next 5m candle; the coordinator published the next candle, fresh forecasts arrived, and later health checks passed. These recoveries did not change the recipe.

Comparison: `C:\DATASTORE\hyperliquid\_operations\paper-improvement\20261002-paper-round-45\round45-closing-comparison-20261002T122452Z.json` (SHA-256 `1e2a95884fa07e93193124c31ce6b8ed52ae2ebe00019eb06cd7d83d20114aa5`). Cadence assessment: `C:\DATASTORE\hyperliquid\_operations\paper-improvement-cadence\assessments\20261002-paper-round-45.json` (SHA-256 `cf68d6c14905970498739d4841fe5afce158e7abbb921d18244f11f48eb2fb15`). Round 46 evidence: `C:\DATASTORE\hyperliquid\_operations\paper-improvement\20261002-paper-round-46`. Cadence state: `C:\DATASTORE\hyperliquid\_operations\paper-improvement-cadence.json`.
