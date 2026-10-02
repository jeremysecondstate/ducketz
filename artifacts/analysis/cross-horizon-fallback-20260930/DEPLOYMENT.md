# September 30 cross-horizon fallback handoff

**Rollout completed September 30 at 17:20 Pacific.** See deployment-status.json
and post-deployment-verification-attempt2.json: 23 installed files, 176 verified
dependencies and 678 passing tests. Both apply records are APPLIED; all native
locks and the supervision claim were released. Do not install the package again.
The earlier post-deployment-verification.json is a preserved pre-test verifier
failure caused by CRLF/LF comparison; the corrected verifier passed on attempt 2
without changing application source. Tonight's new Gameplan is still a separate
21:05 preparation step, with the original October 1 04:00 deadline.

The user explicitly approved a **50% combined daily cap** and preparation for
**next-session Gameplan execution**. They confirmed normal ownership plus the
hierarchy 1h→1h/4h/1d/1w, 4h→4h/1d/1w, 1d→1d/1w, 1w→1w, and acknowledged
smaller hourly fallback allowances using the existing horizon weighting. The
daily reset and unchanged normal own-horizon sales were disclosed. This is
future-session source work; it grants no authority to place orders or start,
stop, restart, or change the controls of today's trader.

The candidate is isolated at
`C:/Users/7980X/.codex/worktrees/cross-horizon-bearish-fallback/ducketz`.
The active source checkout remains `C:/dev/ducketz`. Deploy only the exact files
sealed in `candidate-manifest.json`, never copy the entire worktree or its index.
The package contains original `base/` bytes, final `files/` bytes, test evidence,
dependency hashes and a strictly guarded one-off `apply_candidate.py`.

1. Read the current NIGHTLY_GAMEPLAN operating procedure and this handoff.
   Generate a new supervision UUID, acquire with the native .venv command,
   proceed only on ACQUIRED, renew at least every minute, and release at the end.
   Inspect native status and Scheduled tasks; do not launch a competing pipeline.
2. Verify the candidate manifest, apply helper checksum and final tests. Confirm
   today's session is terminal and its processes are absent. Any live or waiting
   worker blocks deployment. Never stop it to make the deployment proceed.
3. Between September 30 17:00 and October 1 04:00 Pacific, run the guarded helper
   with this package and your own supervision UUID. It verifies exact source,
   dependency, backup and test hashes, holds the native session/cycle/overnight
   lock protocols, refuses foreign locks and source drift, and records a
   resumable receipt. No control, ledger, model or Gameplan is copied or changed.
   The helper rolls back its exact owned writes before releasing locks on a
   caught failure. Never start nightly preparation or a worker while installation
   records lack matching current APPLIED progress AND receipt records and
   verified final source/dependency inventory. Both records must bind this exact
   manifest; an older receipt never overrides a newer failed attempt.
   Any other state, including IN_PROGRESS, FILES_APPLIED_LOCKED,
   APPLIED_UNRECEIPTED or ROLLBACK_INCOMPLETE, needs inspection and recovery.
   An abrupt process/power loss needs
   inspection and exact manifest recovery under native ownership; Python cleanup
   cannot run after a killed process. Do not clear its locks to bypass recovery.
4. Verify every installed file against the manifest and run the same focused
   tests from the active checkout while the native session/cycle locks protect
   the test window. Take the pytest command from combined-verification.json,
   set its working directory to C:/dev/ducketz, and save NEW evidence binding
   active file hashes before/after. Do not run verify_candidate.py unchanged:
   that development script intentionally points at the isolated worktree.
   Retain original test and failed-baseline evidence. Release
   deployment locks before any normal overnight process attempts to acquire
   them. Do not broaden checks into a broker call, provider fetch or order.
5. Keep the existing 21:05 nightly command and its original deadline. Fresh
   October 1 publications bind the new policy; September 30 remains immutable.
   Verify Gameplan/manifest/receipt policy agreement, planning donor hashes,
   weighted quotas, shared/per-donor caps and all existing native final checks.
   Do not rerun the completed September 29 source merely to exercise this code.
6. After the usual next-session preparation and actuals tail, run the read-only
   fallback review for the just-completed action date into a fresh directory
   under `artifacts/analysis/fallback-session-reviews`. On September 30 an absent
   baseline is expected. On later enabled sessions report actual ledger-recorded
   fills, pending/unknown quantities and cap usage separately from normal sales.
   Do not infer profit, manufacture a counterfactual, or change the 50% setting
   without a new user instruction and prospective policy version.
7. The user explicitly requested communication to Atlas, then Scout through
   CODEXSTORE. Send the final deployed/staged status and verified package evidence
   to the current Atlas communication chat. After successful installation, inspect
   this package's `git-push-receipt.json` and verify its remote commit and exact
   source/test/doc inventory against the sealed candidate and installed hashes.
   If those 23 files are already verified pushed, send Atlas a deployment-status
   update referencing that existing commit and the new installation/test evidence;
   do not queue a duplicate source publication for the same 23-file candidate.
   A missing or unverified push receipt is an unresolved publication status to
   reconcile with Atlas, not proof of a push. Any genuinely new subsequent source
   repair still uses the ordinary Atlas ready-record policy for its exact tested
   changes. Preserve unrelated Hyperliquid work and the active Git index/branch.
   A Git notice is not peer deployment or trading authorization.

A source conflict, unknown process identity, active/waiting worker, missing test
evidence, or expired deployment window is a specific blocker. Preserve the
package and report it; do not overwrite a concurrent edit or label the new policy
active. The 21:05 owner may retry a previously blocked deployment only after the
blocking evidence changes. Once a matching deployment receipt and installed
hashes exist, do not apply it twice.

The Ducketz UI may need to be reopened after deployment to load its new display
code. Do not restart the active UI or trader as part of this handoff. The saved
Gameplan.md remains the readable source of the next plan.

## Verification notes

The initial combined test run found seven failures. A clean worktree at exact
base `0407388559d54612603380793fc26a98937697fc` reproduced all seven: five old
quote-clock fixtures expected automatic expiry sales even though the current
manual policy has no expiry liquidation; two September 8 publication fixtures
implicitly inherited the later raw-direction default without its model metadata.
The fixtures were corrected to exercise current bearish-instruction quote checks
and the explicitly historical label contract. Runtime behavior and model gates
were not weakened. Original combined failure and clean-baseline logs are kept.
