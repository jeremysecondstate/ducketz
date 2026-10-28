# Scout independent 1A/1B services

Scout's independent services separate data acquisition and preparation from the
authorized 15-minute trading run. The process topology uses
`scout-independent-v1`. It requires a new bootstrap and the next normal
authorized initial launch; it cannot attach to an already running
`scout-iteration-v1` controller.

## Process ownership

| Role | Owned work | Trading authority |
| --- | --- | --- |
| System 1A data service | Existing market acquisition, normalized observation evidence, causal context, prepared immutable inputs, historical scoring, ready manifests and data recovery | None |
| System 1B model worker | Consume verified exact-reference ready inputs, fit the saved local recipe, predict, publish each completed symbol and record full-scope attempts | None |
| System 1B trading process | Consume immutable forecasts, obtain current quotes and capacity through Scout's existing broker connection, size, send and recover Scout's own orders | Only within its original manually activated logical run and current stop/deadline fences |
| System 1B controller | Own the trading/model process trees and planned model replacements, validate release selections, retain original run identity and stop authority | Does not start System 1A |

System 1A has its own lock, process identity, status, metrics and durable recovery
records. Its service day is independent of System 1B. A stopped trader, missing
model consumer or unacknowledged ready manifest cannot stop collection or
preparation of later inputs. Data collection remains subject to its existing
local authority, source configuration and operating schedule.
The final native data window may arrive after the trading deadline. System 1A
finishes its own latched data day after acquisition completion and owned work
drain; it does not roll yesterday's feed into a new trading day.
After the final native window, a provider-free saved-outcome pass scores the
data day's retained Scout targets. Deferred scoring retains its original
target/evidence and a durable recovery record.

System 1B does not fetch market history or reconstruct datasets in its order
loop. Model fitting runs in a separate process from trading/recovery. The
trading process remains alive across planned model replacements and does not
wait for the next full-scope model iteration before handling a published symbol.

## Immutable handoff

`ml/intraday/service_handoff.py` stores large input objects outside the scientific
database's writer transactions. Objects are written to a temporary file, flushed
and fsynced, then published by an atomic rename. A small ready manifest can
reference an object only after complete object publication and verification.
An interrupted object write never becomes a ready snapshot.
Full input-object verification happens inside each symbol's model work, rather
than in a serial whole-scope binding pass. A missing or briefly busy handoff is
a durable per-symbol deferral and does not prevent another symbol from using
its verified inputs.

Ready identity retains the owning PC and symbol scope, source service session,
reference, actual availability, training cutoff, fixed target definition and
close, local recipe identity, ordered feature hash, source provenance and full
object hash. Consumers validate these bindings and the ready record's immutable
manifest. They retain the original cutoff rather than moving a target or
manufacturing a later trading opportunity.
The snapshot's actual saved recipe is checked against the selected local
recipe's canonical identity, in addition to the recipe file's exact byte hash.

The live ready queue retains at most two unexpired references per symbol for
the current recipe. Newer revisions supersede unsent queue entries; expired work
cannot become a live backlog. Retirement changes queue eligibility and retains
the immutable manifests, objects and recovery evidence. No consumer
acknowledgment is necessary for publication, collection or retirement.

`ServiceStore` keeps the original forecast/cycle API. Existing inline snapshots
remain readable. New service snapshots retain complete scientific inputs in an
immutable object and small execution evidence in the database. The final send
path validates the execution projection's identity without rebuilding the
training matrix. Complete sizing and source evidence remain available for
offline inspection.
The execution indicator projection is bounded to 65,536 serialized characters.
Its features, configuration and provenance retain the original full indicator
evidence ID; complete indicator candles and repeated session history remain in
their independently hashed object. The trading forecast has a separate bounded
serialization gate and retains the same model probabilities, fixed target,
cutoff, direction and execution facts. Complete feature maps, recipe/schema
metadata and individual model diagnostics remain in a hashed scientific-output
object, available through explicit offline/display access.

The immutable execution record binds both the projection and full scientific
output. Cheap file witnesses detect changed or missing retained input, indicator
and scientific-output objects at execution gates after the original full
verification; the broker path does not reread or reserialize those matrices.
Measure forecast payloads and indicator projections separately from the retained
scientific evidence and preparation cost.

Each completed symbol publishes immediately through the existing immutable
forecast store and execution queue. A cycle still covers every configured
symbol. Missing inputs produce durable deferrals; failures retain their actual
failure status. Neither a single publication nor a polling/recovery tick is a
full iteration. Planned model replacement follows a returned full-scope attempt
and a checkpoint validated against the retained scientific store.

## Preparation reuse and resource bounds

The data service uses bounded preparation concurrency and fair symbol scheduling.
An acquisition deferral releases that symbol's slot so other ready inputs can
advance. The model service also uses bounded symbol concurrency. Process
priority and scheduling are resource controls; they do not replace any saved
estimator, preprocessing or calibration parameter.

`ContextCache.build_row` reuses a historical context vector only after that
exact derivation has passed its original source and cache checks. The process
working set contains immutable packed numeric bytes and the original context
hash, rather than references to mutable caller arrays or nested evidence.
Returned NumPy views have a bytes backing and cannot be made writable.

