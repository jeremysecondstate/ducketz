# Paper portfolio loop

Updated for qualified-only Paper behavior on **2026-09-26**. This is the
virtual execution consumer of completed market/model artifacts. It does not
train models, sign orders, synchronize later real-account balances into Paper,
or provide Powder execution.

See [accounting and risk](../PAPER_ACCOUNTING_AND_RISK.md) for formulas and
[the component guide](../../hyperliquid-paper.md) for CLI usage.

## Ownership, inputs and outputs

| Concern | Current contract |
| --- | --- |
| Entry point | [`ml.hyperliquid_paper_runtime`](../../../ml/hyperliquid_paper_runtime.py), `PaperRuntime.run` |
| Settings | [`configs/hyperliquid-paper.json`](../../../configs/hyperliquid-paper.json); loaded when the process is constructed |
| Universe | Model/market configuration; enabled symbols are reloaded on each tick |
| Horizon | First model horizon, currently 4 × 15-minute bars |
| Forecast | `_models/<coin>/15m/h4/latest_prediction.json` plus matching `runs/<model-id>/record.json` |
| Features | The forecast's exact `<coin>/15m/runs/<data-run-id>/features.parquet` row |
| Market evidence | Public metadata, separate spot/perp books and published funding history |
| Durable truth | `_paper/ledger.sqlite3` with transactional accounts, inventory and journals |
| Operator projection | `_paper/_runtime/status.json`, `opening_snapshot.json`, `policy.json`, dated performance and Parquet exports |
| Single writer | `_paper/_runtime/.paper.lock`, acquired without waiting |

Public quote work uses [PublicPaperMarket](../../../ml/hyperliquid_paper_market.py),
an allowlisted `/info` reader. Only the initial mirror step uses public owner
account information; [mirror_accounts](../../../ml/hyperliquid_paper_seed.py)
does not create a signing client. The ledger itself performs no network calls.

## One process lifetime

1. Validate paper/model configuration and common datastore root. Only `mode=paper`
   is accepted; choose the first configured horizon.
2. Acquire the datastore's exclusive paper lock.
3. Open the existing completed ledger. On first creation only, mirror validated
   account inventory, or use explicitly selected manual cash. Missing mirror
   evidence does not fall back to manual balances.
4. Publish the persisted opening snapshot and policy settings. Hash settings plus
   recipe `direction-volatility-v2-qualified-hold` into `policy_id` when
   qualification is required; legacy mixed mode retains `direction-volatility-v1`.
5. Reconstruct stop cooldowns from prior `stop_loss` fills; read funding cursor.
6. Run the tick below, then wait the configured 30 seconds. Actual start-to-start
   duration includes tick work and network waits. `--once` also mutates Paper;
   it is not a read-only diagnostic.
7. On graceful stop/SIGINT/SIGTERM, exit the loop, publish stopped/failed status,
   remove the stop request, export journals and close the ledger.

Changing policy configuration requires a restart to load it consistently. Market
universe reload is separate. Existing seed/cycle history survives both changes.
For the September 26 experiment, the previous ledger was deliberately archived
under `_paper_archives/20260926T073643Z-research-and-qualified`; the new mirror
copies opening inventory/cash before any first-cycle risk or allocation changes.

## Tick sequence

1. Read configured, inherited and currently held symbols. Their union keeps
   removed-but-held assets visible for valuation/reduction.
2. Fetch public spot/perp observations (or reuse first initialization's seed
   quotes). Build marks only from usable returned markets.
3. Value the entire ledger. Missing held-asset marks raise before further tick
   work; there is no automatic substitution of last-known marks.
4. If the five-minute funding check is due, reconstruct settlement quantities,
   request published rates and commit deduplicated estimated funding events.
5. For each symbol, validate its forecast and exact source feature. Current
   qualified-only mode holds exposure when evidence is missing/invalid/stale or
   Research; that absence cannot create signal exposure or transfers.
6. Calculate current managed quantities, entry-based stops and collateral
   excess; check the cycle key before ordinary processing.
7. Derive pool targets, apply stop cooldowns, reject unusable executable quotes,
   plan virtual transfers and apply local account capacity limits.
8. Simulate reductions before increases, reserving consumed book depth. Commit
   transfers, fills, decisions and valuations as one ledger cycle.
9. Commit a separate polling-bucket mark cycle, update status with portfolio and
   per-symbol/quote/funding errors, then export/report if trades/transfers changed
   or 900 seconds have elapsed since the previous export.

