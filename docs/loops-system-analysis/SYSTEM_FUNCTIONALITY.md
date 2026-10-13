# System functionality

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

This is a capability guide. [Current system](CURRENT_SYSTEM.md) separates the
October 11 Atlas observations from portable contracts and historical operation.

## Data and prediction authority

Loop A owns bounded acquisition and readiness, preserving EQUS operational bars,
XNAS archive/target history, Schwab evidence, OPRA partitions, CME and ALFRED as
distinct source families. Similar timestamps do not justify a cross-provider
merge. OPRA's production definition, minute-CBBO and hourly-bar schemas retain
separate partitions, receipts and exclusive completion cursors. Required inputs
depend on the saved run scope; a stock-only run omits optional Strategy stages.

Loop B materializes causal features, samples and compatible directional evidence.
The nightly route models are separate target groups. The immutable stock grid
retains thirteen forward hourly rows plus one non-entry opening-gap row, four
four-hour, five daily and one direct weekly row per saved symbol: 24 rows per symbol, independent of today's watchlist. Weekly output
is not the average of daily probabilities. Calibration, assessment, publication
and sizing qualification have distinct saved results.

The options Strategy path models exact candidate outcomes, with contract/leg
identity and compatible quote evidence. Historical CBBO and explicitly labeled
fallback economics are planning/training evidence, not executable live quotes.
Stock-only placeholder intents must not be reported as promoted options models.

## Publication and consumers

Stats-first preparation scores prior original forecasts before bounded model
review and training. It preserves missing-history status, pending horizons and
the original source of each score. Atlas may synthesize the two Gameplans once
and publish the same combined result on both PCs without waiting for acceptance.

Gameplan, trade-plan, Stats, model-review and joint-readiness outputs each have
their own identity and verification. A single latest Gameplan pointer is not
the sole authority for the whole current system. Readers verify their selected
publication and its provenance; historical display remains a supported use.

The advisory/paper reader is broker-free. Stock execution uses current broker
state and quotes, exact source/ownership checks, reservations and human controls.
Independent horizon ownership replaces the old hourly/four-hour veto model.
Options order authority is not implied by a frozen intent or shared plan.

## Useful detailed contracts

- [Nightly Gameplan](NIGHTLY_GAMEPLAN.md): immutable forecast, review and recovery boundaries.
- [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md): which components contribute data, models and publications.
- [Joint capital](JOINT_ACCOUNT_CAPITAL.md): one account projection, original source identities and execution separation.
- [Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md): horizon policies, ownership and catch-up.
- [Loop C risk](LOOP_C_RISK_CALCULUS.md): independent options observer and gated research-proposal sizing.
- [Datastore hygiene](DATASTORE_HYGIENE.md): retained lineage and dated cleanup evidence.

## Historical September publication

The September 4 first-publication timings, assessment results, all-NO_TRADE
options outcome and nine-second publication-boundary miss remain in the
[original audit](audits/2026-09-04/AUDIT.md) and
[supervision update](audits/2026-09-04/SUPERVISION_UPDATE.md). Those events were
not rerun or re-dated by this review. Older versions of this guide's monolithic
schedule and stock-veto narrative are recoverable through the review's immutable
source reference.
