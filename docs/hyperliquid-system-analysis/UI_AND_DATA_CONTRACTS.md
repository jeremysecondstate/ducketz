# H.Y.P.E.R. UI and data contracts

Last updated: **2026-09-26** for Paper decision checks and resize handling. [Index](README.md) ·
[Accounting](PAPER_ACCOUNTING_AND_RISK.md) · [Operations](MONITORING.md)

## Entry points and ownership

[DucketBucketApp](../../app/ui/ducket_bucket.py) mounts **H.Y.P.E.R.** beside
**Hyperliquid Duckets**. The new
[HyperWorkspace](../../app/ui/hyper_workspace.py) presents Paper and Powder.
[HyperliquidPaperViewService](../../app/services/hyperliquid_paper_view.py)
provides its read-only projection. The manual account workspace retains its
existing services and controls; its real balances are not substituted for
Paper balances or disconnected Powder results.

The view adapter defaults to `C:/DATASTORE/hyperliquid`. Its forecast/data
discovery currently uses the fixed four-symbol tuple BTC/ETH/HYPE/ZEC and
`15m/h4`; the asset picker follows the same scope. It does not dynamically
discover every possible runtime configuration. Treat expanding markets or
horizons as a contract change, including the adapter, labels and filters.

## Source authority

| Display / purpose | Source | Rule |
| --- | --- | --- |
| Latest Paper portfolio | `_paper/ledger.sqlite3`, latest committed cycle and corresponding equity rows | Read one SQLite transaction; avoid combining different marked observations. |
| Pool equity / P/L / fees / gross exposure | Recorded pooled state and equity | Select the pool once. P/L is `total_pnl`, not `net_realized_pnl`. |
| Account cards | Recorded account state/equity with opening baseline | Account P/L already adjusts for net virtual transfers. |
| Positions | Ledger inventory plus matching marked cycle positions | Reuse a mark only when inventory quantity and entry match that observation. Missing marks remain missing. |
| Equity / P/L / drawdown chart | Ledger `equity` history | Select one account or `pooled`; last observation per bucket, never sum valuations. |
| Historical drawdown curve | Adapter projection of opening baseline + `total_pnl` | Running peaks precede sampling; internal transfers cannot manufacture an account drawdown. |
| Headline worst drawdown | `_paper/performance.json` | Display the export's own `as_of_utc`; it can lag the ledger/chart. |
| Decisions / fills / transfers | Corresponding SQLite journals and decoded `details_json` | Latest 500 rows per journal by default; preserve recorded IDs and execution quantities. |
| Latest model forecast | `_models/<coin>/15m/h4/latest_prediction.json` | Explicit qualification, source state, timestamp and probability validation. |
| Model provenance | Matching `runs/<model-id>/record.json` and `report.json` | Publication time and fitting/calibration cutoffs remain separate. |
| Runtime health | Saved data/model/Paper status and read-only process-identity inspection | Saved `running` alone is not current process evidence. |
| Duration details | Data cycle timing and model training events/status | Work, queue, polling and publication are separate measurements. |

The adapter never constructs `PaperLedger`, fetches exchange data, changes a
policy or invokes a runtime control. Its SQLite connection uses `mode=ro`,
`query_only`, a transaction, bounded busy waiting and a query deadline. SQLite
may use its own WAL coordination sidecars; read-only here means no ledger or
application-state mutation, not a promise that SQLite never touches a sidecar.

If a fallback equity observation belongs to a different cycle, older marked
state is discarded and the result is partial. Missing tables, invalid JSON,
inconsistent probabilities and unavailable horizons remain visible as missing
or partial evidence. Source-file errors do not become fabricated zero balances.

## Metric interpretation

| Label | Meaning |
| --- | --- |
| Pool equity | Current marked sum of the three virtual accounts |
| Paper P/L since start | Change from opening marked equity, after costs; inherited historical gains/losses are excluded |
| Return vs opening | `total_pnl / initial_equity`, not a separate time-weighted return |
| Gross exposure | Absolute exposure, including opposing legs separately |
| Free cash / collateral | Ledger availability before additional strategy reserve rules |
| Fees paid | Cumulative simulated fill fees |
| Funding · estimated | Signed estimated cashflow; pending settlement errors remain visible |
| Worst drawdown | Positive magnitude of the export's historical pooled drawdown |
| Position unrealized P/L | Relative to retained entry; may include performance predating Paper |
| Qualified / Research | Recorded model evaluation status, not winning/losing trade classification |
| No forecast / Risk exit | No accepted signal is attributed to the decision/fill; independent risk reductions are not relabeled Research trades |

Current Paper allocation requires fresh Qualified forecasts. Research previews
can still appear in the model panel while the strategy holds current exposure
subject to risk limits. A rejected Research publication is inspectable under
`details.policy.rejected_forecast`; the decision/fill's top-level forecast/model
IDs, qualification and probability stay null. The **Reason** column distinguishes
exclusion, unavailable forecast and risk-exit decisions. A saved rejected model
is audit context, not an execution signal.

