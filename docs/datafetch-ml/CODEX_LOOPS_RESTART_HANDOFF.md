# Codex handoff: restart and prove the Loops system

Paste the prompt below into a new Codex task rooted at `C:\dev\ducketz`.

## Handoff prompt

You are responsible for safely restarting the production Loops system and
proving that it is doing useful, current work. Do not stop at proving that
processes exist.

Current intended state at handoff:

- The seven Loops runtime owners are intentionally shut down.
- The Codex scheduled task `loops-hourly-operations` is intentionally paused so
  it cannot fight the shutdown.
- The seven dead-PID singleton locks left by the forced stop were hash-verified
  and moved intact to
  `C:\DATASTORE\logs\ducketz\shutdown-lock-quarantine\20260819T195012.8993700Z`.
  They are retained only as shutdown evidence and must not be restored into the
  datastore root.
- The production repository is `C:\dev\ducketz` and the datastore target is
  `pc` (`C:\DATASTORE`).
- The production watchlist is exactly `AAPL AMZN GOOG MU NVDA SNDK`.
- Canonical operational equity OHLCV must come from Databento `EQUS.MINI`.
  Schwab may supply quotes, option chains, and explicitly labeled fallback or
  broker evidence; it must not silently become the canonical OHLCV source.
- The separate cold archive remains `XNAS.ITCH`; do not merge it into the
  operational `EQUS.MINI` authority merely to make freshness checks pass.

Treat those statements as expected state, not proof. Recheck them locally.

### Safety contract

1. Inspect `git status` first. Preserve all existing changes and never discard
   or overwrite unrelated work.
2. Never print credentials. Check only whether required credentials are
   present.
3. Do not place, preview, or submit orders.
4. Do not run a broad provider-history download, OPRA bootstrap, cold start,
   backfill, pointer rewrite, model promotion, or datastore cleanup as part of
   this restart. If one is truly required, keep the Loops task paused and
   report the exact blocker and proposed bounded command.
5. Never kill a process based on a fuzzy name. Match only the seven modules in
   `ml.system_guardian.GUARDIAN_LAUNCHES`, capture the exact command lines and
   parent/worker PIDs, and stop only a verified owned process tree.
6. Never delete a lock merely because it exists. Resolve its exact path and
   recorded PID, prove the PID is dead and no matching owner exists, and record
   what was removed. A live or ambiguous lock is a blocker.
7. Keep the scheduled task paused until the end-to-end freshness proof passes.

### Restart procedure

1. Read these authorities before acting:

   - `ml/system_guardian.py`
   - `ml/system_monitor.py`
   - `docs/datafetch-ml/start_all_loops.ps1`
   - `docs/datafetch-ml/current_start_command`

2. Enumerate Windows processes and verify the owner state for all seven
   allowlisted runtime modules. Also verify that no `start_all_loops.ps1`,
   `ml.system_guardian`, or `ml.system_monitor` process is still running from a
   prior attempt. If a valid complete owner pair is already running, do not
   duplicate it. If ownership is partial, duplicated, or noncanonical, diagnose
   that state before any process change.

3. Verify without exposing values that the Databento credential is available,
   `DATABENTO_EQUITIES_DATASET` resolves to `EQUS.MINI`, and
   `DATABENTO_EQUITIES_HISTORY_DATASET` resolves to `XNAS.ITCH`. Verify the
   checked-in watchlist is exactly the six production symbols.

4. Run the launcher audit first:

   ```powershell
   & .\docs\datafetch-ml\start_all_loops.ps1 -AuditOnly
   ```

   An audit exit code of 2 is expected when the intentionally stopped owners
   are reported as missing. It is not acceptable if the output reports a
   partial owner, command drift, a live foreign lock, or an unexplained lock.

5. If the audit proves a clean stopped state, start the seven owners once:

   ```powershell
   & .\docs\datafetch-ml\start_all_loops.ps1
   ```

   Parse the launcher's JSON. Require `issues` to be empty and require every
   runtime to be either `STARTED_VERIFIED` or a previously proven canonical
   `ALREADY_RUNNING` owner. Verify one launcher/worker pair and the worker-owned
   exact singleton lock for every runtime. Do not launch guessed commands or a
   second owner.

6. Poll read-only health in bounded intervals while giving progress updates.
   Use the monitor, not repeated guardian repair calls:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode scheduled --compact
   ```

   Allow enough time for the next eligible quarter-hour cycle and Databento's
   advertised historical-availability boundary, but do not wait silently or
   indefinitely. A live PID is not evidence that a fetch or prediction cycle
   succeeded.

### Required end-to-end proof

Do not declare success unless one final read-only monitor run is `HEALTHY`, not
`DEGRADED` or `UNHEALTHY`, and the evidence establishes all of the following:

- All seven process, lock, and runtime-log checks are valid and current.
- Loop A has a new zero-failure complete cycle after this restart.
- Bar readiness is current for the latest market-eligible target and all six
  symbols. Off-hours freshness means the latest eligible completed market
  boundary; do not demand nonexistent future bars.
- The current canonical operational OHLCV receipts identify Databento
  `EQUS.MINI`, not Schwab price history and not the `XNAS.ITCH` cold archive.
- Directional Loop B has a fresh verified publication with its configured
  `1h`, `4h`, `1d`, and `1w` routes and current prediction evidence.
- Options Capture, Active Pricing, Strategy, CME, and ALFRED have current,
  checksum-valid publications for the market-aware target their contracts
  require.
- Cross-loop lineage and both UI publication contracts verify.
- No check contains a stale condition, unresolved `WARN`, or `FAIL`.

If the monitor is `DEGRADED`, `UNHEALTHY`, or reports stale evidence, treat it
as an incident. Preserve the failing JSON and relevant log/receipt paths;
identify the producing runtime and first bad boundary; distinguish process
health from publication health; and diagnose provider, credential, capacity,
lock, command, code/configuration, and publication-lineage causes before
changing anything. Apply the smallest high-confidence repair, run focused
tests or a direct repro, restart only the exact affected allowlisted runtime if
needed, and rerun the read-only monitor. Never hide a stale publication by
rewriting a pointer or substituting Schwab OHLCV. If a safe repair cannot be
completed, leave the scheduler paused and report the exact evidence and next
bounded action.

### Resume scheduled protection

Only after the final health proof passes, update the existing Codex scheduled
task with id `loops-hourly-operations` from `PAUSED` to `ACTIVE`. Preserve its
name, prompt, hourly cadence, target task, and all other fields. Do not create a
duplicate scheduled task. Verify the saved task is active.

Finish with a concise evidence report containing the final health status and
checked-at time, the seven launcher/worker PID pairs, latest eligible target,
fresh Loop A cycle and Databento receipt, fresh prediction/publication paths,
any repair made and tests run, and scheduled-task status. If anything remains
stale or degraded, say so plainly and do not call the restart complete.
