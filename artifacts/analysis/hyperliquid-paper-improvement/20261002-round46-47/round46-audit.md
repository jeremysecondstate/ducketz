# HYPER Paper improvement audit — Round 46 close and Round 47 handoff

Contract: PAPER_IMPROVEMENT 2026-09-30/v5. Coordination: cross-pc-v2, release 8516a9b40c300c02a28278d3e0016aac852cdb3e. Producer: Atlas / hyperliquid-paper-improvement / pc-original.

## Round 46 comparison

Round 46 was a one-hour challenge, seeded 2026-10-02T12:31:05.198404074Z, due 13:31:05.198404Z. The native comparison read Paper at 14:33:25.493285Z and completed the same three public account reads at 14:33:54.338356Z, 3,769.140 seconds after due. The 5-minute deadline gate failed. Cadence finalized `late_unscored` once (`closing_observation_exceeds_five_minute_lateness_allowance`); the result is not a LOSS, even though its descriptive edge is positive.

All non-timing comparability gates passed: verified opening mirror, comparable performance, complete empty external-flow histories, and 28.845 seconds of observation skew against a 120-second limit. Common-mark Paper equity was $44,883.85 versus actual $44,680.11, an edge of +$203.74. After-cost returns were Paper -0.27935%, actual -0.73200%, excess +0.45265 percentage points. Raw Duckets display gap was +$196.59; common marks are the comparison basis. The positive result is descriptive only because the endpoint was late.

| Account | Opening | Paper common mark | Actual common mark | Edge | Actual display |
| --- | ---: | ---: | ---: | ---: | ---: |
| Alex | $5,876.75 | $5,864.76 | $6,051.83 | -$187.07 | $6,052.08 |
| Clear Pond | $33,742.88 | $33,637.00 | $33,373.05 | +$263.95 | $33,373.05 |
| Jeremy | $5,389.95 | $5,382.09 | $5,255.23 | +$126.86 | $5,262.73 |
| Pooled | $45,009.58 | $44,883.85 | $44,680.11 | +$203.74 | $44,687.86 |

The later stopped Paper snapshot is 14:39:48.545471Z and is not mixed into the comparison above. It ended at pooled equity $44,884.03, total P&L -$125.55, realized P&L -$530.88, fees $37.336680, funding -$0.011945, zero transfers, and $65,965.18 turnover. Maximum pooled drawdown was $126.58 (0.2812%).

| Account | Stopped equity | P&L | Fees | Funding | Turnover | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Alex | $5,864.76 | -$11.99 | $8.582670 | +$0.001858 | $19,072.60 | $16.41 (0.2792%) |
| Clear Pond | $33,637.01 | -$105.87 | $21.426575 | $0.000000 | $30,609.39 | $105.96 (0.3140%) |
| Jeremy | $5,382.26 | -$7.69 | $7.327436 | -$0.013804 | $16,283.19 | $20.84 (0.3858%) |
| Pooled | $44,884.03 | -$125.55 | $37.336680 | -$0.011945 | $65,965.18 | $126.58 (0.2812%) |

The ledger recorded 98 forecast-linked fills: 55 Qualified, 43 Research, 0 unavailable; 97 were signal rebalance and 1 risk cap. It recorded 402 decisions: holds were below rebalance threshold 101, forecast unavailable 90, opposite account direction 85, gross capacity 4; skips were size precision 18 and below $10 minimum 6. Every fill retained a forecast ID. The native v2 evidence also retained a fresh in-round witness at 12:35:37Z with model provenance.

The first 8 post-opening adjustment fills are reported separately and remain included in totals: $61,968.70 turnover, $35.499713 fees, and -$534.40 realized P&L. Coin fill attribution:

