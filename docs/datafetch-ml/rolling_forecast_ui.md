# Rolling forecast dashboard

Implementation snapshot: 2026-08-03

The first Duckets application tab is a read-only view of Loop B's current `1h`,
`4h`, and `1d` routes plus one dynamic remaining-week snapshot. The weekly
model family still uses aggregate `1w` and components `1w-d1` through
`1w-d5`; the current snapshot contains the aggregate and only the contiguous
Day 1 prefix whose targets remain in one exchange week.

## Run

```powershell
python -m app.main ui
```

The compatibility mirror is:

```text
DATASTORE/ml-intelligence/latest/rolling-predictions.parquet
```

Override the file for local testing:

```powershell
$env:DUCKETS_ROLLING_PREDICTIONS_PATH = "C:\fixtures\rolling-predictions.parquet"
python -m app.main ui
```

By default the UI reads `ml/latest/run.json`, the single authoritative
current-view commit pointer, and resolves `intelligence.parquet` from that
immutable timestamped run. It does not scan for a newest directory. If a
legacy datastore has no pointer, the dashboard falls back to the mirror above;
an explicitly configured fixture path is also read directly. The mirror is not
publication authority for receipt-era runs.

## Sibling Options Strategies tab

**Rolling Forecasts** and **Options Strategies** are separate tabs with
compatible symbol and concrete-horizon concepts. This document's sample,
intelligence, card, actionability, and live-evidence rules apply only to
Rolling Forecasts. That screen remains a read-only presentation of underlying
directional probabilities.

The sibling Options Strategies screen resolves the authoritative run's
`strategy-candidates.parquet`, validates its separate exact schema, and joins a
newly fetched Schwab account snapshot when it loads. It displays every
candidate for the chosen symbol/horizon under the exact headings **Rank**,
**Strategy**, **Exact legs**, **Market probability**, **Expected return**,
**Portfolio fit**, and **Overall score**. Its market probability estimates the
candidate's profitable observed-BBO outcome; it is not the Rolling Forecasts
up probability. It uses calibrated strategy probability after a compatible
GOOG route model exists and otherwise displays the explicitly uncalibrated raw
market-state scenario prior. The prior also supplies expected return and the
persisted market score, so constructible prior-only rows do not appear blank.

Current shares, option-position counts, working option-order counts, and
available funds affect display context and the versioned
`current-schwab-position-fit-v1` overlay only. They do not rewrite Loop B
Parquet or become historical training features. Selecting an entry in
**Exact legs** fills or replaces the order ticket, whose visible fields are
**Schwab order**, **Quantity**, **Order method**, **Limit price**, and
**Duration**. Its leg table shows human-readable action, exact contract
expiration/strike/type, quantity, bid, and ask.

The Loop B market score is expected return on risk. The display-time portfolio
fit adjustment is added to it to produce **Overall score**, after which visible
**Rank** is recalculated. This ranking belongs only to Options Strategies;
Rolling Forecasts retains its own persisted directional presentation and does
not adopt strategy-market or account-overlay fields.

That separate screen has a direct **Submit Order** button. After a
human-readable confirmation it uses the existing
`SchwabSession().submit_order(...)` path and the same accepted-order receipt as
Schwab Duckets. Most strategies create one order. Twin-Peak Fly exposes a
lower-price and higher-price complete butterfly; Range-to-Trend Relay exposes
a near-expiration iron condor and a later-expiration long strangle. A successful
component submission advances to the next component. See
[Loop B options-strategy selection](options-strategy-selection.md) for the
analytics, portfolio-score, exact payload, and component contracts.

## Supported contract

`app/ui/rolling_forecast_data.py` is the boundary between Parquet and the view.
Before adapting rows, it selects the contract declared by `schema_version` and
verifies the corresponding exact Arrow physical schema. The current
`INTELLIGENCE_SCHEMA` from `ml/parquet_contracts.py` is
`schema_version = one-id-v2`; the adapter also accepts the legacy physical
`one-id-v1` contract.

The file begins with one Duckets-generated string field:

