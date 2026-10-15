# Gameplan Stats split workspace

The native Tkinter tab presents two independent views of a selected Pacific
session. Prediction accuracy uses the existing verified actuals review. Trade
execution uses a separate private saved summary of the Gameplan trader's own
orders. Refresh reads these saved reports; it does not contact a broker, fetch
prices, rescore predictions, or operate a trader.

## Prediction accuracy

The four cards show direction accuracy, bullish accuracy, bearish accuracy and
Brier score. Directional cards display correct/scored counts and the existing
percentage rounding. Bullish and bearish counts partition the scored direction
population. Neutral, pending and missing outcomes are excluded from direction
accuracy; evaluated neutral forecasts remain eligible for Brier. Lower Brier is
better. A dash indicates an undefined metric, not zero performance.

The horizon selector affects the summary and company scorecard. The prediction
grid continues to show the thirteen saved 1-hour execution windows. Its `?`
means awaiting market-price data and carries no order-status meaning. All saved
companies remain available through scrolling. Company selection also offers an
execution filter; execution figures explicitly label company or whole-session
scope.

## Saved execution evidence

`ml.gameplan_execution_report` reads only the dedicated
`state/gameplan-trader/holdings.sqlite3` ledger, using SQLite `mode=ro`,
`query_only` and one consistent read transaction. It never initializes a ledger.
Exact decision identities may link to saved execution-result files to explain
local pre-submission stops. Those files cannot contribute new orders. There is
no connection to either 15-minute trader or the Gameplan authority database.

The separate local summary is
`state/gameplan-execution-reports/YYYY-MM-DD/summary.json`. It records reader
provenance, snapshot content fingerprint, generation time, the last recorded
broker-observation time, coverage limitations and attributed order attempts.
These are private account-activity records, even when identifiers are hashed.
They are not included in prediction manifests, forecast handoffs or Git output
publication. Existing prediction export allowlists remain unchanged.

The Stats reporting workflow attempts this sidecar after prediction publication
has completed. Execution errors cannot invalidate prediction Stats or become a
nightly prerequisite. A summary can also be regenerated independently, offline:

```text
python -B -m ml.gameplan_execution_report --datastore-root DATASTORE --session YYYY-MM-DD
```

The UI only reads that summary. It never regenerates it or opens the database.
Missing, unreadable, incomplete and stale evidence have explicit panel states.
Unavailable totals display a dash. An empty source is not proof of no orders.

## Population and status definitions

Session attempts are selected by recorded submission time in the Pacific
calendar interval from midnight inclusive to the next midnight exclusive,
including daylight-saving changes. Outcomes are the latest saved evidence, so
later observations and fills can update an earlier session's attempts. This is
not an as-of-close reconstruction.

Earlier submissions with fills on the selected date or still-open orders are
available as carryover records, outside the selected session's attempt totals.
Rows without a reliable submission date remain explicitly unattributed.

Each submitted attempt contributes to exactly one status category: fully filled,
partially filled, working, cancelled, rejected or unknown. A cancelled order with
partial fills remains cancelled in the distribution, while retaining its actual
filled shares and fill details. Submission evidence alone does not confirm a
fill. A locally stopped request is shown separately as not submitted when exact
evidence establishes that distinction; ambiguity remains unknown.

Retries/replacements are additional attempts. Their exact parent/child links
remain visible in order details. A forecast is not an order: catch-up can combine
several forecast IDs into one intention. The dashboard does not infer logical
trade counts from symbols, timestamps or quantities.

Order details show available fills, quantity-weighted actual average fill price,
recorded timestamps, retry relationships and recorded reasons. Missing fill
detail is identified explicitly. Open orders whose last saved observation is
over thirty minutes old are labelled stale. Viewing terminal historical
evidence on a later date does not by itself make it stale.

Filled measures execution completion, not profitability. This feature contains
no realized-profit calculation or combined success rate.

## Offline visual review

`tests/visual_gameplan_execution_fixture.py` imports the Stats tab directly with
theme constants. It does not import the production application or create broker
services. Supply a datastore containing saved reports and a session date. F6
sets 1180×760; F7 sets 1586×990. Escape closes only this preview. Screenshots from
real saved data remain private local review artifacts.
