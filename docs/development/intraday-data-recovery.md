# Native market-data recovery and evidence

The optional local population `intraday-multi-provider-recovery-v1` preserves
the primary EQUS.MINI observations and can recover an empty quarter using
Schwab native one-minute bars, then FMP native five-minute bars. It never
expands a five-minute bar into invented minutes. Each candle has a common
multi-provider population identity and retains its original native source,
interval, rows, receipt clocks and transformation evidence.

The order of operations is genuine observation, corroborated estimate, then
unavailable. `cross-provider-probable-no-trade-v1` requires three distinct,
successful responses covering the same completed interval, no observed
activity, and a recent genuine anchor no older than 15 minutes. Available
fresh bracketing prices must agree. Missing brackets do not introduce an
after-bar wait. Failed, late, incomplete, malformed or conflicting responses
cannot vote. Provider overlap is explicit: this is a probable absence, never
a verified market-wide no-trade fact or a calculated independence probability.
The derived candle has `synthetic=true`, `no_trade=false`, zero estimated
volume and the complete replayable assessment. It cannot seed another carry
as a genuine anchor. Old unproved synthetic data is not promoted.

The existing synthetic inclusion setting remains frozen with every forecast
and fitted artifact. Actual recoveries and estimates use the new population;
old single-source forecasts cannot be scored with it. Synthetic outcomes
retain their separate status. No model support or qualification threshold is
lowered to accept a result.

`GapRecovery` saves original price responses before parsing and records
request and receipt times. `save_recovery_candle` appends only non-primary
results to a small indexed catalog under `intraday/gap-recovery`. Original
canonical files remain unchanged. Historical loading selects only revisions
available at the cutoff; primary observed quarters take precedence. Integrity
and equal-clock conflicts are explicit errors, not silent revision choices.

## Acquisition owner and retries

`run_market_input_worker` is called by the existing manually started 15-minute
trader. It collects each new quarter and resumes pending transient requests
between quarters, independently of fitting. It is not a scheduler and never
starts or restarts a trader. Nightly acquisition and preparation keep their
own ordinary workflow; this module adds no daytime Gameplan collector, peer
dependency or nightly prerequisite.

`RetryLedger` durably retains pending requests, uses a minimum two-second
retry delay, honors Retry-After and manual stops, and does not abandon transient
work when one bounded pass ends. Invalid authentication or request syntax is
recorded distinctly. A Schwab `protocol.http.TooBigBody` response selects
smaller expiration partitions instead of repeatedly sending the same oversized
chain. The saved expiration list and successful partition receipts establish
the requested coverage; native contract keys are deduplicated without adding
their volumes together. All returned raw data stays private.

The full Schwab quote response is saved before its per-symbol projection.
FMP transport retains sanitized HTTP status/cause and Retry-After. GDP uses a
current 90-day request and at most two preceding 90-day windows when empty,
stopping at the first genuine value. News/grades retain their configured event
window: an older returned article does not become a current event.

## Prospective provenance and cleanup

Each market receipt points to frozen loaded Python code and an allowlisted
acquisition-rules object. Every new forecast captures loaded transformation
and recovery modules after acquisition and feature construction. Databento
minute attempts retain the SDK-native DataFrame before OHLCV projection, plus
request/arrival clocks and an indexed immutable producer sidecar. This is not
a claim to preserve original network wire bytes or reconstruct old arrival
times. Historical artifacts remain explicit retrospective reconstructions.

Old Databento SDK versions create an unused event loop at import. The shared
client factories register an exit handler that closes only a never-started,
non-running loop. It never closes a loop used by an active live transport.

## Installation and tests

Local adapters load the explicit data-policy binding; no shared example selects
another PC's symbols or private artifact. The manual owner starts/closes one
collector and calls `run_market_input_worker`; it passes its existing stop flag
and event sink. Divergent local execution entrypoints require an owned hook
review, not replacement with another checkout's trading code. No runtime or
peer acceptance stage is introduced.

Run the offline suites for `market_completion`, `provider_recovery`,
`historical_market_inputs`, `recovered_population`, `market_gap_evidence`,
`gap_recovery`, `databento_idle_cleanup`, and the existing data/runtime/store/
supervisor suites. They use temporary stores and fake provider responses;
actual acquisition receipts and fitted models are private local evidence.