For Paper Hold/Skip rows, **Reason** identifies the actual recorded entry band,
cooldown, account direction, unchanged target, adjustment-size check or failed
execution check. **Decision checks** shows thresholds and cooldown time from
that saved decision, not today's configuration. The model's Qualified label is
preserved because qualification and order eligibility are separate facts.
Filled/partial quantities and the fill journal are never synthesized from a
Qualified label.

Older generic `signal_rebalance` records can display a more precise explanation
only from their saved nested execution or policy details. Missing historical
cooldown/threshold evidence remains unavailable. The original saved record and
ledger history are unchanged. A restart of the UI loads the new presentation;
its five-second data refresh does not reload Python code.

## Interaction and refresh contract

- Default view reads run approximately every five seconds. Only one background
  read is in flight; a queue transfers results to the Tk thread. Refresh does
  not start data, model or Paper workers.
- Filters, chart metric/range and stable selected row identities survive reads.
  Destroying the view cancels callbacks and prevents an outstanding result from
  touching destroyed widgets.
- Resizing between wide, compact and narrow layouts keeps the forecast/detail
  container on the same geometry manager (`grid`). Resize callbacks tolerate
  view destruction during idle layout updates. Closing/reopening Duckets does
  not stop or restart the independently launched data, model or Paper workers.
- Candidate freshness uses the model runtime's accepted `retrain_seconds`,
  currently 900 seconds; its stale threshold is twice that cadence. Older status
  files without a valid cadence retain the historical 3,600-second fallback.
- Account selection also selects the chart. Asset/model filters apply to loaded
  activity and shared forecast previews, not an invented asset-specific equity
  decomposition. Forecasts themselves are shared across accounts.
- Transfer account filtering matches either sender or receiver. Current
  transfer rows have no asset/model attribution, so those filters exclude them.
  Inventory is not assigned the latest forecast's qualification by inference.
- Inherited unmanaged spot dust is hidden by default and available through the
  **Inherited dust** toggle. It remains part of ledger valuation.
- A selected decision/fill inspector preserves hold/skip explanations, target
  constraints, requested versus executed quantity, remainder, book/slippage/fee
  details and recorded provenance. Nested decision execution is distinct from
  the outer decision. **Show saved record** exposes the original fields.
- A failed read preserves the previous display with **Read failed** source
  labels and advancing source ages. Successful but partially available reads
  display their actual missing/partial state.
- Pacific-time labels use `America/Los_Angeles`, with UTC evidence in hover or
  detail. If the timezone database is unavailable the formatter labels UTC,
  rather than incorrectly hard-coding PST.

## Separate clocks

Data/features describe the last completed candle, forecasts their availability,
model fitting the last candidate publication, model polling the runtime
observation, Paper the quote/risk/valuation observation, and the UI its local
read time. The status strip's most degraded source state must remain visible.

The current UI marks performance exports stale after about ten minutes, while
an unchanged Paper runtime may wait fifteen minutes before its next periodic
export. That alert can therefore be normal export lag. Check the ledger and
runtime separately; it is not a command to restart a healthy process.

Operations presents measured data/model durations without replacing those source
ages. Data publication is not independently timed in every record: an available
residual is labeled **publication + unallocated overhead**, not pure publication
work. See [timing definitions](../hyperliquid-timings.md).

## Powder boundary

Powder keeps the panel layout and shows **REAL MONEY**, with **Not connected**
until its separate ledger contains an actual observation. The
[Powder projection](../../app/services/hyperliquid_powder_view.py) uses a bounded
read-only SQLite transaction through `read_status`; it never constructs a ledger
or signer. It displays actual equity, available cash, managed positions, order
intents and confirmed fills, preserving fee currency. Stale observations remain
labeled. Spot entry basis is unavailable; its stop reference is not cost basis.

P/L, drawdown, aggregate costs/funding and transfer history stay unavailable until
complete live cashflow accounting exists. The shared model panel remains labeled
as a forecast preview. Switching modes never converts Paper holdings or enables
trading. Refresh and inspection have no lifecycle or execution authority; the
user uses the separate [activation commands](POWDER_ACTIVATION.md).
Powder shares the now-qualified-only configuration on future activation, but
retains zero targets for missing/rejected signals. The UI must not imply that
Paper's no-signal hold rule applies to real-account execution.

## Verification and visual references

- [Adapter tests](../../tests/test_hyperliquid_paper_view.py): accounting,
  sampling, filtering, missing/stale/partial state and read-only behavior.
- [Workspace tests](../../tests/test_hyper_workspace.py): bounded refresh,
  selection, partial fills, transfer-safe drawdown and separation of Paper/Powder.
- [Powder projection tests](../../tests/test_hyperliquid_powder_view.py): actual
  observation provenance, no invented P/L and retained fee tokens.
- [Tab integration](../../tests/test_ducket_bucket_app.py) and
  [manual UI regression tests](../../tests/test_hyperliquid_duckets_ui.py).
- [Offline/current-local fixture](../../tests/visual_hyper_workspace_fixture.py)
  and [implementation screenshots](../hyperliquid-duckets-design-ui/hyper-2026-09-25/IMPLEMENTATION.md).

The dated fixture screenshots are visual evidence, not current portfolio state.
The current-local preview option reads once before Tk starts; production
H.Y.P.E.R. performs the ongoing background reads.
