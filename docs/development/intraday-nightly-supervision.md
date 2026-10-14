# Overnight 15-minute model supervision

Jeremy authorized this workflow on October 14, 2026. The native scheduled
supervisor uses **GPT-6.1 Sol, Ultra reasoning**, Monday through Friday at
01:00, 01:30, 02:00, 02:30, 03:00 and 03:30 **America/Los_Angeles**. Each PC
uses its own installed coordination profile, watchlist, authenticated data
connection, prediction store and operating bindings. Atlas and Scout remain
independent. A local task registration is not evidence of registration on a peer.

The supervisor invokes the deterministic local workflow:

```powershell
.venv/Scripts/python.exe -B -m ml.intraday.supervision --execute
```

Without `--execute`, this command performs an offline scope/schedule inspection
with no data request, model fit, policy installation or state-directory creation.
`--force` permits an explicitly authorized catch-up outside the normal window.
The scheduled task may use catch-up after a delayed wake; a missed window does
not cancel unfinished work. Preserve existing decisions and installation receipts.

Each wake refreshes canonical **EQUS.MINI OHLCV one-minute data** through the
existing batch continuation/persistence helpers, including their one-hour
overlap and deduplication. It then audits exchange-calendar coverage, the latest
completed bar, the most recent completed trading session and unresolved gaps.
The current models use only causal features derived from these bars: price,
volume, trailing indicators, elapsed gaps and session time. New external inputs
would require their own availability/freshness audit before admission. No
options, CME, account or unrelated provider endpoints belong to this refresh.

Model search compares a fixed, bounded set of real implemented adapters:
regularized logistic models, smoothed Gaussian naive Bayes, shrunk LDA,
uniform-neighbor KNN, shallow forests/extra trees, regularized histogram and
ordinary boosting, and small one/two-layer MLP architectures. Optional XGBoost
and LightGBM candidates run only when installed. Unimplemented neural adapters
are not represented as trained candidates. Selected specifications retain the
same recent-row training limit used during assessment.

Earlier sessions supply expanding chronological validation folds. Each fit
purges overlapping 45-minute target windows and labels received after its
cutoff. Imputation/scaling and the base estimator fit only the earlier training
partition. A later disjoint partition fits a sigmoid probability calibrator.
The search ranks proper probability scores, checks calibration and extreme
errors, compares simple baselines and the incumbent, and records accuracy as
additional evidence. It freezes a diverse shortlist before evaluating the most
recent completed session. That session is a final gate, never a source of
replacement candidates. Minimum rows, classes and calibration evidence are
required. A failed statistical gate retains the incumbent.

The per-session decision is persisted once per local PC and symbol. Later
half-hour wakes refresh data and review results while reusing that decision,
preventing repeated tuning on the same held-out day. Technical failures retain
their evidence and resume. A nonblocking supervisor-only lock prevents duplicate
searches. A soft 24-minute wake budget checkpoints remaining symbols for the
next wake; individual searches have a bounded fit/row/candidate budget. A single
estimator fit already in progress can exceed the soft time limit. This is not
a guarantee that missing provider data or insufficient history can be repaired
before 04:00.

Completed-session assessment waits for a successful scoped refresh whose
advertised source availability reaches that session's close. It does not wait
for today's premarket bar. Advertised availability does not prove observations
exist in every minute: coverage gaps remain explicit and all sample, class,
calibration and held-out gates still apply. When the intended completed session
has no matured labels, the supervisor freezes an insufficient-data decision for
that session and retains existing settings; it never substitutes an older
held-out day. Typed sample insufficiency is terminal statistical evidence;
integrity errors remain resumable failures. The report's completion scope is
completed-session assessment only. Current input freshness remains visible
separately, and completion does not establish live trader readiness or relax any
manual-start, acquisition or live-cycle gate.

Qualified model **specifications** install atomically under
`DATASTORE/intraday/supervision/active/`. Immutable revisions remain under
`policies/`, final decisions under `decisions/`, and dated audit logs under
`runs/`, with `latest.json` as the latest local report. These files and fitted
models are private operating state; they do not enter the shared-source queue.
An installation receipt is separate from source publication or a running
process's loaded version.

An explicitly configured manual live caller can enable supervised settings.
This source publication does not configure or start a trader. Each new live cycle
freezes its selected specifications, active-policy identity and evaluated
synthetic-label/history-window scope before constructing its causal dataset.
The active policy carries only these scope fields from the frozen assessment
manifest. Historical v1 policies without them retain their actual evaluated
scope: synthetic labels excluded, with all available history. Symbols without
a qualified policy retain the explicit configured defaults.

Recovery uses that cycle's original frozen selection and dataset scope. Existing
cycles from before scope binding retain their saved configuration for unfinished
symbols, and existing input snapshots retain their actual bytes and population.
Recorded dataset scope describes the actual snapshot. A new policy affects
subsequent cycles in a process that has loaded this implementation. Existing
historical/research runs retain their explicit configuration. Missing qualified
policies retain the configured defaults; a corrupt or incompatible policy is
an explicit error. No stale fitted model or fabricated probability substitutes
for a failed fit.

Research and supervision membership comes only from this PC's installed
profile and its pinned watchlist, in profile order. Other traders' account
configuration is not read and cannot block this independent workflow. Scout
retains its existing supervisor entrypoint, per-symbol policy store and native
task; incoming source does not register another supervisor or change its state.

Cycle resource measurements record the current process's CPU time and memory,
plus aggregate host CPU and available memory. They do not enumerate other
processes or read command lines, trader identities or another trader's state.
Saved historical measurements retain their original fields and bytes.

The prior-session review reads only this PC's saved live forecasts, observed
outcome revisions and latest execution projections. It records missing outcomes,
late issues, Brier score, accuracy, extreme errors, submission uncertainty and
available submitted/filled quantities. Unknown realized profit remains unknown.
Prediction correctness and reference-price returns are not trading profit.
The workflow does not start/restart a trader, clear a manual stop, submit orders,
or change account authority. Jeremy retains the manual trader start.

The screenshot's 100% readings are individual forecast probabilities. They are
not measured accuracy and do not establish overfitting by themselves. The target
includes unchanged prices and compares the third close with the reference open,
after the first close has been observed. Chronological assessment, calibration,
class balance and actual outcomes determine whether those probabilities deserve
confidence. Historical source availability assumptions remain explicit in every
dataset manifest and do not establish a point-in-time replay claim.
