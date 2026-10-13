# Loop and responsibility inventory

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

This table describes ownership boundaries, not a recurring process inventory.
For dated local schedule observations see [Current system](CURRENT_SYSTEM.md#atlas-task-observation-on-october-11).

| Component | Still useful responsibility | Evidence/limitation |
| --- | --- | --- |
| Loop A | Provider acquisition, inline data work, readiness and lineage | [Implementation report](loops/loop-a.md); a stopped recurring supervisor does not retire bounded stages |
| Loop B | Causal feature/sample and directional publication | [Implementation report](loops/directional-loop-b.md); distinct from final target-model fitting |
| Stock target history | Completed-session targets and archive provenance | Required scope and availability come from the saved run |
| Stats / actuals | Score matured original forecasts, retain pending/missing results | Runs before current model review; [current flow](CURRENT_SYSTEM.md#preparation-and-feedback) |
| Model review / training | Bounded proposals, chronological selection, calibration, assessment and accepted models | A review proposal or successful process launch is not promotion |
| Gameplan / enrichment / local planning | Frozen routes, separate sizing qualification and conditional projection | [Nightly contract](NIGHTLY_GAMEPLAN.md) |
| Private Gameplan exchange / single synthesis | Verify owner packages and allocate one combined cash/stock plan plus Stats | [Joint capital](JOINT_ACCOUNT_CAPITAL.md); peer arrival and joint completion differ |
| Atlas handoff / UI readers | Display the same completed 22-symbol Gameplan and Stats on both PCs | [Current consumers](CURRENT_SYSTEM.md#readers-capital-and-execution) |
| Stock execution | Atlas Gameplan adapter, independent of both local 15-minute traders | [Runtime](STOCK_TRADER_RUNTIME.md); manual start and own local records |
| Loop C observer / risk | Independent options proposal sizing, risk and evidence contracts | [Pooled/observer](POOLED_SEQUENCE_LOOP_C.md), [risk](LOOP_C_RISK_CALCULUS.md) |
| Options pricing/capture/Strategy | Retained optional numerical stages, historical training and exact-leg intents | [Pricing](loops/active-pricing-loop-3.md), [capture](loops/options-capture-loop-4.md), [Strategy](loops/strategy-runtime.md) |
| CME / ALFRED | Cross-asset and macro evidence with distinct clocks and provenance | [CME](loops/cme-l2-runtime.md), [inline history](loops/cme-inline-history.md), [ALFRED](loops/daily-alfred-runtime.md) |
| Onboarding | Explicit candidate validation and atomic membership admission | [Workflow](SYMBOL_ONBOARDING.md); admission does not start trading |

## Legacy recurring owners

The retained `--forever` implementations include `datafetching.cme_runtime`,
`datafetching.orchestrate`, `datafetching.fred_alfred_runtime`,
`ml.option_pricing_runtime`, `datafetching.options_runtime`,
`ml.prediction_runtime`, `ml.strategy_runtime` and
`ml.strategy_profit_training_runtime`. Their old recurrence is historical.
This inventory neither starts them nor asserts their process state on Scout.
Current bounded consumers can still use their modules and artifacts.

The former LOG, hourly guardian, standalone maintenance, paper tracking and
weekly-review descriptions remain distinguishable from current responsibility
scheduling. Match saved native task purpose and binding, not an old display name.
