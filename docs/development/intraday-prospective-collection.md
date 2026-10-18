# Non-trading prospective collection

Prospective assessment loads designated fitted artifacts; independent
qualification is an assessment outcome, never a condition for collecting its
predictions. The collector has no trainer, policy installation, account or
order interface. Each host supplies its own private symbol scope, recipe,
artifact hashes, successful fit evidence, source bindings and protocol.

`ml.intraday.prospective_worker` runs separately from manual trader startup.
Its explicit exchange-calendar dates and quarter-hour decision window are
frozen before observations. An exact reference is required, and both inference
and durable publication must finish before target open. A missed window,
interrupted unconfirmed publication or unavailable input is retained rather
than backfilled. A separate append-only store preserves inputs, original
receipts, model identities, publication receipts and observed outcomes. A new
config/recipe/artifact population requires a separate experiment store.

Native acquisition covers the full exchange session, including premarket
history before the decision window. Bounded recovery records observed and
source-empty intervals separately. Unknown intervals in the feature window
prevent publication; confirmed source absence never manufactures a candle.
Current forecasts take priority over background history and outcome recovery.

The current price-supported artifact adapter verifies the fitted optional
feature projection. It cannot silently load a context-dependent model while
omitting its context. Optional context collection runs separately through
`ml.intraday.prospective_context` under the existing provider configuration and
exact denial blocks. Book subscription acknowledgments are not book baselines.
Confirmed unchanged state retains its original quote/trade clocks.

Private `intraday/<pc>/assessment-reservation.json` excludes overlapping target
horizons from continuing manual preparation, calibration and feature selection
before row caps or support decisions. The trainer rechecks this reservation,
and nightly supervised selection does not inspect reserved sessions. It does
not expire automatically after an assessment. Future inference can use causal
new observations without fitting to their outcomes. Historical research remains
permitted on authentic records available at the actual fit cutoff; missing
original acquisition clocks still limit exact historical replay claims.

Installations must retain original task definitions and durable state, verify
exact dependency bytes, record task and process identities, and test restart,
duplicate exclusion, missed predictions and publication boundaries offline.
Closed-market execution verifies scheduling and persistence only. Live timely
collection and independent performance require actual future observations.

Predeclare the first assessment session, support, per-symbol aggregation,
baseline definitions, qualification gates, outcome recovery allowance and
stopping rules. Preserve failed and insufficient results. Additional collected
sessions do not authorize repeated testing until qualification passes. Keep
manual ensembles separate from supervised policies and preserve all original
protocol versions. Source publication, local installation, worker adoption and
trader adoption are distinct facts; installing these workers never authorizes
trader adoption or clearing a manual stop.
