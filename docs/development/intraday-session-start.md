# 15-minute launcher session start

Jeremy's October 29, 2026 direction sets the two initial 15-minute launchers to
wait until **04:00 America/Los_Angeles** when started early. Their existing
weekday **03:55** Windows task schedules remain unchanged. A launch at or after
04:00 proceeds immediately.

`Start-15m-Data.cmd` and `Start-15m-Trader.cmd` enter
`tools/intraday_session_wait.py`. The terminal identifies the launcher and says
it is waiting until 4:00 AM Pacific session start, repeating the remaining time
once a minute. Ctrl+C during this wait cancels that launch before a data or
trading session is created. Stopping its Task Scheduler instance or closing
the originating terminal also ends the pending launch. Windows process handles
pin the original launcher and any Python redirector, so a detached waiter or
reused process ID cannot later start a session. This observation never signals
another process. At the boundary the wrapper enters the original host in the
same Python process and preserves all forwarded arguments, exit behavior,
independent state and normal manual controls.

This applies to new initial launches. An already running data lifetime keeps
collecting and catching up; it is neither stopped nor made to wait. No delay is
added to existing nightly production or to workers renewing inside a session.
The wrapper owns no broker, provider, data lock, trading control or session
during the wait. Windows supplies Pacific daylight-saving conversion without
loading mutable Python site packages.

The two installed launcher compositions differ from some public source bases.
`patches/intraday-session-wait.patch` carries only the invocation replacements,
with complete context and exact before/after hashes in its JSON manifest. Do
not replace a peer's complete launcher with these contextual bytes. Review and
apply the invocation change to that installation's own launcher. The data
invocation deliberately retains its original 116-byte line and byte-373 batch
tail, including trailing spaces, because an existing `cmd.exe` may still be
waiting for its current data process to return.

`tests/check_intraday_session_wait_package.py` reconstructs both complete
launcher preimages and postimages, verifies raw line endings and that data
tail, then tests the new wrapper and reconstructed consumers in an isolated
synthetic project. It never loads a live host or contacts a provider. Saved
task definitions and actual scheduler tests remain separate local evidence.