```text
id = symbol|horizon|decision_timestamp
```

The remaining fields are readable values:

```text
symbol
horizon
decision_timestamp
forecast_created_at
information_available_at
target_window_start
target_window_end
actionable_until
target_definition_version
probability_up
probability_down
actionability_status
operational_status
model_evidence_status
live_evidence_status
intelligence_status
model_name
completed_decision_count
minimum_live_decision_count
automated_action_allowed
limitations
schema_version
```

The two fields added by `one-id-v2` are `target_definition_version` and
`minimum_live_decision_count`. For a physical `one-id-v1` file, the adapter uses
the canonical threshold for that row's horizon. Unknown fields, missing fields,
a different field order/type, a mixed-version file, or any version other than
`one-id-v1` and `one-id-v2` produce a structured schema error. Duplicate
symbol/horizon rows and unknown horizons are also rejected.

The UI accepts these internal horizon values without adding any Parquet field:

```text
1h
4h
1d
1w
1w-d1
1w-d2
1w-d3
1w-d4
1w-d5
```

If any row is marked `FROZEN_WEEKLY_SNAPSHOT`, the frozen rows must contain
aggregate `1w` plus a contiguous prefix beginning with `1w-d1`. Unused later
model-route rows may remain in current output with no forecast status. The
adapter requires one shared decision timestamp, issuance timestamp, and
information time; ordered component windows; aggregate bounds from Day 1 open
through the last published component close; aggregate and `d1` deadlines at
the first remaining session close; and every later component deadline at its
own close. Probabilities must be present and complementary. The accepted
target-definition versions are:

| Horizon | Target-definition version |
| --- | --- |
| `1w` | `dynamic-remaining-week-aggregate-open-close-v2` |
| `1w-d1` | `dynamic-remaining-week-d1-open-close-v2` |
| `1w-d2` | `dynamic-remaining-week-d2-open-close-v2` |
| `1w-d3` | `dynamic-remaining-week-d3-open-close-v2` |
| `1w-d4` | `dynamic-remaining-week-d4-open-close-v2` |
| `1w-d5` | `dynamic-remaining-week-d5-open-close-v2` |

Retired fixed-window `1w` targets are rejected rather than adapted into a
remaining-week outlook. No synthetic component rows or compatibility forecast
are created.

## Display behavior

The dashboard creates three ordinary cards for `1h`, `4h`, and `1d`, followed
by one full-width **remaining-week outlook**. The subtitle is:

> Read-only 1h, 4h, 1d, and dynamic remaining-week probability outlooks.
> Probabilities are not recommendations.

The weekly composite shows:

- a clear **Current remaining-week snapshot** indicator;
- the shared UTC and local issuance timestamp;
- an **Aggregate (Remaining Week)** panel with up/down probability and UTC/local
  start/end timestamps for the dynamic aggregate window;
- one row for every remaining Day 1-prefix session, with actual weekday/date;
- each component's up/down probability and UTC/local open/close timestamps;
- each route's pending/completed outcome-evidence status and accumulated
  live-evidence progress.

For `1h`, `4h`, and `1d`, fresh probabilities remain visible when
`actionability_status` is `ACTIONABLE`. A second, narrow display contract keeps
a verified carried probability visible while its original target is underway:

```text
actionability_status = TARGET_WINDOW_STARTED
intelligence_status = FORECAST_IN_PROGRESS
automated_action_allowed = false
target_window_start <= loaded_at < target_window_end
```

The card says **Forecast in progress — entry window passed; not actionable**,
continues to show up/down probabilities and the original UTC/local target
window, and never treats the row as actionable. At `target_window_end` the
adapter suppresses the probability. A generic stale/non-actionable probability,
including a `TARGET_WINDOW_STARTED` row without the paired trusted intelligence
status and timing contract, remains suppressed and is reported as a display
safeguard. A validated remaining-week probability remains visible for its published snapshot; this exception requires
`actionability_status = FROZEN_WEEKLY_SNAPSHOT` and one of the six exact target
versions above. It does not weaken ordinary route suppression.

