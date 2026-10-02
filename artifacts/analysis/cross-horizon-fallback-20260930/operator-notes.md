# Cross-horizon bearish fallback — September 30, 2026

Status: **DEPLOYED_VERIFIED** on September 30 at 17:20 Pacific. The exact tested
source is installed in C:/dev/ducketz. The October 1 publication remains the
separate 21:05 overnight task. No broker requests, orders, control changes,
production ledger changes or trader starts, stops or restarts were performed.

The user selected a 50% combined daily fallback ceiling, confirmed the ascending
donor hierarchy, approved smaller hourly allowances, and requested next-session
execution. Normal own-horizon/eligible unallocated selling remains first. The
new versioned publication policy, durable ledger reservations, runtime source
checks, hypothetical planning, UI explanations and read-only session review use
the same contract. See the packaged CROSS_HORIZON_BEARISH_FALLBACK.md for exact
integer quotas, donor restrictions and daily-reset semantics.

Implementation is isolated in the attached cross-horizon-bearish-fallback
worktree at base 0407388559d54612603380793fc26a98937697fc. The sealed package has
23 source/test/documentation files and 176 supporting source/configuration
fingerprints. Exact current bases, candidate bytes and test hashes are recorded
in candidate-manifest.json. Unrelated active Hyperliquid edits are preserved.

The final combined run completed September 30 at 20:40:45 UTC: **678 passed**,
zero failures, zero source drift and a clean diff check. The nine warnings are
existing joblib/NumPy shape deprecations. A prior run skipped one Tk widget
test due to a Tcl initialization failure; that check passed in the final run.
No live broker integration test was attempted.

Seven initial failures were independently reproduced on the clean base. Five
quote-clock fixtures still expected removed automatic-expiry selling; two old
publication fixtures inherited a later target default. They now exercise the
current bearish-instruction quote checks and explicit historical target
contract. No execution or model gate was weakened. Initial-failure and clean
baseline evidence is retained. The temporary baseline worktree was archived.

Review found and fixed planning parity for a donor with an external pending
buy. Independent deployment review also identified that the initial installer
could release locks after a partial write. The revised helper restores
its exact owned changes under the held locks on caught failure, preserves
concurrent edits, and records any incomplete rollback. Its final test run has
**56 passed, 2 skipped**; Windows denied two real symlink fixtures, while three
simulated reparse-point guards passed. Read final helper test and independent
audit evidence before rollout; do not use an earlier helper checksum. Abrupt
process/power loss cannot execute Python cleanup and requires inspection.

Deploy after today's trader is terminal and absent, under a new native
supervision claim and the session/cycle/overnight locks, following DEPLOYMENT.md.
A live or waiting worker blocks installation. Keep the existing 21:05 Pacific
nightly preparation and original 04:00 deadline. Future source publication,
installed verification and actual session results remain separate milestones.

Atlas received the user's authorized coordination request and the final sealed
package/test/rollout handoff for Scout through CODEXSTORE. Scout receipt has not
yet been independently confirmed. An ordinary Atlas
ready record must wait until the exact verified files exist in the active
checkout; no placeholder ready record or claim of peer deployment is valid.

The one-time post-close heartbeat `install-gameplan-fallback-after-close` is
saved for September 30 at 17:10 America/Los_Angeles in this chat. The existing
nightly automation remains 21:05 with its original settings and deadline.
Operations Watch and Daytime Supervision received the narrow future-source
precedence clarification; their schedules and models are unchanged. The app
refreshed Daytime Supervision's project roots to include the user's CODEXSTORE
folder. All saved prompts were read back and verified; details are in
automation-verification.json.

Final manifest SHA256:
`fec2fc4cecc2a7e5f181b3f0cf57ab0d9b3e0423d880cc5cbd330006532d5252`.
Read-only sealed-validation.json confirms all payload/base/dependency/test
bindings. No installer invocation or production lock acquisition occurred.

## Source published before deployment, by direct user request

The user subsequently authorized committing and pushing the source for Scout
and requested a greeting. The exact 23 tested files were independently audited,
committed in the isolated worktree and pushed to
`codex/cross-horizon-bearish-fallback-20260930` at
`be760388063a0fa3099c1cc445ba758be69ceb23`. The commit includes "Hey Scout! 👋".
The remote ref was verified September 30 at 20:58:07 UTC. Active main HEAD and
index hashes were unchanged across both commit and push. The extra untracked
planning-review artifact stayed local. Source bytes and sealed manifest remain
unchanged; see git-commit-receipt.json and git-push-receipt.json.

Atlas received the verified commit link and publication evidence for Scout.
The deployment handoff and both relevant saved automation prompts now reuse
that commit and send a later deployment-status notice, avoiding a duplicate
23-file publication. New source repairs still use ordinary ready records.
Schedules and every other automation field were preserved and read back.
This remains STAGED_NOT_DEPLOYED; the 17:10 guarded rollout check is unchanged.

## Post-close rollout completed

The September 30 session finished at 17:00 Pacific. Native process checks found
no live/waiting trader or overnight pipeline, and the legacy Windows scheduled
launcher remained Disabled. Own supervision UUID
161c058c-458f-41c9-8f09-ed7b74fdfbf8 was acquired at 00:11:18 UTC, renewed during
work, and released at 00:20:38 UTC on October 1. The claim remained valid;
manual renewal gaps reached about 69 seconds during preparation, after which
the test monitor renewed automatically at intervals below one minute.

The exact sealed helper installed all 23 files under native locks from
00:13:26 to 00:13:40 UTC. Matching current apply-progress.json and
apply-receipt.json are APPLIED. The first verification stopped before tests or
locks because its comparison normalized only the installed LF file against a
recorded CRLF hash. Exact original hashes and normalized equality confirmed
the cause. The local verifier now checks original recorded bytes and normalizes
both copies; application files and all gates stayed unchanged. The initial
failure, correction and checksum addendum remain saved.

Attempt 2 ran the exact recorded 27 test files from the active checkout under
all three native locks: **678 passed**, 9 existing NumPy/joblib warnings, in
98.66 seconds. Zero source drift; all installed and supporting hashes matched;
scoped staged/unstaged diff checks passed. The successful report is
post-deployment-verification-attempt2.json, completed 00:20:10 UTC. Both test
and installation locks were released.

production-preservation-after.json reports PASS for 25 protected datastore
files/absence checks, including controls, ownership ledger, session status and
the saved publication/pointers. Active Git HEAD/branch/index and 16 unrelated
dirty files are unchanged. The remote commit and exact source inventory were
verified again; no duplicate publication is needed. deployment-status.json
binds the final evidence. Atlas's current communication chat received the
deployment update for Scout referencing existing commit be760388. Peer receipt
and deployment are separate states. No new overnight run or trader was started.
