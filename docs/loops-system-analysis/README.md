# Ducketz Loops documentation

Start with [the current system and evidence boundaries](CURRENT_SYSTEM.md), then
use the documents below for their specific contracts. This directory contains
implementation contracts, dated investigations and historical operating instructions;
their presence does not authorize a worker, provider call or trader start.

The [October 11 relevance review](RELEVANCE_REVIEW.md) accounts for all 40 Markdown
files present in Atlas's application checkout before this cleanup, including
local work absent from the publication base. It records section dispositions,
Scout/Atlas differences, immutable recovery references and unresolved evidence.

| Need | Read |
| --- | --- |
| Preparation, Stats first, combined planning and current Atlas task observations | [Current system](CURRENT_SYSTEM.md), [nightly contract](NIGHTLY_GAMEPLAN.md) |
| Responsibilities and artifact handoffs | [Inventory](LOOP_INVENTORY.md), [relationships](LOOP_RELATIONSHIPS.md), [map](LOOP_MAP.md) |
| Gameplan, Stats and stock execution boundaries | [System functionality](SYSTEM_FUNCTIONALITY.md), [independent horizons](INDEPENDENT_STOCK_HORIZONS.md), [trader runtime](STOCK_TRADER_RUNTIME.md) |
| Manual trader control and recovery | [Trader operation](STOCK_TRADER_AUTOMATION.md), [monitoring](MONITORING.md) |
| Combined capital, ownership and cross-horizon policy | [Joint capital](JOINT_ACCOUNT_CAPITAL.md), [bearish fallback](CROSS_HORIZON_BEARISH_FALLBACK.md) |
| Loop C observer, risk and gated rollout | [Pooled sequence / Loop C](POOLED_SEQUENCE_LOOP_C.md), [risk calculus](LOOP_C_RISK_CALCULUS.md), [rollout evidence](LOOP_C_ROLLOUT_PLAN.md) |
| Symbol admission and data lineage | [Onboarding](SYMBOL_ONBOARDING.md), [research-selected batches](RESEARCH_SYMBOL_ONBOARDING.md), [datastore hygiene](DATASTORE_HYGIENE.md) |
| Reused numerical components | [Loop A](loops/loop-a.md), [Loop B](loops/directional-loop-b.md), [Strategy](loops/strategy-runtime.md), [prediction contributions](PREDICTION_CONTRIBUTION_MATRIX.md) |
| Options, CME and ALFRED implementation and history | [Options ML](OPTIONS_STRATEGY_ML_UPGRADE.md), [OPRA](OPRA_HISTORY_MAINTENANCE_AUTOMATION.md), [pricing](loops/active-pricing-loop-3.md), [capture](loops/options-capture-loop-4.md), [CME](loops/cme-l2-runtime.md), [inline CME](loops/cme-inline-history.md), [ALFRED](loops/daily-alfred-runtime.md) |
| September defects and verification | [September 4 audit](audits/2026-09-04/AUDIT.md), [supervision](audits/2026-09-04/SUPERVISION_UPDATE.md), [four-hour calibration](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md) |

The old filename [HOURLY_AUTOMATION.md](HOURLY_AUTOMATION.md) is a stable navigation
entry for the overnight history. [Weekly review](WEEKLY_REVIEW_AUTOMATION.md) and
[paper tracking](OPTIONS_STRATEGY_PAPER_AUTOMATION.md) retain useful procedures;
their schedules require machine-specific verification.

The pre-existing local intraday design remains available in the application
checkout. Its [published design revision](https://github.com/jeremysecondstate/ducketz/blob/9d4ca66858d148cdbeaeee46e4031428ca6d15d1/docs/loops-system-analysis/INTRADAY_15M_SYSTEM_DESIGN.md)
and [prototype PR](https://github.com/jeremysecondstate/ducketz/pull/29) distinguish
design, offline implementation and unverified operational adoption. This preserves
the earlier writer's index entry without publishing that writer's untracked file.
The separate Hyperliquid stack is indexed in
[Hyperliquid system analysis](../hyperliquid-system-analysis/README.md).