The displayed probabilities are the `probability_up` and `probability_down`
values already published by Loop B. The UI does not load `model.joblib`, refit
or reapply Platt calibration, or reproduce the aggregate `1w` calibration-range
clipping policy; those operations belong to Loop B before publication.

`PENDING_EVIDENCE`, `COMPLETED_EVIDENCE`, and `FORECAST_IN_PROGRESS` come from
the existing `intelligence_status` field. They describe the current route's outcome state.
`live_evidence_status` and `completed_decision_count` continue to describe the
route's accumulated verified prospective evidence. The UI does not infer
evidence maturity from its own wall clock, and evidence thresholds do not gate
an in-progress probability.

A missing ordinary horizon gets an explicit "No current forecast" card. If no
weekly routes are current, the full-width weekly card says that no current
remaining-week snapshot was published. A valid shorter Day 1 prefix is shown;
a missing aggregate, missing Day 1, or gap inside that prefix rejects the
publication as incompatible.

Cards do not show a separate model-testing row. Model identity remains in Debug
details. Operational state and limitations remain visible in the dashboard
summaries. The automation indicator is informational; Rolling Forecasts never
places an order or writes to the datastore. The sibling Options Strategies
ticket has the explicit, operator-confirmed Schwab submission path described
above.

An older valid physical `one-id-v1` file containing only ordinary short
horizons remains readable. Legacy `1w` rows are not treated as remaining-week
forecasts. Legacy rows have no persisted minimum, so the adapter supplies the
shared threshold for ordinary routes only.

### Responsive card layout

The ordinary route grid is independent of the six internal weekly rows:

| Available width | Layout |
| --- | --- |
| at least 1500 px | three ordinary cards in one row, then the full-width weekly card |
| 760 through 1499 px | two ordinary columns, then the full-width weekly card |
| below 760 px | one ordinary card per row, then the full-width weekly card |

The content canvas tracks the available width and keeps vertical scrolling.
It does not introduce a horizontal scrollbar or require cards to extend beyond
the visible canvas.

The adapter trusts persisted operational and evidence statuses. It does not
recalculate evidence maturity, but it does compare a claimed ordinary
`FORECAST_IN_PROGRESS` row with the load time so an ended target cannot remain
visible. It independently verifies the remaining-week snapshot's structural
timing invariants before rendering the badge.

### Operational status

For `1h`, `4h`, and `1d`, `OPERATIONALLY_CURRENT` requires either an actionable
fresh prediction or a verified carried forecast whose target window is still
in progress at publication. The latter is explicitly non-actionable.
Successfully loading ready inputs and a model is not enough. A remaining-week route instead remains operationally current while its
verified snapshot is published. Its session-close deadline is persisted by
Loop B, and a newer completed decision may replace it with the shorter current
outlook.

The dashboard summary distinguishes a fully stale publication from a mixed
route state. If at least one current ordinary route or verified remaining-week
outlook coexists with stale routes, the summary uses a warning tone and says
that current outlooks have route timing gaps. It uses the red stale state only
when no route reports an operationally current status. The route detail reports
live routes, current remaining-week outlooks, and published rows separately so
a valid weekly outlook is never described as zero current data.

### Live-evidence labels

On a symbol card, a **completed live forecast** is one genuinely prospective
`LIVE` prediction for that exact symbol/horizon route that:

- came from a prior run with a complete, verified manifest and output inventory;
- when the run declares the transactional publication contract, has a valid
  `publication.json` receipt bound to that manifest and is reachable through
  the authoritative pointer's publication chain;
- had valid information and was created strictly before its predetermined
  route deadline (target entry for ordinary routes, session close for the
  dynamic weekly routes);
- has since reached a complete label after the full target window and processing
  delay; and
- reconciled with the same target-definition contract, exact target start/end,
  and round-trip-cost convention.

The natural evidence key is:

```text
symbol, horizon, decision_timestamp
```

Repeated publications of that decision count once. The earliest eligible
`prediction_created_at` is canonical for its live-performance metrics.
Including symbol and horizon in the key prevents cross-symbol and cross-horizon
collisions.

