# Atlas independent 15-minute 1A/1B services

## Atlas scheduled-start direction, October 27, 2026

Jeremy explicitly authorized both Atlas traders to start automatically at the
existing weekday morning wake and required three start commands and three
separate Windows tasks. This supersedes this document's earlier manual-only
initial-start wording for that authorized scheduled wake. It does not change
Scout's operating authority or add a reboot, retry or post-stop restart.

The three entry commands are `Start-15m-Data.cmd` for data-only System 1A,
`Start-15m-Trader.cmd` for System 1B, and `Start-Gameplan-Trader.cmd` for the
independent combined Gameplan trader. Each owns a separate Windows task lifetime;
do not put the long-running data action and trading action in one task or make
one trader depend on another. The installed native data task supplies its own
reviewed wake time and weekdays using `--daily-data-wake` and `--data-weekdays`.
The data command uses the same conventional local datastore default as the
existing launchers; an explicit `--datastore` argument overrides it.

The existing trader launchers enter their normal installed-generation host.
Their historical `manual-` session prefixes and protocol field names remain
unchanged; a scheduled launch is not evidence of a contemporaneous human click.
Local task definitions and authorization receipts record the actual scheduled
source. Each daily authorized wake admits a fresh session. A manual stop still
ends that entire session; generation renewal cannot clear it. No scheduler
retry or additional wake may resume it. Existing session deadlines, models,
state, pending orders and UNKNOWN fences remain unchanged. Registration and
installation never invoke these commands to demonstrate live operation.

The sections below describe the original split installation. Read their initial
manual-start references subject to this later Atlas-only direction.

Atlas's independent 15-minute trader uses two independent lifetimes. System 1A
owns data collection and causal input preparation. System 1B owns model work and
the already manually admitted trading session. The combined Gameplan trader is
unchanged. Neither service consults Scout availability or Gameplan state.

## Processes and storage

`tools/intraday_data_host.py` selects one complete locally installed generation,
starts its sealed Python with isolated imports, verifies the complete source and
dependency inventory, and enters `ml.intraday.split_service`. It has no trading
activation or manual-session token. The existing weekday Windows data wake is
wired to this data-only entry at installation, preserving its identity, triggers,
enabled state and other settings. Installation never invokes that task.

System 1A owns `locks/intraday-system-1a.lock` under the local datastore. Its
independent status, collection/acquisition journals, preparation store and ready
handoff live under `intraday/system-1a`. Candle collection, optional market-input
collection and preparation continue without a 1B acknowledgment. The data service
does not read or clear the trader's manual stop. Actual collection/preparation
errors retain recovery events and do not create trading authority.
Live preparation follows the original 17:00 Pacific close. The data service
finishes its actual retained candle catch-up before normal daily exit; a target
clock alone does not discard unfinished collection. If catch-up spans the next
configured weekday data wake, the already owned data process admits that day's
valid preparation while retaining fair progress on older collection. The local
task action supplies the exact existing wake time and weekdays explicitly;
shared code invents no schedule. A new process selects a complete installed
generation. This does not establish continuous weekend acquisition or loading
at every scheduler wake.

Jeremy starts System 1B using the existing `Start-15m-Trader.cmd`. The supported
Atlas manual-session host and renewal protocol remain in control. The session
runs separate model, scoring and lightweight execution/recovery processes.
System 1A is outside that session's Job Object. A manual stop ends the whole 1B
session without ending System 1A. Installation neither restarts an old session
nor changes the implementation already loaded by it. The next normal manual
start selects the new architecture; an existing old session keeps its original
lanes and order state until its existing lifetime ends.

Model matrices and full diagnostics stay in a separate model database. Completed
forecasts are mirrored immediately into the existing execution store, with
compact immutable execution derivatives. Full indicator observations and model
evidence remain retained by content hash. The broker process uses original
probabilities, the original target and exact precomputed indicators, then obtains
current quotes, cash, positions and order responses through its own existing
broker adapter. It does not reconstruct training history or load training
matrices in the send path. Existing own-order ledgers, reservations, fills,
pending identities and UNKNOWN-send fences remain authoritative.

## Durable handoff and admission

`ReadySnapshots` writes canonical content-addressed snapshot, cycle and ready
records before publishing a short SQLite pointer. Each object is flushed before
an atomic no-replace publication. A first-publication claim preserves the
original cycle/symbol input identity through retries, index loss and crashes.
Partial files cannot become ready work. SQLite index transactions contain only
small pointer operations, with a bounded connection timeout; they do not cover
preparation, serialization or training.

Every handoff binds the actor, PC, symbol universe, producer session, quarter
reference, actual information availability, training cutoff, original target
open/close, configuration, source/data scope, ordered feature schema and recipe.
The consumer verifies original object bytes and all these identities. It rejects
incomplete, corrupt, mismatched or noncausal inputs. The latest live pointer is
bounded to one record per owned symbol; old immutable scientific evidence is
retained. Expired or superseded references are excluded from live model work.
Consumer completion does not gate the producer.

