# Gameplan Stats at 17:30 Pacific

Jeremy's October 26, 2026 timing direction separates a daily Stats snapshot
from evening production. It supersedes older continuation prose only for the
affected batch's release times. The workflow's specified close remains 17:00
Pacific, including exchange early-close days.

For reviewed source session D, use `America/Los_Angeles`:

| Phase | Earliest eligibility | Scheduled wake |
| --- | --- | --- |
| Local-evidence Gameplan Stats snapshot | D 17:30 | Daily 17:30 |
| Same-day acquisition and remaining Gameplan production | D 21:00 | Existing daily dispatcher kickoff D 21:05; later task schedules unchanged |

Windows uses the equivalent `Pacific Standard Time` zone with a floating
17:30 calendar trigger, so daylight saving changes do not change local time.
The existing dispatcher retains its original 21:05 trigger, five-minute timer,
logon trigger, identity, principal, enabled state and unrelated settings.
`tools/register_nightly_watchdog.ps1` extends the existing registration through
`Set-ScheduledTask`; its repeat installation is idempotent.

## Dated execution and continuation

`ml.nightly_dispatch.session_phase` evaluates release instants for the actual
source session, not the current local hour. XNYS calendar selection retains
the next supported action date across weekends, holidays and DST. Monday
evening reviews Monday and prepares the association with Tuesday. Friday
normally maps to Monday. A prior unfinished night remains released during
the next morning or afternoon and after midnight.

The existing watchdog dispatches a dated `stats` responsibility with
`--stats-only` for the early phase. The same option supports an immediate,
idempotent missed-wake catch-up through `ml.nightly_workflow --run`; it may be
combined with an explicit `--resume-action-date` to retain the successor date.
The durable `early_stats` entry is separate from the unchanged evening stage
order: datastore catch-up, post-fetch Stats, model review, training/predictions,
local Gameplan, display and handoff. A legacy combined fetch-and-Stats stage
is never selected for the early phase.

The early worker runs only native `gameplan_stats`, releases ownership normally,
and returns. It never sleeps holding a lock until evening. Repeated timer and
logon wakes reuse valid completion evidence. Crash recovery preserves the same
dated logical work and immutable native attempts. Workflow and native execution
guards cover direct launch/run, explicit responsibilities, catch-up and resume;
native guards run before a selected segment and before individual subprocesses.
No early invocation can cascade into acquisition, inference, training, planning,
synthesis, handoff or downstream display/preparation.

## Evidence and post-fetch reconciliation

Stats reads the existing saved forecasts and supported local observation stores.
It does not acquire provider data. Scores retain the original forecast
probabilities, direction labels, target windows and recorded policy metadata;
missing historical policy metadata remains unrecorded. Its outcome cutoff stays
D 17:00, while generation and receipt times record actual execution time.

Missing reference or target observations remain missing, and immature targets
remain pending. A COMPLETE receipt establishes snapshot production, not complete
data coverage and not satisfaction of the evening acquisition dependency.
Daily review does not require every long-horizon forecast to mature.

Native Stats uses `--reconcile-existing`. Following ordinary evening catch-up,
the regular `prepare_stats` step evaluates the newly available local evidence.
Unchanged semantic results reuse the valid existing snapshot; changed evidence
creates an immutable revision with parent identity. The supported latest/dated
Stats readers see that revision. Model feedback binds the exact revised receipt;
affected unfinished feedback/production is rebuilt without discarding completed
scientific outputs or rewriting old publications.

For an unfinished split workflow, a completed Stats receipt that predates a
newly completed acquisition is preserved in recovery history and the post-fetch
Stats step is reopened. This also recovers a crash immediately after acquisition
was saved. Terminal batches and batches with completed scientific training keep
their historical receipts and bindings.

This change does not alter trading strategy, model selection, independent
15-minute systems, manual trader controls, cleanup or the separate 03:55
launchers. Infrastructure tests mock providers, subprocesses and model inference.
Real Stats execution is recorded separately from installation and offline tests.
