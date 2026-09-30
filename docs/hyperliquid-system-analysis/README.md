# Hyperliquid portfolio system analysis

Last updated: **2026-09-28** for native five-minute candles, next-five-minute forecasts and adaptive Paper improvement rounds.

This is the maintained system-level reference for the Hyperliquid data,
forecasting, paper portfolio and H.Y.P.E.R. operations workspace. It follows the
inventory, dependency-map and operating-guide approach of
[Loops system analysis](../loops-system-analysis/README.md), while documenting
this system's own processes, accounting and evidence.

## Current operating model

Three analysis/simulation runtimes and an optional user-activated Powder runner share local artifacts under
`C:/DATASTORE/hyperliquid`:

1. **Market data:** one coordinator maintains separate BTC, ETH, HYPE and ZEC
   five-minute candle, feature and label publications using shared code. It
   retains the growing five-minute history and reconciles every 288 cycles.
2. **Models:** one runtime consumes completed publications, fits per-market
   models on a five-minute source-candle schedule, and records a next-five-minute
   forecast for each completed five-minute candle (`5m/h1`). The live configuration uses
   chronological 70/15/15 fitting/calibration/assessment with strict horizon
   purges and retains logistic/ExtraTrees/HistGradientBoosting/MLP at weights
   40/20/20/20 with calibration C=0.1. Model polling is five seconds; polling is
   not fitting or a new forecast. Reserved evaluation blocks still create a
   fitting cutoff lag, about 5.2 days in the initial five-minute measurement.
3. **Paper portfolio:** one runtime allocates only from fresh, validated Qualified
   forecasts under the current configuration. Without one, it holds current
   exposure subject to stop/cooldown and exposure-cap reductions; rejected
   signals cannot cause new exposure or virtual transfers.
   It performs account-local risk checks, observes public books, and commits simulated fills,
   virtual transfers, funding estimates and valuations to a SQLite ledger.
   Its configured quote/risk polling interval is 30 seconds.
4. **Powder execution:** an explicitly launched real-account runner reuses the
   validated strategy, adopts matching actual positions, sizes to local collateral
   and records durable order intents/confirmed fills. It remains disabled until
   the user runs the [activation command](POWDER_ACTIVATION.md).

The **H.Y.P.E.R. → Paper** page observes that system through bounded local reads
about every five seconds. **Powder** reads its separate execution ledger and
displays **Not connected** until actual observations exist. It never substitutes
Paper balances. Actual P/L/funding accounting and automatic transfers remain
planned. The existing **Hyperliquid Duckets** manual workspace shares account
ownership locks with Powder so local manual mutations cannot race the runner.
Paper's orange comparison curve observes the real account totals through a
separate public balance reader once per minute while the UI is open. Its saved
history is separate from both ledgers. The local projection used by Operations
Watch remains read-only and makes no provider requests. The orange P/L curve is
real equity change from the mirror opening, including deposits and withdrawals;
see the [chart implementation](../hyperliquid-duckets-design-ui/hyper-2026-09-25/IMPLEMENTATION.md).

The Paper view derives its market list, candle interval and first model horizon
from the configured recipe. Ten-minute aggregation is not implemented; retained
five-minute candles provide a base for future frequency experiments.

The entry points do not install an operating-system startup task. The separately
configured [Paper Improvement](PAPER_IMPROVEMENT.md) task reviews results against
the actual accounts in adaptive rounds using GPT-6 Astra / Ultra, starting at
two hours. Each verified win adds one hour to the next round; each verified loss
subtracts two hours with a one-hour minimum. Ties and unscored outcomes keep the
duration. It runs justified
model experiments, archives the completed run, and verifies a fresh one-for-one
Paper opening. Its accepted-baseline receipt coordinates with the separately
configured [Operations Watch](OPERATIONS_WATCH.md) checks local health every
30 minutes and can recover interrupted Paper operations. Process liveness,
external scheduler configuration, qualification,
balances and performance must be checked at the time of operation; this index
does not declare them permanently current.

