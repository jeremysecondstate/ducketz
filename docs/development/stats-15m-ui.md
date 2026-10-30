# Stats workspace

The desktop tab and page heading are **Stats**. Atlas and Scout selectors each
show their independent 15-minute prediction and order panels in the full content
area. Atlas is selected initially. Wide windows show predictions and orders side
by side; narrow windows stack them, with scrolling for content and tables.

The active workspace uses the existing read-only Atlas and Scout saved-results
readers. Session discovery includes their own forecasts, attributed orders and
supported independent 15-minute archives. Both readers use the same selected
Pacific session and can finish independently. Switching sessions clears old
results immediately; obsolete background replies are ignored. Missing evidence
remains missing, and each trader keeps its own scoring and execution population.

Retired Gameplan prediction cards, horizon filters, hourly grid, execution
summary, report controls and Scout Gameplan horizon history are absent from this
workspace. Historical reader modules, saved outputs and revision ancestry remain
preserved. The active workspace does not invoke Gameplan readers, production,
collection, models, brokers, execution or activation services.

`tests/visual_atlas_stats_fixture.py` previews the active workspace using only
synthetic saved-evidence fixtures. UI source installation does not restart the
desktop app or any trader; an already open desktop retains its loaded UI until
its next normal launch.
