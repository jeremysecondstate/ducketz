# Independent 15-minute preparation and receipt contracts

These shared correctness repairs preserve each PC's model ownership. Installing
source does not migrate a recipe, qualify a model, start a trader, clear a manual
stop, or change a provider subscription.

## Collection and coverage

The normal collector reserves progress for current market observations,
background context, and bounded recovery. Schwab stream bootstrap has its own
bounded concurrency lane so that chain enumeration cannot occupy the REST
quote lane. Option-book requests retain the reviewed capacity and distribute
that capacity across underlying symbols before rotating within each chain.
Full-chain bootstrap archives canonical provider JSON and source-scoped failures
with actual receipt clocks, immutable checksums and capture references. Restart
verifies these retained objects before cache reuse; they are provider payloads,
not a claim to retain byte-for-byte HTTP wire traffic.

A completed bounded stream capture with partial scope settles that window's
work item. Its immutable receipt remains `PARTIAL`; it never claims full-chain
delivery. Retrying an unchanged cached capture cannot recover missing records.
Future windows perform new collection. Source-specific denials remain blocked;
authentication, subscription, capacity, timeout, empty, stale, and partial
outcomes retain distinct evidence.

Requested keys, submitted keys, acknowledged keys, observed native records,
fresh records, persisted records, and eligible model observations are different
counts. A weekend subscription acknowledgment proves neither fresh quotes nor
book delivery. An empty response never establishes market-wide no trading.

## Context version 2

`intraday-causal-market-context-v1` remains exactly loadable. Its saved recipe
continues to exclude partial snapshots. The explicit version 2 migration admits
eligible observed records in `PARTIAL` snapshots, while retaining the partial
flag, original source identity, coverage evidence, record exclusions and actual
receipt cutoff. Failed or capacity-blocked snapshots remain unavailable.

Version 2 also exposes observed quote and book economics that the original
feature projection omitted: per-source option spread and depth imbalance, plus
stock book depth statistics. Each statistic describes only that source's
observed subset; it is not a full-chain or consolidated-market estimate.
Missing greeks and missing book sides remain missing. Actual option stream
`C`/`P` codes map to call/put, and daily cumulative volume contributes once per
latest contract observation. It never becomes quarter trade volume.

Version 2 also projects numeric fields from collected financial statement,
float, rating, target-summary and filing families that previously contributed
only receipt age. Float fractions use observed float and outstanding shares;
statement ratios use the same record and fiscal period. Signed cash-flow
amounts use `sign(x) * log1p(abs(x))`; growth keeps the provider's signed ratio.
Rating scores remain numeric provider scores, not invented sentiment. Target
averages and counts retain their month/quarter/year/all-time window; an empty
window supplies no target price. Filing counts describe only the bounded
observed source response. Latest eligible periods and original publication and
receipt gates remain authoritative. No fiscal date becomes an acquisition date.

The feature contract, ordered names and cache identity bind the version.
Old vectors cannot be reused under a new contract. A migration is an explicit
local assessment and installation operation, never a side effect of loading
shared source. Provider receipts retain their original bytes and clocks.

## Historical recovery

`intraday-covered-native-recovery-v2` is an explicit recipe opt-in. It adds
verified same-source native history through a private immutable receipt
manifest; canonical originals remain in place. Raw native payloads are decoded
and compared with their normalized projection. Exact duplicates are counted,
conflicts are retained, and incomplete or conflicting revisions do not silently
replace a previous price.

Recovered candles retain actual acquisition availability separately from the
historical feature-cutoff reconstruction. Training is unavailable before the
input receipt, including the availability of all trailing feature candles and
the target. Today's receipt never becomes yesterday's context observation.
The original actual receipt gates for calibration and chronological validation
remain intact. Consequently some recovered observations require later genuine
calibration dates before they can train a qualified model. Historical
reconstruction and contemporaneous replay must be reported separately.

## Training and assessment

The optional frozen model projection
`drop_base_training_constant_context_v1` removes constant optional context
coordinates after training-only imputation. This matters for estimators whose
feature sampling depends on total dimensionality: freezing constant values
alone still changes a forest's effective sampling width. The projection learns
from the base training partition only; calibration, validation, and inference
cannot activate unsupported columns. Legacy fitted transformers and recipes
retain their original behavior.

The frozen-ensemble assessment records the exact local recipe, member
contracts, source identity, feature matrix and chronological populations. It
assesses the actual manual ensemble separately from supervised shortlisting.
Repeated use of a spent holdout is diagnostic and never new independent
qualification. Support and calibration gates are unchanged. Development-only
comparisons preserve regressions and do not promise future qualification.

## Non-trading preparation

`ml.intraday.preparation.prepare_cycle` accepts saved histories from the normal
data feed and prepares an exact reference under the frozen local recipe. It
persists source-bound training identities, mature-label counts, class support,
missing inputs and per-symbol failures. Optional fitting reuses only verified
artifacts for identical training inputs; it never reuses an old inference
probability. Changed inputs or source contracts require new evidence.

The module's CLI prepares an explicitly named reference without account or
order construction. The normal collector and closed-quarter minute feed remain
independent of model success. Existing manual runtime continues collection and
preparation each quarter after its human start. Offline replay of saved quarters
can prove maturity, persistence and restart behavior, but cannot demonstrate a
fresh market observation on a closed session.

The explicit `--run` mode watches saved observations without fetching data,
installing a service, starting a trader or changing a manual stop. It consumes
the normal acquisition journal through read-only bounded rowid queries and
maintains its own small indexed checkpoint database; it never scans the large
forecast database. Immutable attempt identities, actual receipt clocks, invalid
records and conflicting revisions remain evidence. A completed repeated source
snapshot retains its earliest adequate receipt. Contradictory or unusable new
observations are withheld until a verified replacement resolves their scope;
a failed request does not itself erase an already observed price.

Each new quarter, newly mature saved observation, or eligible context receipt
can trigger preparation. Exact completed input signatures survive restart and
avoid repeated fits on unchanged support failures. New source or recipe bytes
require a restart with reviewed inputs. Calendar and DST handling include the
final session quarters and delayed receipts after the close; these datasets
are explicitly preparation-only and cannot become executable forecasts. Local
stored observations are paged without truncating a PC's frozen population.
The caller owns the stop event, clock and polling lifecycle. Installing source
alone does not launch this watcher.
An owner-local lifetime lock permits one watcher at a time and is released by
the operating system after exit. Temporary execution failures have three durable
attempts per unchanged input signature; restart verifies and promotes a saved
final attempt if its completion-marker write was interrupted.
The watcher records the expected calendar reference and selects each symbol's
latest eligible completed saved observation for preparation. A later empty or
delayed interval is recorded as a missing expected reference; its price is never
filled. The selected observation retains its original availability provenance.
This per-symbol preparation map cannot move a live executable forecast target.

Shared source publication, local source installation, local recipe migration,
model qualification and running-process adoption are recorded separately.
