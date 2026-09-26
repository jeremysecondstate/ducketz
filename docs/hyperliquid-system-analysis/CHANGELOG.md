# Hyperliquid system-reference changelog

This records changes to the maintained system contract. Runtime events and
performance history belong in their own journals and dated evidence.

## 2026-09-26 — Broader Paper entries and recorded decision checks

Changed the active Paper entry band from five to four percentage points
(55%/45% to 54%/46%) under the user's forward-evaluation direction. Qualified
models, risk limits, costs and training cadence remain required; the existing
ledger/seed continue with a new policy ID. No fabricated fills or reset.

Fixed misleading generic Hold/Skip explanations. New decisions save exact
entry/exit, account, size, execution and cooldown checks; flat cooldown holds
now identify the actual blocker without changing retry behavior. The UI reads
those saved checks and uses only available historical facts for older rows.
See the [decision-gate audit](audits/2026-09-26-paper-decision-gates.md).

## 2026-09-26 — Bounded model comparison and Paper refresh

Archived the previous qualified-only Paper ledger and model history, ending at
-$45.46 P/L including $32.87 simulated fees. Tested two predeclared recent-fit
recipes with unchanged qualification thresholds. Both increased qualification
counts but worsened mean Brier/log loss; neither was promoted. Added a tested,
optional fitting-window cap with an uncapped default; active training settings
remain unchanged.

The user-requested fresh public mirror opened at 10:35:59 UTC with $42,091.465039
and nine inherited positions. Qualified-only Paper and 15-minute model fitting
continue. Updated watch baseline to contract v3, preserving archive evidence and
intentional-stop rules. **359 relevant tests passed**. See the
[comparison and refresh audit](audits/2026-09-26-model-comparison-and-paper-refresh.md).

## 2026-09-26 — Model retraining on each 15-minute candle

Changed the accepted model cadence from 3,600 to 900 seconds at the user's
request. Gracefully restarted only the model worker with an independent hidden
Windows launcher. Data and Paper owners continued; Paper's seed, baseline,
qualification gate and policy ID were preserved. Each new completed candle can
now trigger fitting; unchanged data does not trigger repeated fits.

Candidate-freshness display now follows the running model cadence (30-minute
stale threshold for the current 15-minute schedule), with the historical hourly
fallback for older status files. Removed the hard-coded hourly loading label.
Qualification thresholds, assessment/calibration partitions and immutable
forecast behavior are unchanged. More evaluations do not guarantee more
Qualified models or more independent evidence.

**215 relevant tests passed** across model configuration/runtime/artifacts,
the Paper view adapter and workspace. Verified all four new candidates,
accepted `retrain_seconds=900`, independent launcher ownership and advancing
Paper observations. See the [cadence-change evidence](audits/2026-09-26-model-cadence-15m.md).

## 2026-09-26 — H.Y.P.E.R. resize callback repair

Reproduced the reported Tk `pack`/`grid` conflict when returning from compact to
wide layout. Forecast, detail and policy panels now use `grid` in every layout,
with row/column sizing reset at each transition. Resize callbacks also tolerate
view destruction during idle processing; the layout cache updates only after
successful placement. Runtime workers and trading policy are unaffected.

The regression failed with the original traceback before the fix. **27 tests
passed** across the workspace and desktop mounting suites, including repeated
wide/compact/narrow transitions and destruction during resize. Workspace tests
now capture Tk callback exceptions so printed callback failures cannot silently
pass. Wide and compact offline fixture renders were also inspected.

## 2026-09-26 — Fresh qualified-only Paper experiment

Archived the entire gracefully stopped mixed-model experiment, including its
ledger, policies, funding cursor and logs, with source/configuration snapshots
and a SHA-256 manifest. Started a new read-only mirror seed at 07:42:57 UTC with
pooled opening equity **$42,071.6907597654** and all nine inherited positions.

Enabled `require_qualified_forecasts=true`. The new Paper recipe holds existing
targets without a valid Qualified signal, retains stop/cooldown/exposure-limit
reductions, and separates rejected model provenance from trade attribution.
Shared forecast validation corroborates the active eligible model and rejects
malformed forecast IDs before allocation. The previous mixed-mode fallback is
preserved when the qualification flag is false. Powder remains inactive; its
existing no-signal fallback was not changed.

