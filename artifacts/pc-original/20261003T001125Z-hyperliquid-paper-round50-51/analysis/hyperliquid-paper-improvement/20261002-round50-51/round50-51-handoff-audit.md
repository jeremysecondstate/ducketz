# HYPER Paper round 50–51 handoff audit

Run: 2026-10-02, America/Los_Angeles. Producer: `hyperliquid-paper-improvement`; actor/machine: Atlas / pc-original. Runbooks: Paper Improvement 2026-09-30/v5; Operations Watch 2026-09-30/v16.

## Round 50 endpoint

- Committed one-hour observation was due **2026-10-02T22:37:46.860747+00:00**; native closing observation arrived **2026-10-02T23:33:59.373429+00:00**, 3372.5 seconds late. The five-minute timing allowance failed, so the native assessment is **LATE_UNSCORED** (`closing_observation_exceeds_five_minute_lateness_allowance`). Do not treat it as LOSS despite negative comparative returns.
- At the endpoint, pooled Paper common-mark equity was **$44,171.51** from $44,205.68; public-account common-mark equity was **$44,268.48**. Common-mark Paper edge: **$-96.98**; after-cost Paper return **-0.0773%**, actual **0.1421%**, excess **-0.2194 percentage points**. No score is assigned because endpoint timing was late.
- Comparability otherwise passed: same Alex, Jeremy, Clear Pond accounts; mirror opening reconciled; all three non-funding flow histories complete and empty; endpoint skew 13.70s (120s maximum). Sequential public reads are not an atomic exchange snapshot.
- Endpoint Paper ledger: 67 fills, all 67 linked to forecasts (30 Qualified, 37 Research); $60,486.23 turnover, $34.5848 fees, funding estimate $-0.001394, net P&L $-34.1791. Fills were one risk-cap reduction and 66 signal rebalances.

### Per-account comparison at the endpoint

| Account | Opening | Paper common mark | Public common mark | Paper return | Public return | Paper excess | Paper fees | Funding est. | Turnover | Max drawdown through endpoint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Alex | $7,040.24 | $7,030.29 | $6,777.05 | -0.1414% | -3.7384% | 3.5970 pp | $7.8285 | $0.001097 | $17,396.57 | $9.9839 (0.1418%) |
| Jeremy | $4,505.39 | $4,501.41 | $4,708.19 | -0.0882% | 4.5014% | -4.5895 pp | $6.1315 | $-0.002491 | $13,625.54 | $7.6172 (0.1690%) |
| Clear Pond | $32,660.05 | $32,639.80 | $32,783.24 | -0.0620% | 0.3772% | -0.4392 pp | $20.6249 | $0.000000 | $29,464.12 | $20.4597 (0.0626%) |

### Holds, skips, and costs

At the comparison endpoint, holds were 108 `forecast_unavailable`, 97 `below_rebalance_threshold`, 87 `opposite_account_direction`, and 4 `gross_capacity_exhausted`; 33 skips were `below_size_precision`. No fill was manufactured for a zero/unchanged target. A missing forecast held its target while independent risk checks remained available.
The first eight startup/inventory-adjustment fills used $58,938.88 turnover, cost $33.8885 in fees, and realized $-162.2701; all are included in total round costs and results.
The later stop archive includes 70 fills, 376 cycles, $60,667.43 turnover, $34.6664 fees, funding estimate $-0.001394, and P&L $-34.1505. Its drawdown was $36.2224 (0.0819%). These are stop-time archive totals, after the 23:33:59 comparison endpoint; they are not substituted into the native endpoint result.
Per-account/coin stop-time fills, qualification counts, realized P&L, and decision causes are in the machine-readable audit JSON and archived ledger. Funding is estimated from the completed-perp-close proxy; visible-book fills omit queue/latency and liquidation-engine simulation.

## Change and Round 51 opening

- Round 50 was **late-unscored**, so there was no WIN/LOSS/TIE-triggered parameter improvement. Kept the user-directed exploratory Paper admission (`require_qualified_forecasts=false`, entry/exit bands 0, rebalance minimum 0, $10 floor), truthful Qualified/Research labels, stops, precision, cash/collateral, exposure caps, and strict inactive Powder policy. Five model families remain; 5m candles, one-bar forecasts, and 300s source-progress refits remain.
- Native staged lifecycle completed for Round 51. Fresh mirror opening **2026-10-02T23:44:46.087340593+00:00**, one-hour due **2026-10-03T00:44:46.087340+00:00**, pooled opening equity $44,291.30. Independent public reads verified 10 inherited positions, empty source open orders, and zero opening fills/fees/funding/transfers/P&L. Archive and checksums were preserved before the fresh seed.
- Final health at 2026-10-03T00:09:00.892793+00:00 passed with Paper PID 20884, models PID 28272, coordinator PID 56964; Powder was inactive, warnings 0. Paper equity $44,261.57, P&L since opening $-29.7298, fees $34.3932, funding $0.000000. Forecast slots passed for BTC (Qualified), ETH/HYPE/ZEC (Research).
- Round 51 initial eight simulated inventory/startup fills: $59,745.9724 turnover, $34.296192 fees, realized P&L -$238.55175; retained in current round totals.

## Checks, artifacts, and applicability

- Passed: native comparison and cadence assessment; staged archive verification (461 files, 501,674,244 bytes, manifest SHA-256 `3e0c2729…`); archived SQLite integrity `ok`; independent Round 51 opening, first-cycle verification, cadence status, and final live health. No software tests ran because no source changed.
- Shared source queue: not applicable, no common source/config/test/doc edits. Artifact publisher only publishes the explicitly reviewed Atlas machine-local operating records. Public `main` publication is record publication; it does not mean Scout adopted the artifacts or that runtime was deployed.
- Atlas has no tracked shared-source changes in this round. Scout applicability: none; common implementation and common strategy defaults were untouched. Exact local files, operations and SHA-256 are in `project-file-inventory.json` and `local-artifact-inventory.json`.
- Limitations: late endpoint cannot prove a win or loss; endpoint actual-account reads are sequential; one short horizon is not proof of future profitability; published operating artifacts do not authorize trading or Powder.
