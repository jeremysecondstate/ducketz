# Desktop startup and saved-history responsiveness

The desktop shell creates a workspace when its tab is first selected. Hidden
workspaces no longer start database reads or refresh timers during application
launch. Each constructed workspace keeps its existing state across tab changes;
the two Hyperliquid views retain their shared account-history service.

The 15-minute view reads saved history and computes large-history metrics in
background workers. Tk polls completed results on its own thread. Repeated
refresh clicks share the pending read, failed reads retain the previous view,
and closing the window cancels the UI's read work. Saved probabilities,
direction labels, outcome revisions and metric cohorts retain their meanings.
Original databases and scientific records are not rewritten.

Rolling Forecasts coalesces scroll-region updates instead of recursively
draining Tk idle callbacks during layout. This prevents a resize from running
unrelated initialization before it returns.

The portable delivery contains exact consumer hunks in
`patches/ui-responsiveness.patch` and before/after SHA-256 identities in
`patches/ui-responsiveness.json`. Review applicable hunks against each PC's own
source. Preserve other changes, local symbols and operating bindings. The
package checker distinguishes exact reconstruction from a public base that
only permits hunk projection; projection checks do not establish installation.
Run the full local UI tests after adaptation and seal the reviewed complete
source generation through `tools/trader_installation.py`.

UI verification uses synthetic stores and stalled loaders, never live traders
or broker operations. A cold read of a large historical store can still take
time; it must leave the window responsive and show that loading is in progress.
Source publication, local installation and code loaded by an open window are
separate facts. Reopening the desktop UI loads its installed source. No trader
start, stop or restart is part of this repair.
