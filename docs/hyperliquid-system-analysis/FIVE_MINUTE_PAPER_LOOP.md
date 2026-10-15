# Five-minute fetch, retrain, predict and Paper loop

Architecture update: **2026-10-15**. This applies the stock 15-minute trader's
fresh-fit ordering to Hyperliquid's existing native five-minute data and Paper
execution. Candles, storage, preprocessing, model fitting, predictions, public
books and funding observations are real. Orders, fills and portfolio transfers
in the Paper ledger are simulated.

## One entry point, ordered work for each market

Start the configured loop from the repository environment:

```powershell
python -m ml.hyperliquid_paper_loop
```

The launcher owns the existing market-data coordinator, model runtime and
Paper runtime. It does not activate Powder or live exchange order submission.
These remain three independent workers with their existing single-writer
locks, durable artifacts and error reporting. The required ordering is
enforced for each market's source snapshot; it is not a batch barrier that
requires every coin to finish before another coin can produce a forecast.
Paper's public quote, valuation and risk checks continue while fitting is in
progress; new signal-driven exposure still requires the validated forecast.

```mermaid
flowchart LR
    API["Real public 5m candles"] --> MERGE["DataFrame merge and deduplication"]
    MERGE --> STORE["Retained OHLCV + causal features + labels"]
    STORE --> FIT["Fresh fit on exact completed source run"]
    FIT --> PRED["Persist forecast from that fitted bundle"]
    PRED --> GUARD["Paper validates matching training provenance"]
    BOOK["Real public market books"] --> PAPER["Simulated Paper execution"]
    GUARD --> PAPER
    PAPER --> LEDGER["Durable Paper ledger"]
    MERGE -. "Next completed five-minute candle" .-> API
```

Inspect or stop the launched loop using:

```powershell
python -m ml.hyperliquid_paper_loop --status
python -m ml.hyperliquid_paper_loop --stop
```

`--once` performs bounded real work through the same pipeline, including model
fitting and Paper execution; it is not a read-only diagnostic. Existing data,
model artifacts and Paper history are resumed rather than erased. Runtime
status, model qualification, candle freshness and actual simulated fills are
separate facts; inspect their saved timestamps and identities.

## Initial history and subsequent overlap fetches

The initial request uses the public Hyperliquid `candleSnapshot` endpoint's
available history window of up to 5,000 candles, approximately 17.36 days at
five minutes. The request begins at the completed five-minute boundary minus
5,000 intervals. A publication delay such as five seconds does not shift that
starting boundary forward and accidentally omit the earliest candle.

The pipeline validates market identity, interval, timestamps and OHLCV, removes
still-forming candles, and normalizes observations into a pandas DataFrame.
It saves the completed DataFrame as compressed Parquet; data is not confined
to process memory. The existing archive is loaded on restart.

Subsequent updates request an overlap starting two intervals before the last
stored candle opening, extending backward to a recoverable missing candle
when necessary. A timestamp merge replaces overlapping values with the newest
observation, removes duplicates, sorts the rows, and appends new completed
candles. The full older archive remains present even after it grows past
5,000 candles. Periodic full-window repairs also retain this older local data.

The API window is not an archive-recovery promise. Gaps already outside the
available window remain explicitly unrecoverable; no candles or prices are
fabricated. A full window request may return fewer than 5,000 usable completed
rows. Actual counts and coverage are recorded.

Each changed dataset is written as an immutable run containing
`ohlcv.parquet`, `features.parquet`, `labels.parquet`, the feature catalog and
summary. Only then does `latest.json` select it. Unchanged overlap observations
reuse the completed run. These are full Parquet snapshots of the merged
DataFrame, not in-place byte appends. Old run directories currently have no
automatic cleanup, so disk usage grows alongside the retained history.

## Every new source run must finish fitting before prediction

The checked-in model configuration enables `retrain_before_predict: true`,
with native `5m` candles and `horizons_bars: [1]`. In this mode, a newly
completed source run requires a fresh fit before its forecast is published.
The model runtime pins that run's feature/label/catalog files and uses the
fitted bundle from that exact run for inference. It cannot issue a prediction
for the new run using an older qualified model while its fresh fit is pending.

Each successful candidate is still published with its evaluation and
qualification result. A failed fit leaves real failure evidence and does not
silently become old-model inference. A result that is too late for its target
cannot become a backdated live forecast. Catch-up retains the real candles;
it does not create historical trades for missed forecast windows.

Forecasts remain immutable for their decision candle. A retry or restart
reuses an already recorded forecast instead of replacing its probability or
model version. New provenance checks do not relabel historical forecasts as
having been freshly fitted.