Model admission uses only verified inputs for the current original reference.
Unrelated valid symbols can proceed when another acquisition is unavailable.
The manual host creates bounded independent symbol lanes up to Atlas's existing
explicit local worker allocation, each with separate model storage. A late ready
symbol can enter its lane while another lane is still fitting. The fair symbol
cursor is preserved and each completed forecast publishes immediately. Slow fitting and historical
scoring do not own the execution/recovery loop or its manual-stop checks.
No old model or expired forecast is promoted into a new trading opportunity.

## Scientific and operating preservation

The split retains Atlas's resolved local model selections, parameters,
preprocessing, calibration, feature order, training history/population rules,
synthetic/missing-data policy, validation gates and frozen session decisions.
See `intraday-model-ownership.md`. The producer carries the exact selected
recipe into the handoff; the consumer does not transplant Scout tuning or
silently migrate an existing recipe. Original target labels and probability
interpretation remain intact. The permanent stock direction thresholds and
current-capacity sizing remain unchanged.

The historical-context repair reuses a verified immutable reconstruction within
one process. Every reuse still hashes the complete original artifact bytes;
same-size changes, replaced files, changed contracts and corruption invalidate
reuse. Shared lookup structures cannot be mutated by a caller. Existing journal
audit and immutable order-evidence checks retain their corruption and one-send
semantics; splitting processes does not turn a cached audit or uncertain send
into permission to submit again.

## Atlas measurements and limits

These are Atlas measurements from saved local outputs and read-only offline
replays. No provider/broker request or production model fit was used.

| Measurement | Observed result | Interpretation |
| --- | --- | --- |
| Three existing eleven-symbol cycle wall times | 508.24, 727.86 and 920.66 seconds | Baseline; concurrent symbol times cannot be summed. |
| Saved per-symbol preprocessing | 7.80–259.76 seconds across 33 results | Combines frozen lookup, preparation, provenance and serialization; acquisition is not separately timed in those originals. |
| Saved model training / inference | 6.54–67.23 / 0.017–0.877 seconds | Baseline; existing Atlas allocation was already eleven symbol workers. |
| Historical context, 5,474,194 bytes / 3,394 rows | Original constructor 0.2030 seconds; repaired cold 0.2426; verified warm reuse 0.00359 | Warm reconstruction reuse is proven; it cannot explain the full preprocessing delay. |
| Eleven concurrent warm context readers | 0.01149 seconds wall | 103 sampled joins retain exact original results. |
| One saved 160,969,993-byte snapshot | Read 0.1190; decode 0.4330; canonical serialization 0.4605; hash 0.0631 seconds | Warm local-cache observation, not a general live throughput claim. |
| Immutable handoff publication of that snapshot | 0.6956 seconds total; 0.0030 seconds index operation | Additional durable handoff cost is measured explicitly. |
| Two retained-input sizing replays with fake current quotes/equity | Full path 3.271 / 12.113 seconds; compact path 0.0032–0.0043 seconds | Exact sizing decisions and terms match. One-time model-side preparation costs 5.404 / 14.211 seconds and remains outside execution. |
| Same two forecast records | 93,829,684 → 151,367 bytes; 72,268,785 → 75,398 bytes | Full original diagnostics remain immutable evidence. |

System 1A records preparation wall time and deadline misses. Ready publication
records validation, serialization/publication, index time and snapshot bytes.
System 1B records model-pass time, queue wait by symbol and original deadline
misses. Existing execution diagnostics retain preparation/send-stage timing and
quote ages. Offline contention and responsiveness fixtures verify independence;
post-installation forecast-to-order improvement, sustained contention and live
deadline-miss rates require naturally observed operation and are not established
by installation or the component benchmarks above. Immutable evidence growth
remains a storage concern; bounded live pointers do not delete historical data.
The sizing comparison has one original-path measurement and three compact-path
measurements per case, with a warm OS cache and shared host contention. It proves
that those retained inputs leave much less work in execution; it does not prove
an end-to-end live cycle improvement or remove model-side preparation cost.

## Verification and installation evidence

The offline tests use temporary stores, fake brokers and prerecorded model
outputs. They cover independent progress, slow model work and manual stops,
atomic publication, original causal clocks and feature order, deduplication,
crash/partial recovery, stale work, bounded live queues, corruption/cache
invalidation, unchanged scientific/sizing decisions and existing order fences.

The public package publishes new modules/tests directly and carries only the
owned integration hunks for differing installed consumers in
`patches/atlas-intraday-split.patch`, with exact before/after hashes in its JSON
manifest. `tests/check_atlas_intraday_split_package.py` distinguishes full
reconstruction from a structural hunk projection. On a differing common base it
runs all modules through ordinary pytest collection. Narrow capability guards
skip unsupported adapter modules before unavailable imports; portable tests run
and the checker reports the integrations not run there.
Atlas separately runs the complete tests against a staged copy of its latest
complete installed source plus only reviewed owned overlays. The runtime and all
unchanged parent members remain pinned. These full local checks are required for
the installation claim.

Finish application adoption by sealing the complete reviewed generation through
`tools/trader_installation.py`, then verify the actual native data-only task
action without invoking it. Preserve private recipes, datasets, fitted models,
account state, ledgers, bindings and all prior generations. The completion
receipt records source/PR SHAs, the installed generation, exact checks and task
readback separately. Actual loaded manifests and naturally exercised behavior
remain separate facts from published source and local installation.
