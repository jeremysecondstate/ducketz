# Early manual 15-minute startup

`Start-15m-Trader.cmd` and `python -B -m ml.intraday.manual_launcher --live`
can be launched before the session's operating time. The manually started
launcher records `WAITING` and its `wake_at`, then sleeps until 04:00
America/Los_Angeles on the selected exchange session date. Weekends and holidays
wait for the next session. On shortened sessions, wake is no earlier than the
existing calendar's observed-data opening time. Pacific daylight-saving time
is respected.

During the wait there is no broker preflight, recovery polling, market-data
provider, history loading, model fitting or operating worker. Ctrl-C records a
manual stop and prevents that pending startup. The local runtime lock prevents
a second manual trader from starting. Status heartbeats refresh at most once
per minute while the terminal remains interruptible.

At wake, the process reads the current model recipe and performs fresh normal
startup checks. Missing live inputs or broker prerequisites remain explicit;
waking does not establish readiness. The normal forecast policy is unchanged:
the 04:00 issue boundary is excluded, and the first normal eligible boundary is
04:15 Pacific plus the configured publication offset, subject to actual data
and model gates.

Scout may launch the evening before: an invocation after the current exchange
session has closed selects the next eligible session, including weekends,
holidays and daylight-saving changes. For example, Sunday 21:00 Pacific waits
until Monday 04:00. An already-closed shortened session also advances.

Waiting selects only one operating day. Scout's automatic 17:00 Pacific stop
uses that selected day. If the PC resumes after that day's stop, the invocation
stops without starting the engine or rolling forward. Once the session stops,
a later session needs a new human start. Atlas delegates directly to its existing
entrypoint and retains its existing timing and stop policy.

The additive launcher leaves the engine and frozen model assessment bindings
unchanged. Its own status is `intraday/manual-launcher.json`, with per-invocation
events in `intraday/launchers/`. The launcher lock lasts through engine execution;
the existing engine lock blocks direct engine starts during the wait and is
released immediately before delegation. If another direct caller acquires it in
that handoff interval, the existing engine fails closed. Engine status remains
in `intraday/manual-session.json` after delegation. Keep the launch terminal open
and the PC available. There is no scheduler or automatic daily restart.

Scout delegates to the existing engine's `run_manual` with unchanged runtime
arguments and a startup clock fence. The first engine clock read must still be
within the selected opening and stop; a suspension during handoff cannot select
another day. Every clock read returns actual observed time without shifting it.
After the first valid read, the engine's existing deadline logic owns shutdown.

The direct `python -B -m ml.intraday.live_session --live` entrypoint retains its
historical behavior, including immediate Scout stop after 17:00. Use the command
file or `manual_launcher` for evening starts. Without `--live`, diagnostics
delegate directly and do not wait or create launcher state.
