# Hyperliquid Paper improvement audit — Round 47 to 48

**Run result:** Round 47 assessed `late_unscored`; its duration stays at one hour. Round 48 was accepted and verified advancing. The positive comparison is descriptive because the Paper endpoint missed the due-time gate. No strategy, model, source or configuration retune was made for an unscored result.

## Round 47 comparison

Round 47 seeded 2026-10-02T14:49:38.168449163+00:00 with one-hour deadline 2026-10-02T15:49:38.168449+00:00. Paper observation was 2026-10-02T17:19:53.418340+00:00 (90.25 minutes late); actual comparison completed 2026-10-02T17:20:21.920786+00:00. Observation skew was 28.502s. Baseline reconciliation and complete zero-external-flow checks passed; lateness made the result unscored.

| Measure | Paper | Actual | Relative result |
|---|---:|---:|---:|
| Common-mark equity | $44,583.82 | $44,366.84 | +$216.98 |
| Return on shared opening | -0.2459% | -0.7314% | +0.4855 percentage points |
| Paper fees / funding / turnover | $36.93 / -$0.0225 / $65,232.06 | Not itemized | — |
| Raw display-equity gap | — | — | +$211.30 (not scored) |

All accounts reconciled to the opening and had complete empty external-flow histories. Actual account equity includes observed balances and positions; explicit actual fees, funding and turnover were unavailable. The positive edge did not advance the ladder because timing failed.

### Account comparison

| Account | Paper common mark | Actual common mark | Edge | Paper fees | Paper funding | Paper turnover | Paper max drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|
| Alex | $6,059.10 | $6,249.99 | -$190.89 | $8.02 | $0.0008 | $17,817.62 | $0.17 (0.0028%) |
| Clearpond | $33,289.44 | $33,011.80 | $277.63 | $21.20 | $0.0000 | $30,289.49 | $107.55 (0.3220%) |
| Jeremy | $5,235.28 | $5,105.05 | $130.23 | $7.71 | -$0.0232 | $17,124.95 | $58.27 (1.1014%) |

### Forecasts, fills and remaining decisions
At comparison Paper had 451 cycles, 474 decisions, 93 fills, 14 funding entries and no transfers; fees $36.93, funding -0.0225, turnover $65,232.06. Rule `valid-in-round-forecast-v2` recorded 360 consumed forecasts, 120 valid (60 Qualified, 60 Research), and 0 invalid. At stop all 97 fills were forecast-linked (61 Qualified-labeled, 36 Research/unqualified), $65,607.48 turnover, $37.10 fees and -0.0225 funding. First eight post-opening adjustments cost $35.08 on $61,233.85 turnover with realized P&L -$486.41; included in all-in result.

| Graceful-stop ledger | Count / result |
|---|---:|
| Cycles / decisions / fills | 460 / 486 / 97 |
| Pooled equity / P&L | $44,582.77 / -$110.97 |
| Pooled max drawdown through comparison endpoint | $112.66 (0.2521%) |
| Fill actions | 96 signal rebalances; 1 risk-cap action |
| Holds | 149 below threshold; 114 forecast unavailable; 88 opposite account direction; 4 gross capacity exhausted |
| Skips | 34 below size/precision |
| Archive | 557 files; 617,076,231 bytes; manifest verified; SQLite integrity `ok` |

## Round 48 opening and live state
Maintenance `20261002-paper-round-48` completed 2026-10-02T17:34:16.062638+00:00; mirror accepted 2026-10-02T17:32:06.770685+00:00. Opening equity $44,308.78 across 10 positions; opening verification found zero fills, fees, funding, transfers and P&L. All three source open-order responses were empty. First-cycle trading recorded 8 inventory adjustments, $34.65 fees, $60,487.34 turnover, pooled equity $44,247.62 and P&L -$61.16. Costs occurred after the zero-cost opening and remain in Paper.
Retained policy `88cd1a6a008df9b9`: qualified-only admission off, entry/exit bands 0, rebalance minimum delta 0, executable floor $10. Recipe remains 5m candles / 1-bar forecast; five model families and weights unchanged. Slots: BTC=Qualified, ETH=Research, HYPE=Research, ZEC=Research.
Closeout health 2026-10-02T17:44:47.775723+00:00 passed: Paper, Models and coordinator identities verified; slots fresh; no warnings; Powder inactive. Round 48 seed 2026-10-02T17:31:28.238959074+00:00, one hour, due **2026-10-02T18:31:28.238959Z (11:31:28 PDT)**.

## Changes, checks and limitations
No source/shared configuration/model changes were made, so no source queue item or code tests apply. The separate artifact commit adds only reviewed operating evidence under `pc-original`; its immutable manifest and receipt record exact paths and hashes. Publication does not imply peer adoption or deployment. DataStore originals and archive remain intact.
Passed checks: native cadence assessment; archive manifest/hash match and SQLite integrity; opening verification; empty open-order reads; prepared and advancing health; first-cycle ledger verification; native lifecycle; closeout health (17:44:47Z). No broker/provider action, real order action, training, Powder start, account change or runtime deployment occurred.
Limits: Round 47 is unscored because endpoint was late; actual explicit costs are unavailable; account observations were sequential with 28.5-second skew. No tuning claim is attributed to this run.

### Published artifact source paths
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-48-audit.md`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-48-audit.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-closing-comparison.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-cadence-assessment.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-archive-verification.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round47-paper-ledger.sqlite3`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-operation.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-deployment.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-opening-public-account-reads.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-opening-verification.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-opening.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-first-cycle.json`
- `artifacts/analysis/hyperliquid-paper-improvement/20261002-round47-48/round48-closeout-health.json`

Artifact completion ID: `20261002T180809Z-hl19b1eb9c9d`. Its receipt records exact per-file hashes and remote commit SHA; no shared-source record was queued.
