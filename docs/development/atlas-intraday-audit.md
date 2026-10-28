# Atlas independent 15-minute architecture audit

This follow-up repairs the installed Atlas split without adopting Scout's model
selection, protocol, symbol bindings or lifecycle authority. The original split
and its exact consumer adapters remain documented in `atlas-intraday-split.md`.

The existing data command selects a sealed generation and enters the independent
System 1A service. Its own lock, collection database, acquisition journal,
preparation store and immutable ready objects do not depend on a trader stop,
consumer acknowledgment, Gameplan or Scout. Each ready object binds original
source, recipe, ordered features, availability, cutoff and fixed target. A short
SQLite transaction publishes only its pointer after complete objects exist.
Each System 1B symbol lane verifies its own assigned snapshot, runs the saved
science and immediately mirrors the completed compact forecast. Execution and
outcome scoring run in separate processes inside the original session lifetime.

## Repairs

- Whole-scope 1A preparation previously waited for its slowest symbol before
  admitting the next quarter. Bounded symbol lanes now advance independently,
  with no queued executor backlog and no more simultaneous work than Atlas's
  existing worker allocation. Each lane retains its own incoming history and
  preparation store. All lanes use the original full-scope frozen policy;
  admitted work keeps its original reference and target through final catch-up.
- A damaged queued forecast previously escaped before the execution sweep's
  error handler. Validation now advances one record at a time. A malformed,
  missing or mismatched original receives an atomic durable rejection and queue
  disposition, allowing unrelated work and own-order recovery to continue.
  Original evidence and all order state remain intact. Quarantined records keep
  their supersession position; an unreadable same-symbol reference cannot revive
  an older opportunity. Due and same-symbol lookup indexes avoid repeated queue
  scans. Only actual SQLite BUSY/LOCKED conditions receive bounded continuation.
- Scoring previously contained contract errors but allowed malformed saved JSON
  to terminate the worker. Malformed evidence now stays isolated to its symbol;
  valid symbols still score. Target-store BUSY/LOCKED retries retain completed
  work and the original day and close. Other storage errors remain errors.
- Empty model lanes previously renewed after each one-second no-work poll.
  A bounded 15-second idle admission pass keeps one-second ready checks and
  frequent stop checks in the same interpreter. Completed scientific work still
  checkpoints immediately under Atlas's existing renewal protocol. No target,
  training cadence or initial activation is moved.
- Every assigned model scope now retains append-only forecast, failure or
  deferral accounting. Unchanged polls do not append duplicate events. The union
  of the fixed symbol lanes covers the original full session scope.
- Snapshot admission no longer serializes the full matrix for an extra digest
  after the handoff verified its bytes. The runner retains its canonical hash
  validation. Model timing separates index lookup, snapshot validation, admission
  CPU/wall time, queue residence, model work and original deadline misses.
- Successful worker startup now emits a diagnostic loaded-source receipt bound
  to the actual bootstrap result, source/runtime manifests, interpreter, worker
  and dispatch identity. Later selector changes cannot relabel that evidence.
  Atomic no-replace publication preserves earlier receipts; diagnostic failure
  does not grant or remove session authority.

## Verification and measured scope

Fixtures use temporary stores, prerecorded model outputs and fake brokers.
They cover independent acquisition, per-symbol publication, full-scope
accounting, slow training and responsive stop, immutable publication/deduplication,
causal clocks, corruption and replacement, stale work, contention continuation,
original sizing/directions, and working/partial/UNKNOWN one-send evidence.
Private complete-generation checks also verify source/import closure and the
unchanged pinned dependency runtime. Public package tests explicitly distinguish
portable behavior from integrations requiring Atlas's installed adapters.

The deterministic 60-second idle fixture reduced worker dispatches from 61 to
4, while retaining 60 ready polls in the repaired path. The original path made
61 polls including its initial poll. This measures scheduling behavior, not live
startup latency. A five-sample warm synthetic 50,000-row, 24-feature snapshot
fixture measured the validation/digest component at median 0.510 seconds before
and 0.251 seconds after removing the duplicate serialization; median process CPU
was 0.516 and 0.250 seconds. Disk, training and execution were excluded.

Independent two-symbol scoring fixtures measured 43.21 ms cold and 13.85 ms warm
median. Existing compact sizing measured 2.17 ms against 18.48 ms for full sizing
on identical fixture inputs, with equal features and decisions. These component
measurements establish neither a production speedup nor a live deadline rate.

## Installation and remaining limits

Publish only the owned direct modules/tests and exact consumer hunks in
`patches/atlas-intraday-audit.patch` with its manifest. Different consumer
preimages require reviewed composition. Never replace an entire local consumer
with a peer's file. Retain the original split package as historical evidence.
Seal the complete reviewed source using `tools/trader_installation.py`; preserve
private recipe/configuration bytes and native task definitions. No immutable
generation is edited in place and no trader/provider is operated to verify it.

The supported boundary is the next normal locally authorized launch, or an
existing Atlas session's supported safe renewal where compatible. A controller
must never be attached to a legacy process. Manual stops end the whole session.
Installation does not prove which generation a process loaded or whether its
providers, forecasts or orders succeeded.

Large immutable scientific inputs still require causal validation, decoding and
storage. Complete scientific and order evidence grows over time. Cold history
assembly and historical outcome validation remain material costs; this change
does not replace them with an unverified cache or delete evidence. Native task
readback, selected-generation hashes, historical loaded records and naturally
observed split behavior must be reported separately in the private completion
receipt. Historical combined-session timings are not split-service benchmarks.
