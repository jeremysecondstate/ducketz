# Receipt-time market inputs for the 15-minute trader

`ml/intraday/market_inputs.py` supplies shared implementation behavior. The
configured local symbols and CME operating bindings remain local. It does not
consume another trader's forecasts or account ledger. No provider client is
constructed by importing either market-input module.

This acquisition is an explicit, bounded suite for configured instruments, not
a claim to ingest every endpoint, every exchange or the entire options universe.
Entitlement failures are recorded; the code never buys an upgrade or switches to
paid historical depth outside the subscription. Existing nightly acquisition is
independent. Missing optional context never authorizes stopping it or a trader.

## Concrete acquisition

| Source | Requests/subscriptions | Bound and retained evidence |
|---|---|---|
| Schwab | Shared multi-symbol equity quote, native one-minute price history, Greeks-enabled chain | Last 30 minutes; 40 strikes and 45 days per configured equity; sanitized adapter-returned payloads and request/receipt times |
| FMP Premium | Quote, native five-minute chart, profile, TTM ratios, quarterly income/balance/cash-flow, earnings/dividends/splits, price-target consensus, stock news | Same-day five-minute request with extended hours and unadjusted prices; four statement rows, 12 event rows and 20 news rows; raw JSON retained |
| Databento EQUS.MINI | Definition, MBP-1, native one-minute OHLCV | Configured equities only; MBP-1 includes trades. It is the Mini source, not a consolidated SIP or live XNAS depth |
| Databento GLBX.MDP3 | Definition, MBP-1, MBP-10, MBO snapshot/updates, native one-minute OHLCV, statistics | Existing `DATABENTO_CME_CONTEXT_SYMBOLS` or ES/NQ/CL/GC/ZN front continuous contracts. Raw events retained; numeric contexts cover ES/NQ/RTY/CL/GC/ZN |
| Databento OPRA.PILLAR | Definition, CBBO-1s, trades, native one-minute OHLCV, statistics | Configured equity `.OPT` parents only; no whole-universe wildcard. OPRA supplies top-of-book, not L2/L3 or calculated Greeks |

No live `XNAS.ITCH` depth or FMP one-minute endpoint is requested. Databento
definitions use live intraday replay; fresh market subscriptions start now.
Their receipt clocks are never backdated to historical exchange event time.
The native one-second bars and duplicate sampled equity BBO representations are
not separately requested: underlying MBP-1 trade/top-book events are retained.
This implementation does not currently construct a stateful CME order book from
MBO, fetch futures-option surfaces, or derive OPRA IV. The additional continuous
Schwab adapter supplies service-scoped Level 2 books as described below.
The CME subscription honors `DATABENTO_CME_CONTEXT_STYPE_IN` (continuous,
raw-symbol or parent); defaults are continuous. Raw MBO snapshot records are
stored but snapshot adds do not count as organic new-order flow. Records flagged
as an unrecoverable book gap are excluded from numeric context.
The established configuration parser handles JSON arrays and delimited symbol
lists. CME MBP-1 includes trades, so a duplicate trades subscription is omitted.
MBP-10 books take precedence over MBP-1 while no more than 30 seconds old.
FMP intraday request dates follow `America/New_York`, including across UTC
midnight and daylight-saving changes, matching the provider candle clock.

Actual provider authorization takes precedence over the stated plan outline.
An explicit schema rejection is persisted, removed from this owner's requests,
and never retried by that owner. The failed connection closes before requesting
the remaining suite; this finite process cannot enable an unlicensed schema.
Dataset-wide authentication errors stop that dataset's optional capture. The SDK's
automatic reconnect is disabled to prevent a permanent authorization retry loop.
Transient disconnections are retried by the next collection snapshot, with any
gap recorded through unavailable inputs. Requested schemas, explicitly rejected
schemas and schemas with received records remain separate evidence fields.

