October 1 fallback-enabled preparation: offline final verification

Expected original native run: <LOCAL_DATASTORE>/ml/overnight-runs/20261001T040644.667536Z
Source session: 2026-09-30. Action date: 2026-10-01. Original deadline: 2026-10-01T11:00:00Z.

After matching native stage-report.json and receipt.json both become COMPLETE,
run from <LOCAL_CHECKOUT> with the repository virtual environment:

  .\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260930/fallback-enabled-verification/run_final_checks.py --overnight-run <LOCAL_DATASTORE>/ml/overnight-runs/20261001T040644.667536Z

If native work required a valid resume, pass the terminal COMPLETE attempt to
--overnight-run; the audit follows its ancestry and still requires the original
run/source/deadline. The operator continues renewing its own supervision claim
and monitoring native logs. This helper does neither and has no process control.

Do not run prepare_helpers.py again: it refuses overwriting these helpers and
must not silently establish a new source baseline. run_final_checks.py refuses
an existing audit-runner.json. Diagnose a failed audit and preserve its files
before any explicit rerun. No heavy final checks have run during preparation.

Outputs:
  audit-runner.json - sequence, statuses, implementation before/after, evidence hashes
  completion-audit.json - native report/receipt/log ancestry, source/OPRA scopes,
    configured-universe 24*N forecasts/intents/trade rows, stock source archives,
    archive features/cohorts/seconds consistency, sizing admission, saved planning
    price/reference reconstruction, cumulative evaluation and prior-session actuals
  yg-completion.json - saved fitted-model inference, score reproduction,
    development-selection/grid/calibration, exact history support and honest gates

Fallback additions bind gameplan/manifest/receipt and trade-plan report/manifest/
receipt/ledger to hierarchical-bearish-fallback-v1. Complete planning is rebuilt
from its saved account/ownership snapshot using the exact policy. The independent
rollforward checks initial donor hashes, half-of-initial symbol/donor caps, all
18 integer-weighted fallback quotas, no reset or rollover, unique triggering
forecasts, longer donor attribution, genuine pre-clock normal-route absence,
same-clock bullish/normal-selling and pending-donor exclusions, and final horizon
shares. Ordinary sales are not charged to fallback limits. Every event/hour/end
cash and symbol-share balance retains the original independent conservation checks.

helper-review-tests.json records syntax checks, current exact APPLIED deployment
and 341-file baseline validation, early-COMPLETE refusal, synthetic pure-planner
positive cases and negative quota/donor/cap/reset/horizon/expiry cases. Synthetic
fixtures are helper tests only, never training or production records.

Limits and separate required evidence:
  - No live fallback baseline or fill evidence is inferred from planning. Run the
    documented read-only fallback_review after native preparation/actuals, using
    --action-date 2026-09-30 and a fresh fallback-session-reviews output directory.
    An absent September 30 baseline is expected and is not zero sales evidence.
  - Provider-warning exact interval/condition/range follow-up, if needed, remains
    separate operator evidence; this helper makes no provider requests.
  - Controls, schedule and live ownership/ledger preservation are separate root
    pre/post evidence. This helper has no authority to trade or deploy.
  - Local raw/normalized hashes and native request headers are checked. The
    seconds/minutes check does not replay every raw DBN record.
  - Research model groups, sparse/missing actual prices and unavailable planning
    references retain explicit saved statuses. An audit pass does not promote a
    model, establish realized profit or change the approved 50% setting.
  - Deployment/Git verification and the Atlas notice remain root responsibilities.
    These artifact-only helpers are not a new source publication or ready record.

Older September 30 action evidence under ../verification is preserved.