Paper independently verifies that a forecast satisfies the configured strict
training contract before using it as a signal. Fresh public books, forecast
age, future target time, model identity and existing risk/capacity checks still
apply. The three workers may poll at different rates: the ordering comes
from their saved source and model identities, not from a timer alone.

## Model and Paper semantics retained

The forecast target stays **P(next five-minute close >= current completed
close)**, with equality counted as not-down. This does not import the stock
trader's reference-open-to-third-close target, session hours or broker rules.

The configured five-family ensemble uses Logistic Regression, Extra Trees,
Histogram Gradient Boosting, MLP and Random Forest. It retains the configured
weights, calibration and probability shrinkage. Model changes are explicit
configuration/research changes, not a consequence of using stock-style
orchestration.

Training retains the chronological 70% fitting / 15% calibration / 15%
assessment split with horizon purges. Preprocessing is fitted within the
fitting block; future labels are excluded. The exact evaluated bundle is
published without a hidden refit on reserved evaluation rows. Consequently,
**freshly retrained does not mean fitted through the newest candle**: fitting
and calibration cutoffs lag the newest inference row by their reserved
blocks. Reports preserve those actual cutoffs.

The existing Paper setting is `require_qualified_forecasts: false`, so fresh,
validated research-candidate forecasts may participate in Paper alongside
qualified forecasts. Research status remains visible and does not imply
qualification or profitability. Setting this flag to true restricts signal
allocation to qualified forecasts; the strict training provenance check
applies in either mode.

The Paper ledger continues to record virtual transfers, simulated book-depth
fills, fees, funding estimates, stops and valuations. Its existing forecast
cycle identities prevent duplicate execution after retry. Only the initial
mirror seed uses the real account opening snapshot; later Paper activity does
not mutate real account balances or submit signed orders.

## H.Y.P.E.R. display and a fresh account comparison

The Paper view recognizes the unified launcher's shared process while retaining
each worker's own heartbeat and error checks. Its workflow strip describes the
real five-minute data and retraining stages. The source badges and Operations
detail distinguish a healthy owner from a stopped, stale or failed worker.

A fresh comparison is an explicit experiment rollover, never an in-place ledger
reset. Use `ml.hyperliquid_paper_review begin --cycle-id <unique-id> --reason
"Human-requested fresh account comparison"`, stop the whole Paper launcher, then
run the review's `archive`, `prepare` and `accept` stages with that same cycle ID.
Verify the untouched opening before acceptance, restart the launcher, verify
post-opening accounting and advancing trading health, and supply that health
report to `complete`. Complete source changes before `accept`, which binds the
new experiment to their exact hashes.

Archive preserves the previous Paper ledger and fitted models with checked
hashes. The growing five-minute candle archive and real-account equity history
remain in place. Preparation reads the real accounts without submitting orders,
mirrors their collateral and holdings, retains perpetual historical entries,
and creates an opening with zero simulated fees and zero experiment P/L.
Sequential API reads and market marks can produce small valuation differences;
the source and marked opening totals are retained separately.

The performance chart starts both series at the new opening. Paper P/L uses its
marked opening equity; the real-account series uses the real balances recorded
at that opening. Later real observations are retained while the app collects
them. Real equity change includes deposits and withdrawals, so it is not a
cash-flow-adjusted strategy return. Paper trading may change the inherited
holdings immediately after the verified opening.

## Evidence and implementation map

| Concern | Implementation and evidence |
| --- | --- |
| Launch and lifecycle | [`ml.hyperliquid_paper_loop`](../../ml/hyperliquid_paper_loop.py), launcher status and worker status |
| Public fetch and canonical DataFrame | [`hyperliquid_candles.py`](../../datafetching/hyperliquid_candles.py), actual fetched/closed row counts |
| Archive, overlap repair and atomic snapshots | [`hyperliquid_data_pipeline.py`](../../ml/hyperliquid_data_pipeline.py), source run summaries and Parquets |
| Fit ordering and measured attempts | [`hyperliquid_model_runtime.py`](../../ml/hyperliquid_model_runtime.py), model runtime status/training events |
| Causal fitting, calibration and assessment | [`hyperliquid_models.py`](../../ml/hyperliquid_models.py), per-model timings and split cutoffs |
| Immutable model/forecast publication | [`hyperliquid_model_artifacts.py`](../../ml/hyperliquid_model_artifacts.py), model records and forecast journal |
| Paper forecast validation and accounting | [`hyperliquid_paper_runtime.py`](../../ml/hyperliquid_paper_runtime.py), Paper status and committed SQLite cycles |

Offline tests exercise bootstrap boundaries, duplicate corrections, retention
beyond the API window, exact-source fitting order, failure/expiry behavior and
Paper validation. They do not establish live provider availability, actual
five-minute workload feasibility or profitable performance. Runtime journals
provide that measured operational evidence after an authorized start.