FMP starts a distinct quote/bar lane on every collection. Its own durable cursor
rotates symbols if slow requests exhaust that lane. Independent technical and corporate cursors
continue across the remaining families; corporate inputs refresh every six
hours and news every 15 minutes, measured from the latest attempt, including an
empty or failed response. This prevents corporate progress from pushing fresh
quote/bar requests behind earlier corporate work. Schwab independently rotates
its minutes and chains after the shared quote request.
Each `collect` defaults to a 120-second
request-launch budget; the last in-flight REST call can overrun that budget by
its timeout (Schwab chain 20 seconds, other calls 10 seconds). There are only two
REST workers and no internal retries. FMP spacing caps this worker at 600 calls
per minute, below the stated Premium 750/minute allowance; it cannot account for
unrelated processes sharing that provider account. Deferred work and errors are
explicit in each cycle manifest. The fast FMP lane launches requests during the
first 50% of the budget (60 seconds by default), technical work until 75%
(90 seconds), and corporate/research/global work uses the time remaining. An
unspent earlier lane budget is available to subsequent lanes. Each cursor
resumes fairly in the next ordinary iteration; missing optional context never
changes the core forecast schedule.
Every remaining unattempted job is named when a budget expires; the cadence is
an attempt schedule, not a guarantee that every provider responds within it.
The forecast worker consumes available records
without waiting for optional providers. Configured endpoint availability still
depends on the actual account; no successful live request is implied by offline
tests.

## Expanded receipt schema v2

`intraday-receipt-market-inputs-v2` adds 27 FMP research/technical/macro sources
and 20 Schwab streaming sources. The complete fixed schema has 70 sources,
258 numeric fields and their 258 availability indicators. Together with the
16 price fields, the current implementation has 532 input columns. These are
common implementation fields, not a peer model recipe. Existing v1 receipts
retain their original IDs and clocks; old feature-contract qualification is not
silently transferred to v2. The support transformer still learns solely from
base-fitting rows and freezes unlearned auxiliary coordinates.

The FMP helper supplies annual/quarter estimates, rating/target/grade research,
DCF, press/news/SEC references, nine provider-calculated indicators and four
macro series. Indicator requests are 15-minute, period 10, bounded to 14 days;
macro requests are one inclusive 90-day slice. An actually empty GDP response
permits exactly one preceding, nonoverlapping 90-day slice so quarterly period
dates are not lost at a rolling-window edge. Both raw receipts and the parent
empty receipt ID remain recorded. If the cycle budget ends, this finite pending
request resumes in the next normal pass; it never waits or blocks core work. These are explicit collection
parameters, not changes to estimator/calibration/qualification parameters.
Research pages are bounded and never represented as complete historical counts.
Fiscal/forecast-horizon/period dates never become knowledge dates. Global series
are fetched once and fanned out with exactly the same original receipt clock
and raw hash. Duplicate endpoint/parameter aliases share an actual response,
including an unavailable response, within a cycle. See
[intraday-market-input-research.md](intraday-market-input-research.md).

`SchwabMarketStream` is one asynchronous owned market connection. It receives
native quote/chart/NYSE/NASDAQ/options-book messages continuously, while a
separate bounded chain worker retains each full returned contract-identity
response. Initial and subsequent scope changes coalesce one completed discovery
round, including failed attempts, while the socket reader continues receiving. A conservative 500-membership connection envelope reserves configured stock/
futures subscriptions, then divides the remaining option budget between quote
and book services (the explicit combined option cap is at most 1,000). Each uses
at most 250-key chunks and rotates fairly every 15 minutes. This implementation
bound is separate from actual provider/account entitlement. A code19 rejection
permits at most two smaller scopes: one key per underlying, then one rotating
key total. The owner retains original scope, every attempted scope and ACK, and
remembers the reduced bound rather than resubmitting rejected full scopes. Partial observed-subset features
retain requested/acknowledged/observed scope; this is not full-chain coverage.
Native futures active-root aliases are explicitly derived from supported local
CME roots, but native contract identity and source remain distinct from the
Databento continuous selector. Late or conflicting roll identities cannot be
backdated into chart bars. See
[intraday-market-input-schwab-stream.md](intraday-market-input-schwab-stream.md).

Missingness, source-empty responses and unavailable entitlement are distinct
from actual observed zero values. Nothing in the supplement creates price
candles or changes the verified-source population. No account balances,
positions, order or transaction endpoint is used. Stop closes the owned socket;
in-flight HTTP work can finish its bounded request but cannot resubscribe.
No trader is started by importing or preparing these modules.

