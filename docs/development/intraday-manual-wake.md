# Early manual 15-minute startup

`Start-15m-Trader.cmd` and `python -B -m ml.intraday.live_session --live`
can be launched before the session's operating time. The manually started
process records `WAITING` and its `wake_at`, then sleeps until 04:00
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

Waiting selects only one operating day. Scout's automatic 17:00 Pacific stop
uses that selected day, including when an early launch waits across closed
dates. A Scout launch at or after 17:00 still stops immediately and does not
schedule the next day. Once a manual session stops, a later session needs a
new human start. Atlas retains its existing stop policy. No scheduler, manual
stop override or automatic daily restart is introduced.
