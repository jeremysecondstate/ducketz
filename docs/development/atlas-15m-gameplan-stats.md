# Atlas 15-Minute results in Gameplan Stats

This read-only section adapts Scout's reviewed implementation at
`f3414df843add9c35cd2fa18f4ec7ca6c222d7ad`, PR #385, Completion-Record
`20261024T184800Z-50183328cc3d443cabeeb252a515100b`. Atlas and Scout results
remain separate from each other and from Gameplan's 1h, 4h, daily and weekly
totals. The common panel retains Scout's reader and behavior.

## Predictions

Atlas reads its own live forecasts from `intraday/shadow.sqlite3`, using the
Pacific date of the saved reference timestamp. Session discovery reads indexed
headers; it may expose a historical replay-only date, whose live population is
empty. Only the selected session's scalar forecast fields are projected. No
store constructor, model fitting, acquisition or execution service is imported.

The installed target is
`intraday-15m-open-to-third-close-not-down-v1`: the third target candle closes
45 minutes after the reference open; the target candle opens at reference +30
minutes. The saved probability estimates whether that close is at least the
reference candle's opening price. Successful model probabilities form the
original equal-weight consensus. This display does not change that consensus,
the saved timestamps, model selection, training recipe or original records.

Accuracy compares the saved probability at 50% against the observed binary
label. Brier is mean squared probability error, with zero perfect. Both use
only forecasts issued strictly before target open with observed, scorable
latest outcomes. They are independent of BUY/SELL/HOLD execution decisions.
Pending, missing, unscorable, synthetic, during-target, after-close,
unknown-timing, invalid-probability and incompatible-target populations are
shown outside that denominator. A damaged latest outcome never falls back to
an older successful score. Source revisions remain untouched.

## Orders and fills

Atlas's installed executor writes `intraday/execution/holdings.sqlite3`.
Preserved historical Atlas attempts also exist in the mixed
`state/independent-stock-trader/holdings.sqlite3`. These paths are relative to
the local datastore. The reader opens SQLite in `mode=ro` with `query_only`
and a read transaction, without schema creation, recovery or backfill.

Attribution requires the saved `atlas-15m` request, reservation and allocation
ownership plus matching reservation forecast/hash/snapshot and target links.
A 15-minute horizon or matching symbol alone does not establish ownership.
An allocation's original opening forecast can differ from a later order's
forecast. Account identifiers and raw requests stay out of the UI projection.

Each reservation is one attempt. The independent ledger takes precedence over
a duplicate historical reservation; conflicting copies produce an incomplete
coverage warning. Account-scoped broker identities also prevent duplicate
orders under different request IDs; conflicting fill identities remain unknown.
Latest cumulative order evidence and unique identified fills
are checked together, without adding repeated cumulative observations.
Saved acceptance, fill quantities, fill prices and times determine the displayed
result. Proven pre-acceptance rejections are not sent; a reserved attempt without
submission evidence remains unknown. Submission times are read from existing
records, never backfilled. Realized P&L is unavailable because verified
purchase-cost matching and fee attribution are outside this summary.

## Availability, UI and installation

Each source reports missing, unreadable or partial coverage independently.
Unavailable evidence displays dashes; a readable empty population can show zero.
Atlas-only sessions remain selectable without a Gameplan review. Changing
sessions clears prior results immediately, and obsolete background replies are
discarded. Prediction and order panels stack at narrow widths, with horizontally
scrollable company/order tables.

Offline fixtures cover attribution, foreign-stream exclusion, revisions, timing,
duplicate fills, malformed and partial sources, session selection and isolation
from Gameplan totals. `tests/visual_atlas_stats_fixture.py` renders synthetic
normal and narrow previews without opening operating sources or application
services. Installation changes source only; the application loads it on its next
launch. This integration does not restart any application or trader, call a
broker/provider, train a model, place orders or introduce cross-trader gates.