The separate [expanded-model research command](../hyperliquid-models.md#expanded-offline-model-research)
evaluates additional classical models, compact CNN/GRU/CNN+GRU models and fixed
blends on pinned historical data. It writes only `_model_research` artifacts;
the twelve available candidates in the measured environment are not twelve
active production members. See the
[70/15/15 comparison audit](audits/2026-09-26-expanded-models-70-15-15.md).

## Read by question

| Question | Reference |
| --- | --- |
| What is implemented, and what is still unavailable? | [System functionality](SYSTEM_FUNCTIONALITY.md) |
| How do the pieces connect? | [Loop map and relationships](LOOP_MAP.md) |
| Which process owns each job, clock and artifact? | [Loop inventory](LOOP_INVENTORY.md) |
| How do candles and features become a published input? | [Market-data loop](loops/market-data.md) |
| What do the models fit, qualify and predict? | [Model loop](loops/models.md) |
| What did the expanded models and 70/15/15 comparison show? | [Expanded-model audit](audits/2026-09-26-expanded-models-70-15-15.md) |
| What happens during a Paper cycle or restart? | [Paper loop](loops/paper.md) |
| How do I check, activate, stop or recover real trading? | [Powder activation](POWDER_ACTIVATION.md) |
| How are P/L, transfers, sizing and risk interpreted? | [Paper accounting and risk](PAPER_ACCOUNTING_AND_RISK.md) |
| Where does each UI value come from? | [UI and data contracts](UI_AND_DATA_CONTRACTS.md) |
| How do I inspect health, diagnose a failure or operate a runtime? | [Monitoring and recovery](MONITORING.md) |
| What does the 30-minute scheduled watch check and recover? | [Operations Watch](OPERATIONS_WATCH.md) |
| What must be updated when the implementation changes? | [Maintenance checklist and change-impact matrix](MAINTENANCE.md) |
| What changed in this reference set? | [Changelog](CHANGELOG.md) |

## Account and execution boundaries

| Account | Managed Paper role |
| --- | --- |
| Alex | Short perpetuals; buys reduce existing shorts |
| Jeremy | Long perpetuals; sells reduce existing longs |
| Clear Pond | Owned spot inventory and cash; no shorting or borrowing |

Paper was configured to start from a read-only mirror of real account balances
and positions, then evolve independently. It resumes its existing ledger on
restart. Small inherited spot balances in Alex and Jeremy remain unmanaged.
The opening marked equity, rather than an inherited position's historical cost
basis, defines strategy performance since Paper began.

The September 26 experiment sets `require_qualified_forecasts=true` and records
recipe `direction-volatility-v2-qualified-hold`. Its fresh opening mirror retains
the observed inventory/cash one-for-one before the first allocation/risk cycle.
The prior mixed Research/Qualified ledger is preserved in
`C:/DATASTORE/hyperliquid/_paper_archives/20260926T073643Z-research-and-qualified`;
its P/L and decisions are not the new experiment's opening history. Verify the
new opening snapshot and heartbeat before treating the experiment as running.

The Paper runtime accepts Paper mode only. The separate Powder runner implements
real-money execution and reconciliation after explicit user activation; the
Operations Watch does not activate or restart it. A manual real-account action
does not automatically change the independently evolving Paper ledger.
Powder reads the same qualification setting on a future activation, but its
missing/rejected-signal policy still uses zero targets; it does not inherit
Paper's no-signal hold behavior.

## Evidence and documentation authority

| Label | Meaning |
| --- | --- |
| **Implemented** | Supported by the linked executable code and tests |
| **Configured** | A setting in the linked configuration, as of the verification date; it can change |
| **Observed** | A timestamped artifact or inspection; not a permanent operating guarantee |
| **Planned** | A future capability with no current execution authority |
| **Historical** | An earlier design, measurement or deployment retained as a record |
| **Unknown** | Not established by inspected source or evidence |

Code/configuration define behavior. Completed data/model publications and the
Paper ledger establish their respective records. Runtime status explains
process observations; it does not override the ledger. Exported Parquets and
performance summaries retain their own timestamps. Concept image values are
illustrative, and model qualification is not a profitability claim.

These pages organize the cross-system contract. The existing component guides
remain detailed companions:

- [Market-data and feature guide](../hyperliquid-data-pipeline.md)
- [Models and continuous predictions](../hyperliquid-models.md)
- [Paper accounts and execution](../hyperliquid-paper.md)
- [Operation timing definitions](../hyperliquid-timings.md)
- [Implemented H.Y.P.E.R. UI and screenshots](../hyperliquid-duckets-design-ui/hyper-2026-09-25/IMPLEMENTATION.md)

The dated design and handoff files preserve planning history. Their illustrative
buttons, numbers and pre-implementation statements do not supersede current
code. Update this reference set and any affected companion guide with the same
behavioral change; see [Maintenance](MAINTENANCE.md).

## Source entry points

The current accepted opening and worker handoff are recorded in
`C:/DATASTORE/hyperliquid/_operations/paper-current-accepted.json`; inspect its
experiment identity and timestamps before interpreting current performance.
The [Paper improvement workflow](PAPER_IMPROVEMENT.md) defines archive/reset
verification. Dated openings below are historical, not current balance claims.

Historical September 26 opening: [70/15/15 training and BTC-inclusive fresh mirror](audits/2026-09-26-701515-btc-fresh-mirror.md),
experiment `20260926T180818Z-701515-btc-mirror`, at **18:08:18 UTC on September 26**.
The nine-position opening was **$44,083.99583107362**, with zero opening P/L,
fills and fees; Clear Pond held **0.2239149983 BTC**. Retained raw public-account
responses independently matched opening inventory, cash/collateral and perpetual
entries. The current four estimators now use the approved chronological
70/15/15 split; added classical and sequence families remain research-only.
Shared 49%/51% thresholds, the 20% ordinary-rebalance threshold (at least $25),
fees and risk limits are unchanged. First-trading and advancing accounting/health
checks passed; maintenance completed at **18:16:19 UTC** and Operations Watch v11
accepted the baseline. The receipt distinguishes later startup fills from the
unchanged opening and records each health observation's timestamp. Its
[prior 15:49 experiment](audits/2026-09-26-paper-retune-fresh-mirror.md) was preserved
intact, and the [research comparison](audits/2026-09-26-expanded-models-70-15-15.md)
states the mixed per-market results and profitability limits.
A separate predecessor seeded at 10:35:59 UTC is permanently excluded from analysis and recovery.
The user completed deletion; absence of its cleanup directory was verified at
**2026-09-26 11:36:32 UTC**. Independent model research and still-earlier archives
remain separate and preserved.

- [Market configuration](../../configs/hyperliquid-markets.json),
  [model configuration](../../configs/hyperliquid-models.json),
  [Paper configuration](../../configs/hyperliquid-paper.json)
- [Data coordinator](../../ml/hyperliquid_coordinator.py),
  [model runtime](../../ml/hyperliquid_model_runtime.py),
  [Paper runtime](../../ml/hyperliquid_paper_runtime.py)
- [Paper ledger](../../ml/hyperliquid_paper_ledger.py),
  [read-only view adapter](../../app/services/hyperliquid_paper_view.py),
  [H.Y.P.E.R. workspace](../../app/ui/hyper_workspace.py)
