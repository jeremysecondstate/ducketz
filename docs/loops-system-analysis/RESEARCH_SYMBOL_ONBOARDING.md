# From a Sunday research memo to the Loops stack

The Sunday researcher publishes evidence and maintains its recommendation ledger.
The operator then selects companies to onboard. Publication alone never changes
the production universe. A selection can come from a watch memo; onboarding does
not upgrade its investment assessment or claim that a recommendation qualified.

The reusable entry point is `python -m datafetching.research_onboarding`. It adds
all selected companies as one candidate batch, runs the existing native stock
pipeline, verifies the outputs, and activates the complete batch atomically.
`datafetching/watchlist.txt` remains the production authority. Every consumer and
Scheduled task must read it rather than maintain a separate literal symbol list.

## Plan an explicitly selected batch

Use the repository virtual environment in `C:/dev/ducketz`. Read the current
overnight status and follow the supervision claim procedure in
[NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md#active-overnight-supervision). Leave any
healthy existing worker intact. Carry out preparation after the action close
and before the next exchange session's 04:00 Pacific publication deadline.

```powershell
.\.venv\Scripts\python.exe -m datafetching.research_onboarding plan --edition 2026-09-13 --symbols CROX PATH TWST IONQ --reference COST --datastore-target pc --output artifacts/analysis/research-onboarding-20260913/plan.json
```

For a future Sunday edition, substitute its date, the operator-selected symbols,
and a new output directory. `plan` verifies the immutable research edition and
snapshot checksums, requires a company memo for every selection, verifies the
company CIK against FMP, and records the research publication digest. It performs
current-ticker inception checks against Databento symbology: a SPAC predecessor's
profile date cannot pull predecessor prices into the selected company's series.
For example, IONQ begins October 1, 2021, despite FMP's January 4 SPAC date. It uses
provider metadata preflights and writes a reviewable plan; it does not register
an active bootstrap, change membership, or submit data download jobs.

The default historical policy is `standard-included-history-2018-v1`:

- No historical request begins before January 1, 2018.
- Stock and option history begins at the later listing date or actual schema
  availability. L0 uses that full period; L1 uses the included last 12 months.
  L2/L3 and equity auction imbalance use the included last calendar month.
- All Databento acquisition quotes must be **$0**. A saved incomplete request is
  quoted again before acquisition/resumption. If it has aged into paid history,
  resolve its remaining included scope; do not silently authorize the charge.
- The default batch ceiling is 300,000,000,000 uncompressed billable bytes.
  The existing `5 GiB + 2 × billable bytes` free-space check also applies.
- Shared CME, FRED/ALFRED, and FMP commodity context are reused. Expanding the
  shared CME archive is a separate maintenance scope, not a per-stock copy.

The plan contains the prior universe, complete candidate universe, per-symbol
canonical requests and IDs, budgets, company identity, and research lineage.
Checksums bind the batch to its child plans. Do not edit an existing plan to
change its scope. Merely creating a plan is not authorization for a Scheduled
task to execute it.

## Acquire, train, verify, activate

After the operator selects the batch, run its saved plan:

```powershell
.\.venv\Scripts\python.exe -u -m datafetching.research_onboarding run --plan artifacts/analysis/research-onboarding-20260913/plan.json
```

The process acquires and renews the existing supervision lease every 30 seconds
and holds the research-batch lock. An operator already holding a lease may pass
their own `--owner-token` UUID; never reuse a different operator's token. The
claim is released on exit. A vanished process stops renewing it.

The same workflow has separate `fetch`, `train`, `validate`, `activate`, and
`status` commands for supervised maintenance. `fetch` prepares reusable OPRA
and XNAS batch jobs while the provider processes their histories, invokes the existing native Databento/FMP/Schwab/SEC collectors,
and writes verified stock/options archives. It records the explicitly started
batch under `state/research-onboarding` and each child under
`state/symbol-onboarding`. Persistent checksummed policies under
`state/symbol-history-policy` retain the 2018 floor for later FMP/Schwab fetches.
Other symbols' existing historical policies remain unchanged.

`train` completes the company archive: 2018-onward SEC submission metadata and
complete submission files, compressed with per-file checksums, plus FMP daily
prices. It checks secondary-price quality and operational-bar coverage before
running the normal native pipeline with the candidate watchlist inherited only
by child processes:

1. Loop A close fetch and production OPRA catchup.
2. Loop B directional training and predictions.
3. XNAS stock-target history and cumulative Gameplan evaluation.
4. Stock-only Gameplan training/publication for independent 1h/4h/1d/1w targets.
5. Independent stock enrichment training from the exact published cohorts.
6. Account-aware trade planning and completed-session actuals review.

This is the current stock-only preparation mode. Options data is collected and
retained, while option-strategy rows remain the normal `NO_TRADE_STOCK_ONLY`
placeholders. Paused options Strategy jobs and legacy traders remain paused.
Historical research documents do not become invented point-in-time features;
normal feature admission and availability timestamps still govern training.

Large second-level quote archives also use daily-split provider batch delivery.
Each day retains its native source, normalized data and exact validation. A day
above the 20-million-record target uses the existing deterministic intraday
download/split path; the 25-million-record validation maximum is unchanged.
Previously completed intraday segments are verified and reused on resume.

Validation runs in a fresh process with the exact candidate universe. It requires
the verified current Gameplan, four independent enrichment fits bound to its
cohorts, the complete native overnight receipt with zero orders, and 24 unique
routes per symbol in both forecast and intent tables. Activation additionally
verifies the complete account-aware trade plan, the overnight stage/log checksums
and its pinned Gameplan. It rejects changed current publication/model pointers
between validation and activation. Activation also
requires every selected company's history, corporate archive, operational-bar,
and secondary-quality receipt. It rejects an independently changed production
watchlist and appends the entire batch in one atomic update.

For eleven symbols the expected publication contains **264 forecasts and 264
options placeholders**, with **33 production OPRA cursors**. Use `24 × N` and
`3 × N` for later universes. Evaluate older six/seven-symbol publications against
their own saved manifests; never rewrite their membership or outcomes.

Activation admits companies to the operational universe. Model assessment,
promotion, live controls, capital limits, the selected trading policy, and the
user's manual initial trader-start requirement remain in force. Onboarding
commands do not place, cancel, or replace orders and do not start a live trader.
Fitted or published models can remain research/shadow when their assessments
do not qualify.

## Resume and Scheduled-task ownership

A living batch/native worker with advancing logs or CPU/I/O is supervised rather
than duplicated. Inspect the batch `progress.json`, per-symbol progress, native
locks, batch job states, and `overnight-run.json`. `status` reports the saved
phase, validation, and activation receipts.

Resume data acquisition using the same `fetch --plan ...`; native receipts,
checksums, and provider job IDs are reused. Corporate submissions reuse their
verified files. Never delete locks, reset cursors, or relabel missing data.

A failed native overnight attempt must be diagnosed and, if its supervisor died,
recovered through `ml.overnight_runtime` before resuming. Use the batch's exact
candidate environment and recorded run. After a verified repair or new provider
availability, continue with:

```powershell
.\.venv\Scripts\python.exe -u -m datafetching.research_onboarding train --plan artifacts/analysis/research-onboarding-20260913/plan.json --resume-run C:/DATASTORE/ml/overnight-runs/EXACT_RECORDED_RUN
.\.venv\Scripts\python.exe -m datafetching.research_onboarding validate --plan artifacts/analysis/research-onboarding-20260913/plan.json
.\.venv\Scripts\python.exe -m datafetching.research_onboarding activate --plan artifacts/analysis/research-onboarding-20260913/plan.json
```

The native resume retains completed stages, pinned sources, and its original
deadline. Do not rerun successful training to retry an unchanged downstream
failure. A completed bound attempt is reused. The same 04:00 deadline applies;
missing a deadline does not authorize intraday publication.

- **Loops Weekly Opportunity Research:** publish Sunday research and explain
  which memo symbols could be selected. Link to this path. Do not create or
  execute an onboarding merely because the report found a company.
- **Loops Operations Watch:** continue an explicitly started registered batch
  when unowned, after claiming supervision and examining its exact progress.
  This is an exception for authorized unfinished onboarding, not permission for
  routine new onboarding or speculative model development.
- **Loops Overnight Gameplan:** avoid a second pipeline while a batch owns
  supervision. After activation, use the production watchlist normally. An
  unfinished batch uses its own candidate file and preserved native attempt.
- **Weekly Review and Daytime Supervision:** read dynamic current membership,
  retain each saved plan's universe, distinguish onboarding/model readiness from
  execution permission, and preserve the existing schedules and operating modes.

## September 13 batch

The operator explicitly selected CROX, PATH, TWST, and IONQ from the published
September 13 edition. Evidence and logs are under
`artifacts/analysis/research-onboarding-20260913`.
The saved metadata preflight totals 230,492,746,800 uncompressed billable bytes,
quotes $0, and requires 466,354,202,720 free bytes under the existing capacity
policy. Consult its activation receipt for completion; a submitted batch job
or running fetch is not activation.

Seven related Scheduled task prompts were updated through the app and verified
against saved before/after snapshots. Their recurrence, active/paused state,
model, reasoning effort, project/thread target, and notification settings were
preserved. The two paused date-specific follow-ups were left intact. See
`schedules-before.json` and `schedules-after.json` in the evidence directory.