## Storage and causal feature admission

All private evidence is rooted at `DATASTORE/intraday/market-inputs`:

- `objects/YYYY/MM/DD/<sha256>.json`: compact immutable receipt envelope with
  sanitized request parameters, actual request-start/receipt clocks, numeric
  extract/status and `payload_ref`. The ID hashes the exact canonical JSON bytes.
- `raw/YYYY/MM/DD/<sha256>.json`: JSON-safe adapter-returned payload after secret
  field/query redaction, hash-bound by the envelope's `payload_ref`. The Schwab
  quote adapter flattens the HTTP response envelope before this capture; these
  objects therefore do not preserve every original HTTP field. Separating
  payloads allows selected receipt hashes to be verified on every feature join
  without re-reading large options chains. `read_response(root, object_path)`
  checks both hashes and returns the expanded envelope and payload.
- `receipts.sqlite3`: indexed catalog of symbol/source/receipt and object path.
  Features may be read through short `mode=ro` connections. Historical joins
  bound their catalog read to 200,000 records and do not read raw payloads.
- `cycles/<sha256>.json`: actual receipts, deferred work, failures and refresh
  policy for one collection attempt. Repeated attempts never overwrite it.
- `collector-state.json`: latest scheduling cursors only. This small mutable
  operating pointer prevents starvation after restarting an explicitly owned
  collector; it is not historical source evidence.
- `streams/<dataset>/YYYY/MM/DD/<uuid>.frames`: original DBN market records with
  local receipt time. Each frame is an unsigned 64-bit big-endian Unix receipt
  timestamp in nanoseconds, unsigned 32-bit big-endian byte length, then those
  DBN bytes. A same-name JSON manifest records the sealed segment's hash, count
  and receipt range. Only an actively written segment is mutable; snapshot/stop
  closes it and the filename is never reused.
- `stream-errors/YYYY/MM/DD/<sha256>.json`: bounded sanitized provider error
  code/message and actual receipt clock. Known API key values, credential-shaped
  assignments, bearer tokens and URLs are removed before writing. Errors also
  accompany the affected context receipt; raw error bytes are not retained.

REST raw payloads are durably retained before extraction, with a defensive copy
passed to extractors. Raw/envelope/cycle files publish complete fsynced temporary
bytes with an atomic no-overwrite link; failures do not leave truncated final
hash paths. Each cycle freezes the actually loaded collector/helper functions
and source objects independently of future disk edits.

Callbacks write raw records before derived feature processing. The model summary
uses receipt-minute sufficient statistics plus the latest book per instrument,
so thousands of updates do not require thousands of retained feature rows.
The rolling window begins at the minute floor of receipt time minus 15 minutes
(therefore 15–16 minutes), with exchange event time no later than receipt.
Published statistics can remain for three days, with their actual receipt
recorded. A maximum 200,000 instrument/state entries per dataset bounds derived
memory. Overflow is explicitly recorded and makes the affected derived window
unavailable until 15 minutes after the last overflow; raw records continue to be
preserved. Snapshots reference all raw segments covering their window and older
segments needed for retained definitions/statistics.
Definition identities and receipt times accompany the selected instruments.
Provider errors use the sanitized diagnostic format above because they can echo
authenticated URLs. Every configured recognized CME root receives an explicit
unavailable snapshot when no usable data exists.

`augment_snapshot` appends fixed `MODEL_FEATURE_NAMES` to a copy of the existing
snapshot. Each numerical feature has a separate `__available` column. Missing
values are encoded as `0, 0`; an observed zero is `0, 1`. The first zero is an
explicit model input encoding, never a synthetic market observation or a volume
claim. There is no future backfill: historical rows join only receipts available
by their reference opening plus 15 minutes, also bounded by the fit cutoff;
inference joins only receipts available by its actual cutoff. This conservative
historical clock can use a previous collection's context rather than a later
receipt. Newly acquired corporate history is not treated as known before its
first local receipt, even if its fiscal period is old.

Most inputs expire 30 minutes after receipt. Corporate inputs expire after 48
hours and news after two hours. A newer unsuccessful receipt is represented as
missing instead of silently resurrecting an earlier successful quote. Retained
raw responses support later independently authorized feature work; endpoint
availability is not proof that every saved field contributed to this model.

