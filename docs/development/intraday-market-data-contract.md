# Fifteen-minute market data and causal model context

This contract applies to the independently started 15-minute trader. It does not
select another PC's symbols or models, promote a research candidate, start
trading, or change the nightly Gameplan/Loop B populations.

## Price observations and verified absence

An empty response is not a price and does not establish that no market-wide
trade occurred. The `intraday-covered-native-interval-v1` population aggregates
genuine one-minute observations inside the exact quarter. It never fills a
minute, carries a previous close into a new quarter, or fabricates an entirely
empty quarter. Sparse live quarters require stable consecutive covered source
responses. The original observation count and source-empty count remain saved.

The separate legacy minute-fill path now requires a typed source-absence proof:
the exact mapped symbol, source/dataset/schema, interval, request/receipt clocks,
fully decoded persisted response, raw-content and canonical-row hashes, and the
supported omission semantics. Two consecutive successful full responses must
agree after the required wait. Errors, source lag, partial parsing/persistence,
changed rows, or missing mapping evidence invalidate confirmation. A same-session
adjacent known price is also required. Filled prices remain explicitly synthetic
with their constituent evidence; they never become raw observations.

The provider's [OHLCV documentation](https://databento.com/docs/schemas-and-data-formats/ohlcv)
defines omission for an interval without a trade in that source. This is a
source-specific convention. EQUS.MINI absence does not establish consolidated
market-wide inactivity. A string citing the convention is insufficient proof.
Wholly empty unresolved responses remain unavailable.

All training paths reject unsupported synthetic rows before constructing
features or labels. The opt-in native manual recipe excludes synthetic data
entirely, including verified fills. Historical raw/normalized files are retained;
the native historical loader records canonical-file hashes and explicitly labels
close-time availability as assumed. Fetching a file today does not establish
that its revised historical contents were available at an earlier decision.

## Collection, storage and retraining

`market_collection.py` is a market-data-only worker. It has no order/account
execution dependency. It stores immutable source receipts, request windows,
event clocks, actual receipt/availability clocks, content hashes, capability
states, partial results and sanitized failures under the PC's private intraday
data directory. The durable queue resumes unfinished requests, deduplicates
completed tasks, honors bounded provider concurrency/backoff, and gives fresh
quarters priority over recovery work. Unsupported products and unverified
entitlements remain explicit.

The provider adapters use the existing local connections and configured scopes:

| Source | Intended inputs | Important boundary |
|---|---|---|
| Databento EQUS.MINI | Native equity trades, top of book, bars, definitions | This feed is not a consolidated all-venue trade tape or full-depth product |
| Schwab market data | Equity quotes/history, full returned option-chain Greeks/IV, native equity/options/book and configured futures stream observations | Read-only user preferences supply the stream connection IDs. No financial-account or order service is subscribed. A late or delayed quote is unavailable for current features |
| FMP | Entitled quotes/bars, corporate financials, actions, events/news, analyst estimates/ratings/targets, DCF, 15-minute technical indicators and configured macro series | Per-endpoint responses establish access; the supplied Premium plan does not imply Ultimate one-minute or options access |
| Databento OPRA | Local equities' option-parent market records | Provider Greeks are not invented from OPRA records |
| Databento CME | Existing locally configured context/contract scopes | No peer symbol or tuning import and no automatic new subscription |