Reuse keys bind the exact derivation implementation, validated context contract
and feature order, source selectors, receipt identities/hashes, symbol, decision
cutoff and local cache namespace. Current receipt witnesses are checked before
use and at the successful dataset boundary. A source change during preparation
rejects publication. Failed preparations publish no new working-set entries.
First-use and evicted rows retain the original verified durable-cache fallback.

The process working set and each preparation's pending set are independently
bounded to 128 MiB of conservatively weighted entries and 65,536 entries.
Eviction changes cost only. Inference continues through the original full
context validator. A valid in-memory historical derivation can be used without
reading the disposable disk cache; the original full-context path continues to
detect and rebuild corrupt disk entries.

This reusable projection is independent of model selection. Labels, eligible
training rows, history limits, support gates, assessment reservations, recipe
selection and the final prepared snapshot retain their own complete checks.
Changing a recipe cannot install another PC's settings through this cache.

## Trading and recovery controls

The existing initial manual/existing-task activation remains the only way to
start an authorized trading run. Data readiness, a scheduler wake, a forecast,
model completion and planned model replacement do not activate trading or clear
a stop. A stop is run-specific and durable; observed revocation is permanent
for that process role.

The controller preserves the original logical run, selected calendar day,
authorization, wake and deadline. Each role is fenced by controller/process
identity, generation, token and exact loaded release. Windows process ownership
uses the existing suspended-start and kill-on-close Job Object mechanisms.
Controller loss revokes order authority before bounded cleanup. A terminal role
failure records its actual error and does not create an automatic crash retry.

Scout's order journals, pending broker IDs, fills, reservations and uncertain
send records remain in place. An UNKNOWN send never becomes an unsent retry
because a model worker rotated or new data arrived. Historical journal audit
reuse is valid only while the independently checked journal witness remains
unchanged; changed or corrupt data invalidates that proof and receives the
original full audit. No historical record is removed to make recovery faster.

The existing unrounded probability interpretation, sizing policy, fresh quote
requirements, own-account capacity checks and final submission gates remain in
force. Delayed forecasts preserve their fixed targets and are classified as
expired when appropriate. No older fitted model substitutes for an unavailable
current result.

## Installation and activation boundary

`tools/scout_service_install.py` composes a complete immutable release beside the
selected predecessor. Its reviewed operation list includes explicit
add/modify/delete intent, installed before hashes and final source hashes. All
unowned predecessor source and the exact private recipe bytes are retained.
Runtime overlays are composed by reviewed hunk or field; adopting shared source
does not adopt another PC's recipe.

The installer creates a distinct independent-services selector/bootstrap. It
rejects replacing the legacy selector with an incompatible protocol. The new
selector is committed atomically only after source review, passing offline
checks and release validation. Prior releases, selectors, journals and local
receipts remain available.

Local launch/task bindings may be prepared under existing installation authority.
Binding changes affect the next normal authorized initial launch. They do not
restart a current trader or attach this topology to an existing run. The data
service's supported entrypoint is `ml.intraday.scout_services --data-live`; its
separate saved task/binding retains the established data authority and does not
include a trading activation flag. The controller bootstrap's `--live` starts
the authorized 1B run, `--check` performs offline release/recipe/calendar checks,
and `--stop` writes the existing run's durable stop latch.

The controller resolves the independent selector before each planned model
replacement. That successor validates its exact manifest before application
imports and resumes the original run/checkpoint. The trading process remains
pinned to its initial release for that run; a trading-process or controller
topology change uses the next normal authorized launch.

Installation, configured task bindings, actual loaded manifests and observed
runtime behavior are separate facts. An installed selector or passing fake
broker test is not evidence that production data, models or trading processes
have loaded or exercised the new architecture.

## Offline verification and measurements

Use fixtures, fake process/broker adapters and prerecorded model results. Tests
must not call market providers/brokers, fit production models, start/stop a live
trader or place/cancel real orders for infrastructure verification. Source
publication uses the pinned installed completion queue and reviewed isolated
candidate procedure. Private recipes, matrices, fitted models, raw observations,
account state and ledgers remain local.

Record preparation, acquisition readiness/queue waiting, model work,
forecast-to-order delay, database contention and deadline misses independently.
Phase measurements can overlap; their medians must not be added. Live metrics
establish behavior only after a process supplies its actual loaded manifest and
relevant observed evidence.

The preparation fixture benchmark compares warm durable JSON context lookup and
matrix assembly with verified packed row reuse. It uses 4,096 sparse historical
rows and 4,864 features, with no provider, broker or fitted model. Three samples
produced:

| Measured subphase | Median seconds |
| --- | ---: |
| Existing verified durable JSON lookup and matrix assembly | 6.06985 |
| Verified process-local row reuse and matrix assembly | 0.34573 |

Every fixture matrix cell and original context hash matched. This is a measured
improvement for that sparse context subphase only. It does not establish total
preparation throughput, production model latency, live forecast-to-order delay,
database wait attribution or the number of future deadline misses. The
reproducible fixture is `benchmark_rows` in
`tests/test_intraday_context_verified_reuse.py`.
