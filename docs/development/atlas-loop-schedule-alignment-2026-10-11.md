# Atlas Loop A / Loop B and schedule alignment: October 11, 2026

Atlas's reviewed weekday schedules are already installed. This audit corrects
documentation and records an installed cleanup coverage gap; it changes no
native recurrence, task identity, memory, notification preference, private
binding or Windows registration. Configured scheduling, local installation,
documentation publication and actual runtime completion are separate facts.

The current-state observation is **October 11, 2026 at 12:46:17 p.m. PDT** for
native Codex definitions and next-run metadata, with adjacent Windows/configuration
inspection. This revision supersedes older Atlas daily-checkpoint, paused
handoff/reconciliation, five-minute Codex-supervision and 01:25 reconciliation
prose. The earlier observations remain preserved in the
[scheduled-task chapter](atlas-scheduled-tasks.md),
[Loop A reference](../loops-system-analysis/loops/loop-a.md) and
[Loop B reference](../loops-system-analysis/loops/directional-loop-b.md).
This precedence also applies to older local `docs/development/nightly-operations.md`
and `docs/development/nightly-workflow.md` prose, whether or not that runtime
documentation lineage is present in the publication checkout. Their five-minute
Codex wording and 01:25 gate are not Atlas's current native configuration.

## Comparison and independently verified findings

