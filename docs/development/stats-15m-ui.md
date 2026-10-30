# Stats workspace

The desktop tab and page heading are **Stats**. Atlas and Scout appear together,
with Scout directly below Atlas in the full content area. Wide windows show each
trader's predictions and execution side by side; narrow windows stack those
columns, with scrolling for content and tables.

The active workspace uses the existing read-only Atlas and Scout saved-results
readers. Session discovery includes their own forecasts, attributed orders and
supported independent 15-minute archives. Both readers use the same selected
Pacific session and can finish independently. Switching sessions clears old
results immediately; obsolete background replies are ignored. Missing evidence
remains missing, and each trader keeps its own scoring and execution population.

Scout's verified aggregate reports are read separately from
`intraday/scout/statistics-reports/v1/<completion-id>/` under the private local
datastore. The original `report.json` and `report.md` stay byte-for-byte intact;
`receipt.json` pins their hashes, producer, session, original report cutoff and
completion identity.
The report reader checks these bindings and reconciles original per-symbol and
aggregate totals. A damaged latest report is unavailable rather than silently
replaced by an older result. Report dates participate in session selection.

The report panel displays Scout's recorded accuracy, Brier, scoring coverage,
missing/synthetic exclusions, execution statuses and known submitted/filled
quantities. It labels report provenance and its original cutoff. It does not
construct forecast, request or fill rows from aggregates; the execution table
contains company summaries. Individual records and realized P&L remain unknown
when absent. If no report exists for a selected session, the existing Scout
saved-record reader and its independent history remain available. No report
bytes, private statistics or operating receipts belong in public source/tests.

Retired Gameplan prediction cards, horizon filters, hourly grid, execution
summary, report controls and Scout Gameplan horizon history are absent from this
workspace. Historical reader modules, saved outputs and revision ancestry remain
preserved. The active workspace does not invoke Gameplan readers, production,
collection, models, brokers, execution or activation services.

`tests/visual_atlas_stats_fixture.py` previews the active workspace using only
synthetic saved-evidence fixtures. UI source installation does not restart the
desktop app or any trader; an already open desktop retains its loaded UI until
its next normal launch.
