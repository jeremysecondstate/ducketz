# Covered native observation intervals

The default observed-only recipe requires all fifteen one-minute constituents
to be observed before accepting a fifteen-minute candle. This can exclude
otherwise usable native source observations for sparsely represented symbols.
The optional `intraday-covered-native-interval-v1` contract instead aggregates
the genuine observations inside an exact quarter hour. It is a separate local
data-population choice, not a change to the default meaning of observed-only.

## Source and price evidence

Only the exact configured EQUS.MINI native one-minute source is supported.
Each admitted quarter must have successful request coverage for the entire
interval. Its open is the first observed minute's open, its close is the last
observed minute's close, its high and low are the actual observed extrema, and
its volume is the sum of actual observations. Empty minutes contribute no
price or volume. A wholly empty quarter has no candle and remains unavailable.
An omitted source row does not establish that the stock did not trade elsewhere.

Every candle retains the original minute observations, their count, the count
of source-empty minutes, exact interval and source identities, and coverage
evidence. No prior price, carry-forward value, shifted reference or synthetic
constituent is admitted. Validation recomputes OHLCV from the saved observations.
Removing a contract tag does not make its sparse evidence a valid legacy candle.

Live sparse responses must repeat consistently across consecutive qualifying
attempts after the existing minimum wait. The saved candle includes both
responses' timing and coverage evidence so later validation can enforce the
same rule. A failed request or changed snapshot cannot provide a usable candle.
The candle becomes available at the qualifying response's actual completion.

Historical loading verifies canonical minute values against their native raw
counterparts and source metadata, records exact file hashes, and rejects
conflicting covered snapshots rather than unioning incompatible observations.
Canonical historical files can already merge successful fetches; their range
metadata proves coverage rather than reconstructing the original responses.
Historical receipt times are not known: availability at quarter close remains
an explicit assumption in the manifest. Offline results are conditional on
that assumption and do not establish historical live availability.

## Frozen local selection

A version 3 manual recipe explicitly maps selected local symbols to the new
feature contract. It retains the original recipe identity as migration evidence.
The default feature contract, all unselected symbols, resolved estimators,
preprocessing, calibration settings, training limits and support floors remain
unchanged. Loading shared source never migrates or rewrites a private recipe.

Manual startup validates the local recipe and freezes its selection for the
session. The minute feed persists the selected interval scope in its identity,
uses compatible history, and rejects a changed selection when restoring the
same feed. Model inputs, inference, forecasts and scoring retain the selected
contract. A native interval cannot retroactively supply the outcome of a legacy
forecast, and a legacy candle cannot score a native-interval forecast.

Assess a new local population before installation, preserving chronological
training, purged calibration and the latest completed held-out session. Keep
failed calibration, class-balance or performance gates in the saved assessment.
Do not retune against the held-out session or lower gates to declare readiness.
An explicitly authorized manual experiment and a qualified supervised policy
remain distinct, as described in [model ownership](intraday-model-ownership.md).

Recipes, raw observations, fit artifacts and assessments stay private. Publish
only shared correctness machinery, documentation and offline tests. Installing
a local recipe does not start a trader or change the operator's manual controls.