`BACKTEST` rows, pending labels, post-deadline predictions, incompatible target
contracts, mismatched target windows or costs, invalid predictions, incomplete
or invalid manifests, and closed-lockbox rows never increase
`completed_decision_count`. Historical rolling samples and offline assessment
evaluations are not prospective live evidence. A `4h` forecast cannot complete
until its entire target and processing delay have elapsed. Each `1w-dN` route
can complete only after that session's close plus processing delay; aggregate
`1w` can complete only after its dynamic final close plus processing delay.

The count and `minimum_live_decision_count` are both route-specific. They refer
to the card's symbol/horizon, not a pooled horizon-wide model sample.

| Completed count | Exact card and accessible wording |
| --- | --- |
| zero for `1h` or `4h` | `Live evidence: Awaiting first completed forecast (0 of 60)` |
| zero for `1d` or any remaining-week route | `Live evidence: Awaiting first completed forecast (0 of 30)` |
| one or more | `Live evidence: X of N completed forecasts`, with the route's persisted count and threshold substituted |

The thresholds are 60 decisions for `1h`, 60 for `4h`, 30 for `1d`, and 30 for
each of `1w` and `1w-d1` through `1w-d5`.
`NO_COMPLETED_DECISIONS` applies at zero,
`INSUFFICIENT_LIVE_EVIDENCE` below the threshold, and
`LIVE_EVIDENCE_AVAILABLE` at or above it. Crossing the threshold changes the
persisted status and visual tone, while the wording remains honest progress
against the same denominator.
`automated_action_allowed` remains false and the dashboard remains read-only.

An empty but valid file is different from a missing or unreadable file. The view
states whether there are no rows, no actionable routes, or operational
limitations.

## Refresh and errors

The dashboard loads once when the tab opens and again only when the user selects
Refresh. There is no periodic refresh timer or automatic polling. Each load
happens on a background thread so a slow Drive-backed Parquet does not freeze
the window. The refresh and debug buttons are disabled while a load is in
progress. Generation tracking prevents an older load result from replacing a
newer one.

Starting a load clears the previously rendered frame. A load error leaves the
forecast frame cleared and shows the structured error; the UI does not keep
displaying stale values from its prior successful in-memory load.

Errors are grouped into:

- missing or invalid authoritative pointer, or missing referenced current
  artifact;
- unsupported schema version;
- incompatible physical schema;
- corrupt, incomplete, or temporarily unreadable file;
- unexpected adapter failure.

## Debug details

The debug view shows:

- source path and load time;
- supported schema and source row count;
- operational statuses and automation flag;
- every ordinary, aggregate, and component route's `id`, readable timestamps,
  `model_name`,
  `model_evidence_status`, target-definition version, probabilities, statuses,
  unique completed-live count and threshold, limitations, and schema version.

Removing the visible model-testing line does not remove offline metrics from
model artifacts or change the Parquet contract solely for presentation. Debug
continues to expose the model identity and raw evidence status present in the
intelligence row.

There are no internal model, feature-set, target, decision, route, or run IDs to
decode.

## Ownership

Loop B publishes all required symbol/horizon routes as one completed cycle. A
failed required route aborts the cycle before current-output publication, so the
authoritative pointer never exposes a partial run. The existing pointer
continues to resolve the prior successful immutable run, and a manual refresh
still reads that publication. Compatibility mirrors are not used to infer a
new current run.

On a successful cycle, Loop B atomically commits `ml/latest/run.json` only
after the timestamped outputs, verified manifest, verified prepared publication
receipt, and deadline checks are complete. The pointer makes that receipt
publication-valid. The Rolling Forecasts UI is a consumer and never repairs,
rewrites, or deletes the pointer, run artifacts, or compatibility mirror.

With three requested symbols and public horizons `1h`, `4h`, `1d`, and `1w`, a
successful new publication contains 27 current routes: three ordinary routes
plus six internal weekly routes per symbol. The UI groups those 27 rows into
three ordinary cards and one weekly composite per symbol. Deployment and
supervisor restart remain operator actions; no UI action starts either loop.