| Coin | Fills | Turnover | Fees | Realized P&L |
| --- | ---: | ---: | ---: | ---: |
| BTC | 23 | $22,841.14 | $15.726444 | -$61.64 |
| ETH | 31 | $4,250.45 | $2.613052 | -$6.57 |
| HYPE | 12 | $19,306.20 | $9.822034 | -$211.48 |
| ZEC | 32 | $19,567.39 | $9.175151 | -$251.19 |

## Decision and retained policy

The late unscored result retains the one-hour duration and does not trigger a LOSS/TIE retune. No model, source, or configuration change was made. The loose exploratory Paper policy remains `88cd1a6a008df9b9` with Research admission, zero entry/exit/rebalance bands, executable $10 minimum, and existing books, precision, fees, stops, cooldowns, cash/collateral and exposure protections. The five positive model families, 70/15/15 recipe, 5m candles, one-bar forecasts, refits per 300 seconds of source progress, 30-second Paper polling and 900-second exports remain. Research labels were not changed. Powder remains inactive on its separate strict policy.

## Archive and checks

The graceful stop completed before archive. Native archive verification passed at 14:42:46.857326Z: 481 files / 529,164,553 bytes, manifest SHA-256 `27e35d7ff0916a4e67d3ddd1471d8ceebac70f3efe719a48b66bb540202b123b`. Independent read-only SQLite integrity check returned `ok`. The post-comparison advancing health passed at 14:38:02Z; after graceful stops, 14:42:24Z health confirmed Paper/models stopped, coordinator alive, Powder off and no warnings. The coordinator remained running.

## Round 47 accepted handoff

Round 47 is accepted and running. Seed: 2026-10-02T14:49:38.168449163Z. Requested duration: 1 hour. Score deadline: 2026-10-02T15:49:38.168449Z. Opening equity: $44,693.74 across Alex $6,006.04, Clear Pond $33,396.76, and Jeremy $5,290.94. Public opening reads supported 10 inherited positions and zero open orders. Independent opening verification passed at 14:49:38.261838Z with zero opening decisions, fills, funding and transfers; signed quantities and cash/collateral/basis were reconciled.

A fresh models snapshot at 14:48Z verified all four BTC/ETH/HYPE/ZEC 5m/h1 forecasts. Labels then were BTC/ZEC Qualified and ETH/HYPE Research. The hidden model launch first missed because archiving had removed `_models/_runtime`; the absent control directory was created and a second hidden launch produced fresh forecasts. No application source or config changed.

First-cycle verification passed at 14:53:15Z: 7 cycles, 8 fills, 12 decisions, zero transfers/funding, and unchanged immutable opening. Those 8 initial adjustments cost $35.080274 in fees on $61,233.85 turnover and realized -$486.41; these costs remain in the active ledger. Advancing health passed at 14:55:44Z (portfolio observation 14:55:35Z), with Paper, models and coordinator identities verified, Powder inactive, and no warnings. Native maintenance completed at 14:56:01Z.

Cadence `advance` consumed the ending assessment once and retained one hour. Current score deadline is 15:49:38.168449Z. The automation was paused at invocation start and remains paused pending final closeout; the final schedule operation will reactivate it at `FREQ=HOURLY;INTERVAL=1`, anchored after closeout. A wake after the five-minute allowance will be recorded unscored.

## Evidence

- Native comparison: `round46-comparison-20261002T143344Z.json`, SHA-256 `82e3399ae247ea0edcea971256af10ee07c3d481ab088ec5469935e9e7ffe1d7`.
- Native immutable assessment: `_operations/paper-improvement-cadence/assessments/20261002-paper-round-46.json`, SHA-256 `7ebd2f87b44792bacf8802784fdfc733b7b71866724a4bbebc18ab8c5e09b0df`.
- Full account/cost/activity ledger summary: `round46-ledger-summary.json`.
- Accepted Round 47 machine handoff: `round47-handoff.json`.
- No application source or configuration changed, so no shared-source queue item was created. No software tests ran; the checks were native comparison/cadence, archive and SQLite integrity, independent opening and first-cycle verification, and advancing health.