Features include spread and size imbalance, native observed-bar returns/ranges
and volume, Schwab IV/Greeks and put/call support, FMP fundamental/event values,
Mini trade counts/volume/VWAP, OPRA quoted contracts and trade ratios, and CME
depth imbalance/trade flow/cancel-add counts/statistics. Undefined prices,
nonfinite values, incomplete recent candles and invalid OHLC are excluded from
their numeric extracts. Snapshot metadata freezes the feature version, receipt
IDs, eligibility/status, expiry rules and missing-value encoding.
Schwab and FMP quote features additionally require a provider quote/event
timestamp no later than receipt and no more than 30 minutes old; missing event
clocks are explicitly unavailable. Schwab chain numeric features likewise use
only contracts with timely quote clocks. Selected receipt envelopes are hash-checked
and their catalog fields compared before use. Corrupt or missing evidence masks
that source and records an integrity reason. Inference information availability
is advanced to the latest eligible auxiliary receipt, and per-column historical
support counts are frozen without implying model qualification.
An unavailable optional catalog, a malformed catalog record or the bounded
receipt-query limit produces the same fixed context columns with missing masks
and a compact read-status reason. Original price values, labels and availability
remain intact. Malformed price snapshots and unrelated programming errors still
raise instead of being disguised as absent market data.

## Integration and offline checks

```python
collector = MarketInputCollector(datastore_root, configured_symbols)
collector.start()                         # only in explicitly started owner
manifest = collector.collect(reference_at) # existing independent input worker
store = MarketInputStore(datastore_root)
snapshot = augment_snapshot(snapshot, store, symbol=symbol,
                            inference_at=cutoff, cutoff=cutoff)
collector.close()                         # preserve manual stop ownership
```

Attach context after checking the selected price-feature policy and before
freezing inputs. On retry the immutable stored input snapshot takes precedence.
The supervision path must perform the same augmentation before any train,
calibration or validation split; preprocessing must remain fitted only on its
base training partition. Old fitted metadata does not qualify the expanded
feature set by itself.

Run `.venv/Scripts/python.exe -B -m pytest tests/test_intraday_market_inputs.py
-q -p no:cacheprovider`. Fixtures exercise actual adapter call signatures, plan
bounds, native DBN record types, receipt cutoffs, missingness, immutable hashes,
raw frame clocks, stream ownership and redaction. They do not call a provider,
read credentials, train, start a trader or alter operational DATASTORE files.

Protocol references checked October 17, 2026:
[Databento Mini schemas](https://www.databento.tech/docs/venues-and-datasets/equs-mini),
[Databento live subscriptions](https://www.databento.tech/docs/api-reference-live/client/subscribe),
[DBN record types](https://www.databento.tech/docs/standards-and-conventions),
[DBN quality flags](https://www.databento.tech/docs/standards-and-conventions/common-fields-enums-types),
[FMP stable endpoints](https://site.financialmodelingprep.com/developer/docs),
[FMP endpoint entitlement caveats](https://site.financialmodelingprep.com/faqs?code=data).
The private local provider-plan outline supplies the installation's stated plans;
it is private context, not provider response evidence.


## Training support for new market coordinates

The independently versioned `intraday-market-variation-support-v1` preprocessing
step follows training-only imputation and precedes scaling and the estimator.
Every `market_` coordinate constant throughout the base training partition stays
at that learned constant during calibration, validation and inference. This
prevents a newly received value from activating an unlearned coefficient or
random neural-network weight. Price coordinates are unaffected. The raw frozen
snapshot preserves all received values and availability masks; the preprocessing
does not invent or overwrite a market observation.

Only variation in a later fresh fit's base training partition admits the field.
Calibration and holdout cannot supply that support. Saved fit evidence records
the support version, fitted row count, constrained names and constants; runtime
provenance includes the transformer. Existing core recipe preprocessing and
candidate parameters remain unchanged. The changed feature contract requires a
new assessment; earlier qualification does not transfer to new inputs.

Regression tests: `tests/test_intraday_feature_support.py` includes an MLP case
with entirely unobserved historical context and changing live context, a
calibration-only variation case, and admission on a later fresh fit.