Scout's [reconciliation report](https://github.com/jeremysecondstate/atlas-scout/blob/0c76367a79c1b6b224f01c55f63173f2dc7f789f/system-details/scout/loop-schedule-alignment-2026-10-11.md)
and [schedule chapter](https://github.com/jeremysecondstate/atlas-scout/blob/0c76367a79c1b6b224f01c55f63173f2dc7f789f/system-details/scout/scheduled-tasks.md)
are comparison evidence from commit `0c76367a79c1b6b224f01c55f63173f2dc7f789f`,
published through [PR #15](https://github.com/jeremysecondstate/atlas-scout/pull/15)
stacked on [PR #13](https://github.com/jeremysecondstate/atlas-scout/pull/13).
The reviewed [weekday handoff](https://github.com/jeremysecondstate/atlas-scout/blob/480e531e415518aa3d0f3e7c75788d0d547764dc/messages/atlas/2026-10-11-datastore-weekday-eligibility.md)
is pinned to commit `480e531e415518aa3d0f3e7c75788d0d547764dc`.
These references do not substitute for Atlas's own native readback.

Atlas's later [schedule clarification](https://github.com/jeremysecondstate/atlas-scout/blob/3db3ff4d6e750fd9329e14082a5cbce9a1f4c688/system-details/atlas/scheduled-tasks.md)
and [workflow/reader follow-up](https://github.com/jeremysecondstate/atlas-scout/blob/3db3ff4d6e750fd9329e14082a5cbce9a1f4c688/system-details/atlas/nightly-workflow.md)
at commit `3db3ff4d6e750fd9329e14082a5cbce9a1f4c688`, published in
[PR #14](https://github.com/jeremysecondstate/atlas-scout/pull/14), were also read.
Their October 11 01:30 readback supersedes the older main/noon snapshot and
agrees with this audit's weekday schedules. The reader-source follow-up is a
separate reviewed UI/source transition; schedule changes do not explain those
reader hashes or prove a new runtime completion.

| Topic | Earlier description or comparison | Verified Atlas state and disposition |
| --- | --- | --- |
| Preparation eligibility | Older Atlas chapters say daily | All five preparation checkpoints use Monday-Friday. The reviewed handoff is already applied; no update required. |
| Machine-specific times | Scout uses 21:05 Stats/model/Gameplan and a 04:05 datastore fallback | Atlas retains 03:00 Stats/model/Gameplan, 21:05 datastore and 03:35 display. No Atlas 04:05 fallback definition was found; none was created. |
| Handoff and reconciliation | Preserved noon snapshot says paused; older runbooks say five minutes | Both original tasks and their existing companions are active, every twenty minutes within the verified windows below. |
| Reconciliation gate | Older ordinary-review gate is 01:25 | First eligible evening wake, nominally 21:00, owns the once-per-operating-evening review; after-midnight continuation retains that evening's date. |
| Loop A completion | Core COMPLETE can be mistaken for all data/history work | Core generation, subsequent OPRA/catalog maintenance and stock-target history are distinct boundaries. |
| Loop B scope | Older stage-2 or model-task wording can imply all training/planning is B | Stats/review precede B; Gameplan evaluation/publication, enrichment and planning follow distinct boundaries. |
| Cleanup | Enabled Windows helper can imply current Codex chats are covered | Installed exact-five-minute predicate rejects both allowlisted twenty-minute rules. Companions are outside the allowlist. Coverage remains incomplete. |
| Windows cadence | Five-minute watchdog can be mistaken for a Codex recurrence | Windows dispatcher and cleanup timers remain separate five-minute support schedules. |

## Verified current Atlas schedule

All rows below are **ACTIVE** native Codex definitions. Pacific means
`America/Los_Angeles`, following PST/PDT. Native recurrence strings have no
embedded `TZID`; October next-run readbacks agree with PDT. A future native
scheduler DST transition was not exercised by this audit.

| Responsibility | Native recurrence, Pacific | Next nominal launch at the observation |
| --- | --- | --- |
| DATASTORE CATCH-UP | Monday-Friday 21:05 | Monday, October 12, 21:05 |
| GAMEPLAN STATS | Monday-Friday 03:00 | Monday, October 12, 03:00 |
| MODEL REVIEW/TRAINING/PREDICTING | Monday-Friday 03:00 | Monday, October 12, 03:00 |
| GAMEPLAN | Monday-Friday 03:00 | Monday, October 12, 03:00 |
| DUCKETZ DISPLAY | Monday-Friday 03:35 | Monday, October 12, 03:35 |
| Joint Gameplan Handoff | Monday-Friday 21:00-23:40 every twenty minutes | Monday, October 12, 21:00 |
| Joint Gameplan Handoff After Midnight | Tuesday-Saturday 00:00, 00:20, 00:40 | Tuesday, October 13, 00:00 |
| Priority Source Reconciliation | Monday-Friday 21:00-23:40 every twenty minutes | Monday, October 12, 21:00 |
| Priority Source Reconciliation After Midnight | Tuesday-Saturday 00:00, 00:20, 00:40 | Tuesday, October 13, 00:00 |

The five preparation supervisors retain `gpt-6-luna` / low; both Handoff and
Reconciliation pairs retain `gpt-6-astra` / ultra. The substantive post-Stats
review remains separately configured. Across the full Ducketz-related inventory
there are fifteen definitions, ten active and five paused. Trader Representative
is the tenth active definition and is outside the numerical-loop schedule
changes audited here. Legacy Loops Overnight Gameplan and the historical
recovery follow-up remain paused. No replacement tasks were created.

Native metadata distinguishes nominal checkpoints from assigned launch times:

| Responsibility | Next assigned launch, October 12 PDT | Difference from nominal |
| --- | --- | --- |
| DATASTORE CATCH-UP | 21:05:40 | 40 seconds later |
| GAMEPLAN STATS | 03:00:09 | 9 seconds later |
| MODEL REVIEW/TRAINING/PREDICTING | 03:02:15 | 2 minutes 15 seconds later |
| GAMEPLAN | 02:55:34 | 4 minutes 26 seconds earlier |
| DUCKETZ DISPLAY | 03:34:40 | 20 seconds earlier |

Both evening supervisors and both midnight companions had assigned times equal
to their nominal times. These are a dated scheduler observation, not a promised
execution order or proof that a future turn completed. Prerequisites, durable
workflow state and notification thresholds retain their own checks.
An early assigned launch does not bypass the actual dated 03:00 or 03:35 alert
gate. The metadata does not establish that either alert was or will be delivered.

## Responsibilities and executable boundaries

The installed Atlas configuration selects the XNAS archive stock-price source
and archive history. Private configuration, full native definitions, native
identities, operating paths and task memory stay local. The installed
`ml.nightly_dispatch` responsibility map and `ml.nightly_workflow._run_native`
establish the following boundaries:

| Responsibility or stage | Work and admission boundary |
| --- | --- |
| DATASTORE CATCH-UP | `loop_a_close_fetch`, including the orchestrator's bounded acquisition/history subprocesses, followed by the separate `stock_target_history` stage for Atlas's configured source. |
| Loop A core | Provider and calculated-feature cycle under the shared datastore lock; current `COMPLETE` or `FAILED` generation publication. OPRA/catalog follows the core publication and must also succeed for the bounded fetch stage to pass. |
| GAMEPLAN STATS | `prepare_stats` runs `gameplan_stats` for the completed source session. Immature outcomes and no-history baselines remain explicit. |
| MODEL REVIEW/TRAINING/PREDICTING | `model_review` consumes frozen Stats; `train_and_plan` runs directional B, Gameplan evaluation, Gameplan publication and stock enrichment. This responsibility is broader than Loop B. |
| Directional Loop B | `loop_b_directional_generation` invokes `ml.prediction_runtime --once`. It takes the shared datastore-cycle lock, requires the current Loop A record to be COMPLETE and uses its finish-time availability cutoff. |
| Subsequent numerical work | `gameplan_evaluation`, `gameplan_publication` and `stock_enrichment_training` are distinct stages after directional B. Stock-only mode omits Strategy-profit training and Strategy generation. |
| GAMEPLAN | `local_gameplan` starts at `gameplan_trade_planning`, bound to the exact accepted prediction publication. |
| DUCKETZ DISPLAY | `verify_display` and `local_handoff` verify the exact saved local Stats/plan and prepare the local package; combined-reader verification follows the returned handoff. |
| Synthesis and adoption | Scout numerically synthesizes the packages. Atlas's Handoff responsibility delivers, verifies and adopts the exact returned combined Gameplan, Stats and receipt. |
| Reconciliation and cleanup | Reviewed source intake/repair coordination and completed-chat housekeeping respectively; neither is a numerical worker or trading executor. |

Code reviewed in Atlas's application checkout includes
`datafetching/orchestrate.py` (core completion then history exit),
`ml/overnight_runtime.py` (stage ordering and executable commands),
`ml/prediction_runtime.py` (current-cycle admission), `ml/nightly_workflow.py`
(split segments, immutable pins and receipt reuse), and `ml/nightly_dispatch.py`
(owners and session selection). The inspection used actual working bytes,
including independently inventoried local changes; the application HEAD alone
does not identify all installed behavior. Publication of this report does not
install those runtime sources elsewhere.

## Calendar, continuity and recovery

`ml.gameplan_actuals_review.completed_session_context` uses XNYS and the
completed 17:00 Pacific action day, then selects the next exchange session.
`ml.nightly_dispatch.intended_session` binds 21:05 on that source-session date
to 04:00 on the successor action date using `America/Los_Angeles`.

Friday, October 9 preparation and Saturday, October 10 after-midnight wakes
therefore retain source October 9, action October 12 and the original Monday
04:00 PDT deadline. Monday's 03:00 and 03:35 checkpoints inspect the same
intended session. Monday evening selects Tuesday; a 21:00 supervisor wake is
before that night's 21:05 preparation kickoff. On holidays the application
calendar selects the actual next session. Weekday recurrence is an outer
eligibility window, not a substitute calendar.

The final companion launch is 00:40; an already running turn can finish later.
The companion uses the prior operating-evening identity for once-per-evening
coordination. Dated 03:00 readiness-risk and 03:35 missed-confirmation ownership
and deduplication remain intact: **GAMEPLAN STATS owns 03:00 readiness-risk**
and **DUCKETZ DISPLAY owns 03:35 missed-confirmation**. There is no new Sunday-evening supervisor
window and no new Atlas fallback schedule.

Missed-night recovery retains the original action date and 04:00 deadline,
records its fixed expiry within the existing seven-hour and 17:00 Pacific
limits, and resumes only unfinished work. Completed native receipts and frozen
source/prediction bindings remain authoritative; a later wake or source edit
does not reopen a completed session. This audit neither renewed a deadline nor
modified any completed-session receipt.

Loop A's internal OPRA gate is a separate clock: UTC date/hour plus in-process
attempt memory. Its configured 00:00 UTC threshold is 17:00 PDT or 16:00 PST;
it does not implement the outer Pacific kickoff or durable global daily deduplication.

## Windows support tasks and cleanup limitation

Windows reports Pacific time with DST support. The enabled nightly dispatcher
retains a floating local daily 21:05 trigger, an elapsed five-minute timer and a
logon trigger, with StartWhenAvailable and IgnoreNew. The enabled completed-chat
cleanup retains its separate five-minute timer and logon trigger. The historical
independent-stock-session registration remains disabled. A support invocation's
success code does not establish numerical-loop, cleanup or trading completion.

Atlas's installed cleanup helper was inspected independently of Scout. Its
private allowlist contains the original Handoff and Reconciliation identities;
the installed cadence predicate accepts an exact five-minute cron rule.
Offline predicate checks rejected both current twenty-minute weekly rules and
accepted the historical five-minute control. The after-midnight companions are
not allowlisted. The enabled timer therefore does not establish cleanup
coverage for these current tasks. This report records the gap without changing
the helper, allowlist, minimum one-hour completion age or native chat state.

## Validation and remaining limits

The pinned coordination release
`7bd3da6cd674d78a6aafa01e1d4ecf3fbe66384a` was verified before work; Atlas's
local profile, ownership and reviewed publication base were captured privately.
Full saved/native definition comparisons reported no mismatches across the
fifteen definitions. Windows XML, local configuration, code boundaries and
cleanup predicate evidence were inspected without launching their workers.
A final native recheck at **13:10:28 PDT** confirmed all fifteen definitions
were unchanged. Full definitions remain local.

Offline checks cover Friday-to-Monday selection, after-midnight continuity,
Monday pre-kickoff/at-kickoff behavior and a Friday-PDT to Monday-PST deadline.
Existing focused dispatch/supervision tests also passed for responsibility
boundaries, completed-stage reuse and immutable recovery expiry. The existing
Markdown checker validates the selected documentation's encoding, whitespace,
fences and portable link syntax; it does not attest linked content or runtime
completion.

Native scheduler DST transition behavior remains unexercised. This audit does
not claim a newly completed preparation, synthesis, adoption or trading session.
No providers or brokers were called, no models were trained, no predictions or
synthesis were generated, and no application was restarted or deployed. All
full definitions and operating evidence remain private; this publication contains
only reviewed schedules, architecture, discrepancies and validation conclusions.
