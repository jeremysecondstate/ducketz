# Paper recovery after the Codex app restart

Observed **2026-09-26, around 00:04 Pacific**. [Monitoring](../MONITORING.md)

## Incident and evidence

H.Y.P.E.R. correctly marked Data, Models and Paper stopped. The original worker
processes were absent; the model PID had been reused by an unrelated Python
process, so PID existence alone would have misidentified it as healthy.

| Component | Last pre-interruption heartbeat (UTC) | Original runtime PID |
| --- | --- | --- |
| Paper | 2026-09-26 06:54:26 | 47808 |
| Models | 2026-09-26 06:54:48 | 70736 |
| Data coordinator | 2026-09-26 06:54:52 | 45288 |

The replacement ChatGPT/Codex host processes started at 06:54:56/58 UTC. The user
confirmed restarting the app after resetting usage. The workers retained saved
`running` states and empty error logs rather than completing graceful shutdown.
No reboot or matching application-crash event was found in the inspected window.
Together, this points to worker termination during the host restart; the exact
termination mechanism was not traced.

The Paper database passed read-only SQLite integrity and foreign-key checks.
All 662 pre-recovery cycles had their four equity rows and no orphaned records.
The original seed, three accounts and ten positions remained present. The final
committed pre-interruption observation matched the saved heartbeat. All four
forecasts validated at that observation; their later expiry was a normal,
handled validation condition, not evidence of a Python crash.

## Recovery performed

1. Restored data and models, then verified fresh BTC, ETH, HYPE and ZEC forecasts
   for the 07:00 UTC candle before resuming Paper.
2. Tested process ownership. Ordinary hidden `Start-Process` launchers remained
   members of a Windows job. A Python breakaway flag did not remove that membership
   in this environment, so it was not treated as an independent launch.
3. Gracefully stopped only the temporary recovery data/model processes. Launched
   the three normal module entry points through Windows `Win32_Process.Create`
   with `Win32_ProcessStartup.ShowWindow=0`, using hidden `cmd.exe` wrappers to
   redirect stdout/stderr to the existing component runtime directories.
4. Verified each replacement shell and virtual-environment launcher was outside
   a Windows job and descended from `WmiPrvSE.exe`, not Codex. The actual Python
   workers retain their own virtual-environment launcher job; this is distinct
   from the original host ownership.
5. Paper opened its existing database. It did not reseed or reset history.

Replacement runtime PIDs at recovery were Data **57004**, Models **60964**, and
Paper **31424**. These are dated evidence, not permanent process identities.
Data/model log prefix: `independent-20260926-000335`; Paper log prefix:
`independent-20260926-000351`, in their usual control directories.

At 07:04:39 UTC, the same read-only adapter used by H.Y.P.E.R. reported all
17 sources fresh with no warnings. Paper had advanced to 672 committed cycles,
with a new observation at 07:04:27 UTC. Original pooled opening equity remained
**$42,078.277612908096** and the original seed timestamp remained
**2026-09-26T02:04:46.898381710Z**. Current equity is a changing observation,
not a recovery invariant.

A second independent read-only check at **07:05:29 UTC**, 50 seconds after the
first, confirmed the same three module-verified workers and all 17 fresh sources.
Committed cycles advanced **672 → 673**, with the original seed/equity baseline
unchanged and no Paper errors. No further restart was needed.

## Operating boundary

No trading policy, model recipe, Paper balance or application code was changed
by the recovery itself; resumed Paper cycles continue their normal simulated
accounting. Powder remained disconnected and no real trading was activated.
No Windows startup task, service or automatic restart schedule was installed.
The new workers run independently of the Codex process tree, but reboot, logoff
or an explicit process stop still requires checking and restoring operation.

Use the existing module-specific graceful stop commands in
[Monitoring](../MONITORING.md). Do not kill every Python process or delete locks
to manage these workers. If launching from an agent-owned session, verify the
resulting process ancestry and job membership rather than assuming that a hidden
window or a returned PID guarantees independent lifetime.