Databento context collection uses bounded Live replay instead of silently
buying historical queries. [Live replay](https://databento.com/docs/api-reference-live)
has a limited retention window; weekend availability can be shorter. Reconnects,
unresolved symbology, missing completion markers, time limits and record limits
must be reported as incomplete, never full coverage.

The v4 primary minute adapter also uses independent Live replay. Its local
request bounds are not historical-provider availability claims. A receipt must
bind the exact quarter, native schema, mapped symbols, control acknowledgements,
decoded row count and persisted DBN hash. Each sparse confirmation makes a fresh
gateway capture. The prior historical adapter remains bound to older recipes.

Native context capture streams DBN to private storage, then decodes bounded
chunks. The versioned model projection preserves native bars; trade projections
record actual first/last trades, counts and summed size per instrument. Quote
projections retain actual native observations. Full raw responses remain
available for later audit. Memory/record, byte and time limits remain explicit;
reaching a limit cannot produce a complete-quarter claim.

In a manual session whose private recipe opts in, the context collector runs
independently of the existing minute collector, forecasting and scoring workers.
Price or model failures do not stop unrelated source collection. Each model
iteration reads only the receipts actually available at its frozen cutoff.
Newly matured labels enter the next quarter's training population; both configured
models are fully refitted using the saved row limit and purged calibration split.
Already issued forecasts and input snapshots remain immutable.

Current snapshots or nonreplayable streams use their actual capture clocks.
They cannot reconstruct the requested historical quarter. A successful request
is evidence about its defined source and capture scope; it is not proof of an
entire market's transactions, every venue, every possible entitlement, or all
history for a paginated endpoint.

Schwab streams and OPRA CMBP use five-second current observation windows after
their subscription acknowledgements. They are samples at each refresh, not an
uninterrupted tick archive between refreshes. Their native market payloads are
retained privately; login credentials and account messages are not persisted.
Unknown/mismatched symbols, rejected subscription chunks, early disconnects,
short observation windows and explicit delayed flags remain unavailable or
partial. Futures root aliases require an actual active-contract identity seen
no later than the observation being mapped.

Large native option chains use bounded, rotating subscription subsets. Receipts
distinguish the full requested chain, planned subset, keys actually submitted and
keys acknowledged. Such subsets remain partial; the full returned REST chain is
stored separately. A follow-up control for a capacity-rejected request cannot
override the original rejection or acknowledge another pending subscription.

The data-only CLI can run a bounded batch or an explicitly requested continuous
collector. It never starts the trader:

```text
python -B -m ml.intraday.market_collection --datastore DATA_ROOT --pc scout --symbols LOCAL_SYMBOLS
python -B -m ml.intraday.market_collection --datastore DATA_ROOT --pc scout --symbols LOCAL_SYMBOLS --once
python -B -m ml.intraday.market_collection --datastore DATA_ROOT --pc scout --symbols LOCAL_SYMBOLS --run
```

The inventory command does not establish entitlement. Inspect the resulting
per-source capability statuses and receipts before claiming successful capture.

## Causal context features

`market_context.py` appends a versioned, ordered numeric contract to the existing
price features. It represents quote spread/imbalance, actual trade/bar count and
volume, option Greeks/decimal IV, financial ratios, published events and
configured cross-asset changes. Every numeric feature has an explicit missing
indicator; zero placeholders are an encoding of missing information, not synthetic
price observations. Receipt age, source age, availability and partial status are
also explicit.

Each selected source retains a separate feature block. Multiple providers or
FMP endpoints are not silently mixed. The private recipe freezes source IDs,
feature order, clock policy, maximum ages and the collection configuration hash.
Snapshots are verified before joining. Conflicting observations and partial
responses do not supply numeric market features.

FMP technical inputs freeze the requested interval, calculation period and
provider history window; unfinished indicator bars remain excluded. Economic
period dates never substitute for publication or receipt clocks. The commodity
batch uses only the configured native symbols. Its basket features are the
median observed `price / previousClose - 1`, population dispersion across at
least two valid instruments, and valid-instrument count. Prices are equally
weighted, invalid/missing operands remain missing, and commodity symbols are
never silently treated as futures-contract identities.

Source timestamps retain nanosecond precision during eligibility comparisons.
The immutable receipt index supports exact source/symbol/decision-time reads;
forecast preparation does not scan every raw response for every participant.
The index accelerates discovery and never replaces snapshot-content validation.

For each historical training reference, context is joined at that reference's
own feature/decision availability, strictly before the target begins. It is
never joined at `label_available_at`, which includes the future target receipt.
Current REST snapshots cannot be backfilled into earlier training rows. Newly
introduced context will initially have little or no point-in-time history;
missing indicators remain truthful while real receipts accumulate.

## Local recipe migration and outcomes

The v4 manual recipe is an explicit PC-local migration. It preserves the saved
model names, complete parameters, preprocessing, calibration settings, support
floors, row limit and dataset scope. It selects genuine native observations for
that PC's explicitly enumerated symbols and freezes the added context contract.
Old v1-v3 recipes/artifacts remain readable with their original feature scope.
Shared source adoption never installs another PC's private recipe.

Fit success does not establish supervised qualification or profitability.
Support failures and calibration/validation failures remain visible. More data
may resolve a support problem; no gate is lowered to manufacture success.

New scoring rejects an unsupported synthetic target. For an already saved
synthetic outcome whose proof fails, `invalidate_unverified_outcome` appends an
`unscorable` revision linked to the original outcome and rejected candle. It
does not alter the forecast, original target, old revision, execution ledger,
or trade intent. A newer native candle cannot score a legacy forecast under a
different price-population contract. Current and historical evidence remain
distinct.

## Verification

The focused offline regression suites cover source-proof integrity, missing and
sparse windows, historical synthetic exclusion, immutable outcome invalidation,
durable collector recovery/fairness, market-only provider adapters, causal
context joins, frozen recipe migration and manual worker integration. Tests use
temporary stores and fake provider transports. Real local fits and real provider
receipts belong in private installation evidence, never the shared source queue.

Application installation, published source, main integration and the code loaded
by a running process are separate facts. The human still starts the trader.

## Receipt lookup and derived-context reuse

The collector indexes immutable receipt headers so each training decision selects only
the latest eligible receipt for each configured source at its original cutoff. The
index is a lookup aid; the archived receipt and raw capture remain authoritative.
Feature construction uses a separate disposable SQLite cache per symbol. Its key
includes the frozen context contract, decision cutoff and selected receipt identities.
An unchanged cache entry reuses the previously verified vector. A changed receipt,
contract or cutoff requires verification and recomputation; a corrupt cache entry is
discarded and recomputed. Warm hits validate the derived artifact and original
receipt identity, plus ordinary file-stat changes; they do not rehash every source
file. Deliberate source-byte replacement preserving both file size and modification
time therefore requires a full integrity read to detect. It cannot alter the already
verified cached vector. This avoids rebuilding years of unchanged context at each
15-minute iteration without assigning later information to earlier decisions.