The runtime catches per-symbol processing errors and continues to other symbols.
A whole-tick exception produces `degraded` status with `last_error` instead.
Published `status=running` may still have nonempty `errors`, `quote_errors` or
`funding_errors`; it is not proof of complete fresh evidence.

## Forecast and execution gates

| Gate | Exact interpretation |
| --- | --- |
| Identity | Coin, interval, horizon, model record and model ID must match; data/model run IDs must have the accepted local format. |
| Probabilities | Finite `p_not_down` in [0,1], complementary `p_down`, explicit boolean qualification. |
| Times | Decision ≤ publication ≤ now; decision age ≤900 seconds; outcome remains future and exactly matches the configured horizon. |
| Model age | Model publication is not later than forecast publication and is ≤86,400 seconds old. |
| Volatility | Exactly one source feature row at the decision close; finite nonnegative 20-return volatility scaled by `sqrt(horizon)`. |
| Qualification | Current config requires `qualified=true`, `role=active` and matching record `eligible=true`; Research cannot allocate. |
| No accepted forecast | Keep current target, then apply stops, persisted cooldown and account/symbol/pool exposure caps; legacy `require_qualified_forecasts=false` keeps zero-target fallback. |
| Book | Valid separate market book, no earlier than an accepted signal's publication; 45-second age and 5-second future tolerance also apply to no-signal risk reductions. |
| Fill | Market precision, visible depth, adverse slippage and $10 minimum; a zero/partial fill is not the desired position. |

The public market layer independently enforces a fixed 45-second book age.
If a required risk-reduction book is unavailable, the account keeps its target
at current exposure and the coin's increases are suppressed. A held market
omitted by the public snapshot fails the earlier whole-ledger mark gate instead.

Rejected Research provenance is retained in `details.policy.rejected_forecast`.
No-signal decisions/fills have null top-level signal IDs, qualification and
probability. Reasons distinguish `unqualified_forecast_excluded`,
`qualified_forecast_unavailable`, `stop_loss`, `stop_cooldown` and `risk_cap`; underlying forecast validation
errors remain in policy details. A later accepted Qualified forecast resumes
allocation. Model publication itself still permits a labeled Research fallback.

## Replay identities and restart behavior

| Key | Scope |
| --- | --- |
| `forecast:<prediction-id>` | Ordinary processing once for a saved forecast |
| `stale:<coin>:<floor(now/900)>` | No-forecast processing once per 15-minute bucket |
| `risk:<coin>:<floor(now/poll_seconds)>` | Revisit an already processed key when stops/over-limit risk require it |
| `mark:<floor(now/poll_seconds)>` | Separate portfolio valuation bucket |
| `funding:<coin>:<settlement-ms>:<account>` | Funding event and cycle identity |

`execute_cycle` checks identity inside `BEGIN IMMEDIATE`, returns existing results
for duplicates, and rolls the whole cycle back on failure. A crash after commit
but before status/export publication leaves recoverable committed truth.
Funding replay remains deduplicated if its JSON cursor lags that commit.

Ordinary processed keys also suppress repeated partial-fill/unfilled attempts;
retry on every 30-second poll is not guaranteed. New forecasts, later stale
buckets or the qualifying risk path create another opportunity. Stops are
polled entry-loss reductions, not resting exchange orders. One-horizon cooldown
is restored from the last committed stop fill for each account/coin.

Sources: [runtime methods](../../../ml/hyperliquid_paper_runtime.py),
[transactional ledger](../../../ml/hyperliquid_paper_ledger.py),
[policy](../../../ml/hyperliquid_paper_policy.py).

## Observation and stop boundaries

The UI's five-second local read is independent of this loop. Ledger valuations,
forecast availability, model publication and performance-export time are separate
clocks. Shutdown refreshes Parquet journals but does not call `report()` to rebuild
`performance.json`; retain its own timestamp.

A graceful stop ends future quote/risk checks and does not flatten positions.
Its request is checked between iterations; network/work in the current tick can
delay completion. There is no pause mode that leaves risk management running.
An existing process lock and saved status have different meanings; verify both
process identity and observation age through the read-only UI adapter.

`read_status`/`--status` create a control directory and open the lock, so they are
not strictly zero-write filesystem reads. This analysis was written without
starting, stopping, reseeding or otherwise running the paper loop.