Added explicit Paper fill reasons and No forecast/Risk exit labels to H.Y.P.E.R.
Updated operating documentation and watch recovery expectations for the new
seed and policy. **379 distinct relevant test cases passed** across the initial
regression selection and final reader/runtime checks. See the
[experiment evidence and limitations](audits/2026-09-26-qualified-only-paper.md).

## 2026-09-26 — Scheduled Hyperliquid Operations Watch

Created the separate local app automation `hyperliquid-operations-watch`, active
every 30 minutes using GPT-6 Luna / Extra High. Added the maintained
[watch procedure](OPERATIONS_WATCH.md), conditional recovery of unexpectedly
terminated data/model/Paper workers, explicit intentional-stop handling and
read-only Powder monitoring. Local PC/app availability is required; this does
not install an operating-system boot service or activate real trading.

Verified the saved scheduler configuration and ran the documented read-only
projection against the current installation. At 07:14:38 UTC, all three owners
were running and the Paper ledger observation had advanced to 07:14:32 UTC;
its original opening seed/baseline remained intact. The 643-second performance
export age was ordinary export-only lag. Powder remained unactivated. No
worker restart was needed for the watch setup. Checked Markdown paths/fences
and whitespace; the earlier independent-launch audit supplies recovery-method
evidence rather than a new failure-injection test.

## 2026-09-26 — Paper session recovery

Restored data, model and Paper workers after the user-confirmed Codex restart.
Validated the existing ledger and preserved its original seed/history. Relaunched
workers outside the Codex process tree through Windows, with fresh forecasts
and UI health evidence. No real trading or startup schedule was enabled. See
the [incident audit](audits/2026-09-26-paper-session-recovery.md).

## 2026-09-25 — Explicit Powder activation path

- Added the separate Powder execution runtime, strict verified mainnet broker,
  durable client-order intent/reconciliation ledger and user launch/check/stop
  commands. Every session requires explicit activation; no scheduler or `.env`
  change was installed.
- Shared validated forecast reading and pure allocation policy with Paper,
  adopted matching existing positions, enforced actual account collateral and
  shared ownership locks with manual submit/modify/cancel operations.
- Added separate read-only Powder observations/intents/actual fills to H.Y.P.E.R.
  Actual fee tokens are preserved; P/L/drawdown/funding totals and automated
  transfers remain unavailable pending complete cashflow implementation.
- Added the [activation/recovery runbook](POWDER_ACTIVATION.md) and updated the
  index, map, inventory, functionality, monitoring, UI and maintenance contracts.
  Test evidence and limitations are recorded in the runbook. Verification uses
  fake exchange responses and temporary ledgers, not live execution.
- Final regression selection: **917 passed** across all Hyperliquid tests and
  both workspace/app integration suites. Normal/compact Powder fixture images
  were inspected; local saved status reported **NOT_ACTIVATED**.

This entry supersedes the earlier disconnected-Powder implementation boundary.
The earlier dated screenshots and initial reference entry remain historical.

## 2026-09-25 — Initial system-analysis reference

- Established this index and operating reference alongside the existing
  stock/options [Loops reference](../loops-system-analysis/README.md).
- Mapped the independent data, model and Paper owners, their publication
  dependencies, the one-time mirror seed, evaluation evidence and read-only UI.
- Documented model qualification versus profitability, Paper opening-baseline
  accounting, transfers, account roles, execution assumptions and risk limits.
- Added process/source/timing diagnostics, control side effects and recovery
  guidance. Kept runtime observations separate from permanent architecture.
- Recorded H.Y.P.E.R. Paper/Powder boundaries and the current fixed UI market
  and horizon scope; Powder remains disconnected.
- Added a change-impact matrix, update checklist, evidence format and existing
  test-family map for future maintenance.

Baseline review used the working-tree implementation/configuration at revision
`f4efaeb` plus the existing component and UI implementation guides. Verification
for this documentation change is source review and local Markdown link/path
checking: 202 local links across the 12 new documents and six linked entry
pages resolved, with balanced fenced code blocks. No worker lifecycle,
strategy, ledger or scheduling change is part of
this entry. Previous UI screenshots and test results remain dated delivery
evidence, not newly measured runtime performance.

For later entries, record the behavior/configuration change, affected pages,
checks actually performed and any unresolved limitation. Do not claim automatic
documentation updates or replace old observations with current numbers.
