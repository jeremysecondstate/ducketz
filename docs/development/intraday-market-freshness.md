# Market state, event age and collection coverage

Context version `intraday-causal-market-context-v3` is an explicit local recipe
choice. Existing v1 and v2 recipes keep their original feature names and rules.
Source adoption alone does not migrate another PC's model or population.

An instrument's last trade, last quote change, confirmed current quote, and
connection health are separate facts. The `intraday-field-market-state-v1`
record preserves native event clocks and records actual field confirmation or
supported connection continuity separately. A current authoritative snapshot
can confirm an unchanged value. It does not create a new trade, volume increment,
candle, or historical publication date.

Reducers admit quote, option and cumulative state fields using their own
confirmation evidence. They retain last-trade values with their age and expose
quote-change age, confirmation age, continuity age and relevant session state.
Historical bars and trade events retain event-window and completion checks.
An empty response, acknowledgment or heartbeat without an instrument baseline
does not establish that instrument's state. Missing state remains missing.

Stream state requires a genuine baseline and merges supported updates within
one connection. Connection-local record numbering is identity/provenance, never
an exchange sequence or proof of gap-free exchange delivery. Immutable native
messages and state checkpoints preserve the baseline, update and disconnect
boundaries. A reconnect starts with no baseline; persisted disconnected state
is audit evidence and cannot silently resume continuity.

The bounded collector keeps its existing subscription limits and minimum
observation window. When book baselines are missing, it allows an additional
bounded opportunity up to thirty seconds within its overall request deadline.
It does not claim full-chain continuous coverage between captures. Closed-market
book silence is reported as missing baseline, with the session status, rather
than proof of an inactive instrument or provider failure.

Coverage reports distinguish requested, submitted, acknowledged, observed,
confirmed-current, persisted and model-eligible populations. Context eligibility
is not model support: optional-feature selection and preprocessing still fit
only on the base-training population. Newly collected context cannot activate a
feature merely because it occurs in calibration or inference. Publication-aware
fundamentals and news retain their separate as-of restrictions; a current
snapshot is never pasted onto older price candles.

Non-trading preparation binds its source and timing contracts to durable
training identities. Exact repeated inputs reuse checksum-verified artifacts.
Changed observations, labels, recipes or source contracts trigger appropriate
new preparation. A corrupt symbol projection remains isolated and retains its
evidence while healthy symbols continue. Source/recipe installation must update
the preparation worker's reviewed bindings and verify its next actual cycle;
this does not start or authorize a trader.
