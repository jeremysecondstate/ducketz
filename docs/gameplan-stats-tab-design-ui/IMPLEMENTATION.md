# Gameplan Stats implementation

The prediction version of Concept A is now implemented as the fifth tab in the Duckets desktop UI.

## Use

Open or reopen the Duckets UI, then select **Gameplan Stats**. The existing launch command is:

```powershell
.venv/Scripts/python.exe -m app.main ui
```

The latest reviewed session loads automatically. Choose a saved session or a horizon to inspect its summary. Click a company row, or focus the table and use the up/down keys, to update the selected-company panel. Click an hourly cell for the original call, saved probability, observed return, target window and actual observation timestamps.

**Open full report** opens the immutable Markdown report belonging to the displayed session. **Metric definitions** explains the targets, denominators and exclusions.

Refresh runs in a background thread. While visible, the tab also checks for published results every five minutes and when reopened after at least 30 seconds. A manually selected session stays selected. Stale background responses cannot replace a newer selection.

The table and grid scroll horizontally when needed; the page scrolls vertically at the app's default 1180×760 window size. Cards and the company/coverage panels stack at narrower widths.

## Data and metric behavior

[gameplan_stats_data.py](C:/dev/ducketz/app/ui/gameplan_stats_data.py) reads the dated/latest native actuals-review pointers. It verifies the receipt hash, manifest, output checksums, reviewed date, forecast identities, saved probability errors and direction outcomes. It does not read broker accounts, fetch prices, train models, rewrite forecasts or publish reviews.

The cards and company summary show:

- Direction accuracy and its correct/scored count.
- Brier probability error, with lower-is-better wording.
- Separate bullish and bearish accuracy with sample counts.
- Counts of evaluated, pending and missing outcomes.

Neutral calls have no directional score but their probabilities still receive a Brier score when evaluated. A direction with no scored calls displays a dash and **0 scored calls**. Pending and missing outcomes never become failures simply because time passed.

The horizon filter affects summary statistics and selected-company metrics. The labelled hourly grid always covers thirteen 1h execution windows; it does not substitute daily/weekly forecasts into hourly columns. Its scope is stated below the grid. All-horizon summaries include the saved opening-gap research forecasts, matching the reviewed concept. Unpromoted forecasts are excluded explicitly.

These remain snapshots of the published review, with outcome cutoff and review time visible. Refresh loads newly published reviews; it does not rescore a historical session or manufacture maturity for pending forecasts.

## Verified data

| Session | Direction accuracy | Brier | Evaluated / pending / missing |
|---|---|---|---|
| September 10 | 47/87 = 54.0% | 0.222 | 121 / 42 / 5 |
| September 11 | 49/105 = 46.7% | 0.215 | 119 / 42 / 7 |

September 11 has no scored bullish calls. All 105 scored directional calls are bearish.

## Validation

- 226 related tests passed across the new data/UI tests, app tab integration, Rolling Forecasts, independent forecast UI, native actuals reviews, app startup, and the Schwab/Hyperliquid UI tests.
- 21 focused tests passed again after the final percentage-formatting and scrollbar adjustments.
- Verified both production review snapshots and all per-company 1h correct/scored counts against the saved data.
- Visually inspected the actual rendered widgets, including the default window size.
- Checked whitespace with git diff --check.

[Finished tab — September 11](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/implemented-gameplan-stats-september-11.png)

[September 10 render](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/implemented-gameplan-stats-september-10.png)

[Default-size render](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/implemented-gameplan-stats-default-size.png)

The screenshot fixture is [visual_gameplan_stats_fixture.py](C:/dev/ducketz/tests/visual_gameplan_stats_fixture.py). It mounts the actual tab using saved local reviews without initializing the app's other services. It creates a short-lived preview window for capture, then exits.

The implementation leaves the approved concept image intact. No changes were made to the nightly schedules, trading processes, broker controls, training pipeline or saved result data.

