# Loop relationships

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

## Current sequence

```text
completed-session data / exact provider readiness
  -> Stats from original saved forecasts and actuals
  -> bounded model review
  -> local target fitting, Gameplan, enrichment and planning inputs
  -> two private Gameplan research packages
  -> one combined 22-symbol Gameplan + Stats
  -> same completed result / both PCs' readers
  -> separate manually controlled Atlas stock execution
```

This shows responsibility dependencies. Individual saved runs retain their own
stage list and source revision; [Current system](CURRENT_SYSTEM.md) records the
installed-source and task evidence limits.

## R1 - providers and prior storage to Loop A

Acquire only the configured scope through its provider, cost, lock and receipt
contracts. Preserve provider/dataset identity and exclusive coverage cursors.
Closed-session currentness is not the same clock as a live execution quote.

## R2 - Loop A to Loop B and target history

Loop B consumes causally available feature families and publishes compatible
samples/directional evidence. Target-history maintenance supplies explicit
stock-label provenance. Missing or incompatible required evidence must remain
visible; an old partial generation cannot be silently renamed current.

## R3 - optional options models and Strategy

Explicit options scopes use compatible Loop B context, OPRA contract identity
and quote/surface history. Strategy-profit models and candidate generation are
separate from stock-direction training. Production cursor count scales with
the saved universe, not a hard-coded historical count. Stock-only scopes omit
these optional stages without pretending options promotion succeeded.

## R4 - Stats to model review and immutable Gameplan

Score matured original forecasts before reviewing candidate model specifications.
Train/select/calibrate/assess with separate chronological cohorts. Preserve
the original route identity and target window, including research-only or
unavailable status. Normal pre-opening publication and explicitly dated late
recovery have different evidence; neither may backdate availability.

## R5 - local publications to combined planning

Both owners supply their Gameplan packages. Atlas can synthesize once against one actual cash/stock snapshot and return the same 22-symbol plan and Stats to both PCs without a reciprocal acceptance wait. Conditional planning sales do not create
live buying power. An incomplete peer package is not a complete joint plan.

## R6 - publications to daytime consumers

The UI and advisory/paper reader consume verified artifacts without placing
orders. The separate stock adapter requires current source, role, ownership,
quotes, account state and controls. Four stock horizons own separate inventory;
the old hourly/four-hour confirmation veto is historical. Current cumulative
catch-up nets due quantities against actual fills and commitments rather than
replaying historical order instructions. See [independent horizons](INDEPENDENT_STOCK_HORIZONS.md).

Loop C's observer/risk contract is independent. Neither a UI view nor an observer
receipt is a live execution receipt. Options intents require separate future
authorization and exact-leg execution revalidation.

## R7 - observed outcomes to future Stats

Revisit each saved forecast when its exact target matures; preserve pending and
missing-data outcomes. Combined Stats uses original-source counts and metrics.
Directional correctness and hypothetical planning prices are not realized P/L.
Option P/L requires exact-leg execution evidence; unexecuted studies must say
counterfactual.

## Evidence-family boundaries

EQUS operational bars, XNAS archive/target provenance and Schwab history remain
distinct. OPRA native bytes, normalized tables, manifests and receipts describe
one lineage. Historical quote evidence is not a current tradable quote. Legacy
minute/hour phase relationships in the per-loop reports are not present task
authority on either machine.
