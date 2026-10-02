October 1 source / October 2 action: offline final verification

Original native run: C:/DATASTORE/ml/overnight-runs/20261002T040848.225355Z
Original hard deadline: 2026-10-02T11:00:00Z (04:00 Pacific).

Only after the matching native stage-report.json and receipt.json both COMPLETE:

  .\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20261001/verification/run_final_checks.py --overnight-run C:/DATASTORE/ml/overnight-runs/20261002T040848.225355Z

For a valid resumed attempt, pass the terminal COMPLETE run path. The verifiers
follow exact ancestry and retain the original run, October 1 source, October 2
action and original deadline. The root operator keeps renewing its own claim
and monitoring; these helpers have no process control or supervision authority.

The seven helper copies retain the preceding full audit's checks. Changes are
limited to this operation's paths, explicit run/date/deadline and fixture date.
preparation.json binds all source and adapted helper hashes. The fresh 341-file
baseline also verifies the exact candidate package, APPLIED current progress and
receipt, and all installed candidate/dependency bytes. It records two preexisting
Hyperliquid source changes since the prior audit, without modifying them.

helper-review-tests.json records 12 passing bounded checks: syntax, exact source
baseline and APPLIED state, early-COMPLETE refusals, independent donor/slot/cap
arithmetic, rejection of quota/donor/cap/reset/horizon/expiry tampering, and normal
sales remaining uncapped and excluded from fallback donors. These are synthetic
in-memory helper tests only; no production data or training rows were written.

Do not rerun prepare_helpers.py or replace its baseline: it refuses existing
helpers. run_final_checks.py refuses an existing audit-runner.json. Any failed
audit requires diagnosis and preservation of its evidence before explicit rerun.

Final outputs:
  audit-runner.json: sequential checks, implementation before/after, evidence hashes
  completion-audit.json: native eight stages and receipt/log ancestry; 24*N
    forecasts, stock-only intents and trade rows; 3*N OPRA scopes; stock history;
    archive history/causal features/source files/cohorts/seconds consistency;
    fitting versus sizing qualification; source-bound planning reconstruction;
    saved sparse close assumptions; cumulative own-universe evaluations; actuals
  yg-completion.json: fitted-model inference, saved score reproduction, exact
    symbol/route history, selection/calibration/grid and honest promotion checks

Fallback checks bind Gameplan/manifest/receipt plus plan report/manifest/receipt/
ledger to the exact policy; rebuild the plan from its saved account snapshot;
independently check donor hashes, half-of-initial symbol and donor caps, 18
integer-weighted slots across 24 units, no reset or rollover, genuine normal-route
absence, same-clock donor exclusions, donor ownership and cash/share conservation.

After the native preparation/actuals tail, run the documented local read-only
session review using a newly chosen UTC timestamp directory (do not reuse it):

  .\.venv\Scripts\python.exe -B -m ml.stock_trader.fallback_review --datastore-root C:/DATASTORE --action-date 2026-10-01 --output-dir C:/dev/ducketz/artifacts/analysis/fallback-session-reviews/NEW_UTC_TIMESTAMP

That command reads one SQLite read-only transaction and writes report/manifest
outside the datastore. Report ledger-recorded fills, outstanding/unknown quantities,
cancelled/rejected releases and cap use separately from ordinary sales. NO_BASELINE
is not evidence of zero sales. Planning does not establish a live frozen baseline,
actual fills or realized profit. No automatic adjustment to the 50% policy.

Separate root evidence remains required for provider-warning metadata, controls,
schedule and ledger preservation, Git publication and Atlas deployment notice.
Native raw/normalized hashes and headers are checked; every raw DBN record is not
replayed. Research/unsupported models and pending/missing actuals stay explicit.
These local helpers are operation evidence and create no new source publication.
