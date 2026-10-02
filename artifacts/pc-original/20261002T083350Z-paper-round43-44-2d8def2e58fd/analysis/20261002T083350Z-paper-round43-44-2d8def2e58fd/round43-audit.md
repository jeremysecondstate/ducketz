# Hyperliquid Paper improvement — Round 43 closeout and Round 44 handoff

## Outcome

- Round 43 duration: 1 hour; seed 2026-10-02T05:45:02.728868723+00:00; due 2026-10-02T06:45:02.728868+00:00; Paper endpoint 2026-10-02T07:47:53.367192+00:00.
- Endpoint was 3,770.638 seconds late, beyond the five-minute gate. Native assessment: **UNSCORED (`late_unscored`)**; consumed once. Next duration stays 1 hour.
- No model, source or parameter retune was made. With an unscored comparison, no loss/tie retune was justified. The exploratory Paper policy and truthful model labels remain.

## Comparative closeout

- Native comparability: `performance_comparable=false`; `zero_external_flows_verified=false`; maximum account observation skew 26.700s (native limit 120s).
- Pooled Paper ledger equity $44,646.4206; Paper common-mark equity $44,646.4860; Duckets display equity $44,638.3200; Duckets common-mark equity $44,638.3474.
- Point-in-time common-mark balance edge: **+8.1387**; raw display edge +8.1006. Paper return, actual return and excess return are null because native gates failed. This balance edge is descriptive, not a WIN.
- Pooled: 362 cycles, 89 fills, 9 funding rows, 0 transfers; turnover $64,771.4425, fees $36.7298, funding -0.0014, max drawdown $36.6032 at 2026-10-02T07:09:27.431062+00:00.

### Per-account ledger and balance evidence

| Account | Paper opening | Paper ending common mark | Duckets ending common mark | Paper P&L since opening | Fills | Turnover | Fees | Funding | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Alex | $7,114.9242 | $7,076.4217 | $6,062.7976 | -38.5024 | 26 | $18,674.9093 | $8.4037 | +0.0021 | $38.6914 |
| Clear Pond | $32,322.5588 | $32,308.8926 | $33,331.1577 | -13.6687 | 13 | $30,330.5175 | $21.2314 | +0.0000 | $13.7410 |
| Jeremy | $5,244.6741 | $5,261.1718 | $5,244.3921 | +16.4345 | 50 | $15,766.0157 | $7.0947 | -0.0034 | $5.5278 |

### Forecast attribution and execution holds

- Forecast-driven fills: 65 Qualified and 24 Research (89 total); qualification labels remain intact.
- Holds: 87 below rebalance threshold; 84 forecast unavailable; 74 opposite-account direction; 4 gross capacity exhausted.
- Skips: 45 below size/precision; 1 below the executable $10 minimum. No unchanged or zero target was fabricated into a fill.
- Coin attribution (fills / turnover / fees / realized P&L):
  - BTC: 24 / $22,662.5143 / $15.6078 / -14.6742.
  - ETH: 24 / $3,527.8592 / $2.2748 / +0.1542.
  - HYPE: 12 / $19,051.5164 / $9.6945 / -150.3060.
  - ZEC: 29 / $19,529.5526 / $9.1527 / -237.8362.

- Round 43 opening inventory adjustment, separately identified and retained in totals: 8 fills, turnover $61,443.0455, fees $35.1966, realized P&L -403.8673.

### External-flow evidence

- Public histories covered the window. Jeremy had a complete empty nonfunding history. Alex had two USDC `send` events totaling $1,051.00; Clear Pond had two totaling $1,050.20. Native evidence does not classify whether these were external or paired internal flows; zero-flow and excess-return gates remain unavailable.
- Follow-up hypothesis: verify receiver-side histories and fee-adjusted amounts for the time-paired sends against retained public evidence; capture the next comparison at its committed deadline. Do not infer a win from endpoint balances.

## Round 44 accepted opening and runtime

- Fresh seed 2026-10-02T07:59:32.930325747+00:00; one-hour due 2026-10-02T08:59:32.930325+00:00; opening pooled equity $44,702.2735. Independent opening verifier passed 2026-10-02T08:00:10.630280+00:00.
- Ten inherited position rows reconciled across all three accounts, including signed quantities, collateral/cash and basis; source open orders were zero. Opening fills, fees, funding, transfers and P&L were zero.
- Fresh 5m/one-bar forecasts were available at preparation: BTC and ZEC Qualified; ETH and HYPE Research. The five positively weighted families and 70/15/15 recipe were retained.
- First trading interval passed 2026-10-02T08:03:09.533640+00:00: 8 fills, 12 decisions, 4 funding rows, no transfers. Startup adjustment: 8 fills, turnover $61,511.4975, fees $35.2417, realized P&L -373.1334, funding +0.0528; costs remain in totals.
- Native maintenance completed 2026-10-02T08:04:16.267232+00:00. Latest health passed 2026-10-02T08:35:43.260309+00:00: Paper PID 65216, models PID 48064, coordinator PID 56964 alive; zero warnings; Powder disconnected/off.
- Existing automation re-anchored PAUSED→ACTIVE at 2026-10-02T08:00:42.391+00:00 (69.461s after seed); ACTIVE hourly, `gpt-6-luna`/max, configured project and notification setting. The prompt was preserved at activation; a later stored prompt update is recorded in the schedule receipt. Schedule and cadence agree on 2026-10-02T08:59:32.930325+00:00.
- Five-minute candle collection continues; Paper polls every 30 seconds and exports every 900 seconds. Powder remains inactive under its strict policy.

## Evidence and limitations

- Round43 archive: 474 files / 512,575,685 bytes; manifest SHA-256 `9455c0ed7949affa5dc9344feea8fd8f099b3b971e682b58823c785bc3ae7fdb`. Archived holdings ledger passed SQLite `integrity_check` (`ok`); all 474 archive file hashes reverified at 2026-10-02T08:43:59.699120Z with no mismatches.
- Passed checks: Round43 late-close health (07:51:41.370066Z) and trading verifier (07:52:05.492626Z); Round44 opening verifier (08:00:10.630280Z); first-cycle verifier (08:03:09.533640Z); advancing health (08:03:59.410625Z); post-completion health (08:09:08.289165Z); final handoff health with a newer committed observation (08:35:43.260309Z). No software tests ran because no source/config/model change was made.
- Round44 stdout/stderr remain live and were not snapshotted; Round43 archive had no runtime log files. Shared source, tests, documentation and common configuration were unchanged, so no cross-PC source queue item applies.

## Durable records

- Native Round43 comparison and cadence assessment remain immutable beside this audit; the archived ledger and successor handoff accompany the published audit.
- Round44 opening, first-cycle, schedule and post-completion health receipts remain beside the handoff.
