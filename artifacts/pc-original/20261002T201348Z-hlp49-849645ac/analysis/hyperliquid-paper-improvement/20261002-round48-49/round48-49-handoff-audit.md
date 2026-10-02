# Round 48 to Round 49 Paper handoff audit

Generated 2026-10-02T20:01:44.603915+00:00 UTC. Producer: hyperliquid-paper-improvement; Atlas / pc-original; cross-pc-v2 release 8516a9b40c300c02a28278d3e0016aac852cdb3e.

## Round 48 close comparison

- Seed 2026-10-02T17:31:28.238959074+00:00; one-hour due 2026-10-02T18:31:28.238959+00:00.
- Paper endpoint 2026-10-02T19:23:09.070678+00:00 (3,100.832 seconds late); account-read collection 2026-10-02T19:23:43.010354+00:00 (3,134.771 seconds late). Native assessment: late_unscored because closing_observation_exceeds_five_minute_lateness_allowance; the ladder retained one hour.
- Common-mark, baseline, zero-flow and observation-skew gates passed (skew 33.939/120 seconds). Timing failed.
- Common-mark equity: Paper USD 44,233.72, actual USD 44,089.24, edge USD 144.48. After-cost returns: Paper -0.1694%, actual -0.4955%, excess 0.3261 percentage points. This is descriptive, not a scored win.
- Endpoint Paper: 79 fills; USD 63,451.01 turnover; USD 36.01 fees; USD -0.0115 funding. Actual fees, funding and turnover were not present in native response.

### Per-account common marks

| Account | Paper | Actual | Edge | Paper / actual return | Paper fees / funding |
|---|---:|---:|---:|---:|---:|
| alex | USD 6,262.63 | USD 6,977.83 | USD -715.20 | 0.268% / 11.719% | USD 7.91 / USD 0.0000 |
| clearpond | USD 32,903.01 | USD 32,556.86 | USD 346.15 | -0.158% / -1.209% | USD 20.89 / USD 0.0000 |
| jeremy | USD 5,068.08 | USD 4,554.55 | USD 513.54 | -0.777% / -10.831% | USD 7.22 / USD -0.0115 |

### Archived Round 48 activity

- Stopped ledger: 435 cycles, 633 decisions, 85 fills and 8 funding entries; transfers 0. Native archive and SQLite checks passed.
- At stop: opening USD 44,308.78, ending USD 44,234.04, total P&L USD -74.74; turnover USD 63,688.72; fees USD 36.13; funding USD -0.0115; max drawdown 0.1700%.
- Forecast-linked fills: 50 Qualified / 35 Research. Decision reasons: skip below_size_precision 189; hold opposite_account_direction 174; hold below_rebalance_threshold 89; fill signal_rebalance 81; hold forecast_unavailable 81; hold target_unchanged 10; hold gross_capacity_exhausted 4; fill stop_loss 3; fill risk_cap 1; skip below_min_notional 1.
- Fill reasons: risk_cap 1; signal_rebalance 81; stop_loss 3.
- By-account and by-coin costs, balances and drawdowns are in the machine-readable audit.
- First eight inventory-adjustment fills: USD 60,487.34 turnover, USD 34.65 fees, realized P&L USD -383.43; included in total.
- Archive: 445 files / 486,089,233 bytes; manifest SHA-256 0468dfbf1566a93e73a8292566d5ec2714acfd00682cb23cb4167ef4fbf461f0.

## Round 49 accepted handoff

- Fresh 1:1 opening at 2026-10-02T19:36:12.740584135+00:00: USD 44,138.09, 10 inherited positions. Independent verification passed with zero opening decisions, fills, funding, transfers or P&L; three public open-order reads were empty.
- Opening account balances: Alex USD 6,906.45; Clear Pond USD 32,624.67; Jeremy USD 4,606.96.
- First committed cycle passed: 8 fills and 12 decisions. Startup adjustment costs: USD 33.97 fees on USD 59,142.05 turnover; realized P&L USD -186.98. These remain in running totals.
- Final health at 2026-10-02T20:08:33.914762+00:00 passed: Paper/models/coordinator identities verified; Powder inactive; no warnings. Paper equity USD 44,110.56, fees USD 34.16, funding USD -0.0022.
- Follow-on ledger snapshot at 2026-10-02T20:10:21.516130+00:00: 108 cycles, 120 decisions, 25 fills; 7 Qualified and 18 Research/unqualified fills; turnover USD 59,589.26, fees USD 34.17, funding USD -0.0022, realized P&L USD -190.21.
- Current holds/skips: hold below_rebalance_threshold 27; hold forecast_unavailable 24; fill signal_rebalance 22; hold opposite_account_direction 19; skip below_size_precision 6; skip below_min_notional 5; hold gross_capacity_exhausted 4; fill risk_cap 1.
- Latest forecast slots passed; Qualified: BTC; Research: ETH, HYPE, ZEC.
- Cadence remains one hour; next comparison due 2026-10-02T20:36:12.740584+00:00. Loose Paper policy 88cd1a6a008df9b9, 5m/one-bar forecasts, 70/15/15 recipe and five model families retained.
- Powder stays inactive under its strict separate policy. No real order or account change.

## Changes, checks and limits

- No application source, tests, shared configuration, symbol fields or runtime policy changed. No source queue item; no software tests were run. No retune followed the unscored result.
- Pinned installation, native archive and SQLite check, native prepare, independent opening, first cycle, advancing health, review completion and final health passed.
- Public account reads were sequential, not atomic. Actual trading costs were unavailable. Public artifact publication does not imply peer adoption or runtime deployment.

## Evidence identifiers

- Round 48 comparison SHA-256 66e7655094933d4290c7e8c7daad3c3d3a4be584b7131d34dcfb4643f034e087.
- Round 48 assessment SHA-256 7308d5be3f30738dcecda973649409bb9bc8bdc41dbdd18f7b3748124ebd60e8.
- Archived ledger SHA-256 623fb909c87263c48cc55563635ba63de370d0c8a509b8fd40523c937aa186c2.
- Round 49 archive manifest SHA-256 0468dfbf1566a93e73a8292566d5ec2714acfd00682cb23cb4167ef4fbf461f0.
- Round 49 final-health receipt: round49-final-health-20261002T2008Z.json (SHA-256 b8a9a32896e6567b77e5136006d4d8bb0488030ecbc5e1786b738aaaf8597afb); opening, first-cycle, advancing-health and deployment receipts are included in the evidence bundle.
