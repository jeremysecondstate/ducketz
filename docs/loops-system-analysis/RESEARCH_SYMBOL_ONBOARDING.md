# From a research memo to the Loops stack

Reviewed October 11, 2026: **mixed retained batch interface and dated rollout
history**. The explicit batch/receipt/activation contract remains implemented.
Its helper still invokes the native stock-only overnight stages and account
ownership preparation described below; it has not been rewritten here as a
Stats-first entry point. [Current system](CURRENT_SYSTEM.md) describes the newer
normal preparation and combined-account responsibilities.

**Unresolved cross-PC operating applicability:** the retained helper's ownership
preparation is not proof it is suitable for Scout's research-only role or Atlas's
broader combined execution binding. Review that local compatibility before a
new authorized run; this documentation audit performed no onboarding and changed
no provider, account or membership authority. The September batch remains useful
verified history, not a standing instruction to repeat it.

The historical Sunday researcher published evidence and maintained its
recommendation ledger. An operator explicitly selects companies to onboard;
the old Sunday cadence does not establish a current native schedule. Publication
alone never changes
the production universe. A selection can come from a watch memo; onboarding does
not upgrade its investment assessment or claim that a recommendation qualified.

The reusable entry point is `python -m datafetching.research_onboarding`. It adds
all selected companies as one candidate batch, runs the existing native stock
pipeline, verifies the outputs, and activates the complete batch atomically.
The durable production authority is `datafetching/watchlist.local.txt` when
present in this checkout, otherwise the shared `datafetching/watchlist.txt`.
Local research consumers must resolve this PC's configured watchlist rather
than maintain separate literal symbol lists. The combined executor's accepted
account-wide universe is a separate binding and must not be replaced with one
producer's research selection. Candidate runs can use
`DUCKETS_PRODUCTION_WATCHLIST`; activation still updates the durable production
list. See [Running Ducketz on two PCs](../multiple-pcs.md) for setup and precedence.

## Plan an explicitly selected batch

Use the repository virtual environment and resolve the installed owner/binding
through [current recovery guidance](CURRENT_SYSTEM.md#recovery-and-evidence-limits).
The retained batch helper uses the
[native supervision lease](NIGHTLY_GAMEPLAN.md#active-overnight-supervision).
Leave any healthy existing worker intact. Carry out preparation after the action close
and before the next exchange session's 04:00 Pacific publication deadline.

```powershell
.\.venv\Scripts\python.exe -m datafetching.research_onboarding plan --edition 2026-09-13 --symbols EXAMPLE_EQUITY EXAMPLE_EQUITY EXAMPLE_EQUITY EXAMPLE_EQUITY --reference EXAMPLE_EQUITY --datastore-target pc --output artifacts/analysis/research-onboarding-20260913/plan.json
```

For a future Sunday edition, substitute its date, the operator-selected symbols,
and a new output directory. `plan` verifies the immutable research edition and
snapshot checksums, requires a company memo for every selection, verifies the
company CIK against FMP, and records the research publication digest. It performs
current-ticker inception checks against Databento symbology: a SPAC predecessor's
profile date cannot pull predecessor prices into the selected company's series.
For example, EXAMPLE_EQUITY begins October 1, 2021, despite FMP's January 4 SPAC date. It uses
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

The same workflow has separate `fetch`, `train`, `validate`, `activate`, `finalize`, and
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
by child processes.

The candidate preparation also reads fresh broker holdings under the existing
stock-trader lock. When the saved ownership reconciliation is missing only the
new symbols, it appends the observed holdings through the native horizon ledger
with zero execution budgets. It requires a matching account, a previously ready
reconciliation, and no working orders, open reservations, or ownership blocks.
It preserves manual inventory as unallocated and does not start a trader.
Other discrepancies require the normal reconciliation workflow; this step does
not waive them. Its receipts are `ownership.json` and `ownership-snapshot.json`.

The retained batch helper invokes these native stages:

1. Loop A close fetch and production OPRA catchup.
2. Loop B directional training and predictions.
3. XNAS stock-target history and cumulative Gameplan evaluation.
4. Stock-only Gameplan training/publication for independent 1h/4h/1d/1w targets.
5. Independent stock enrichment training from the exact published cohorts.
6. Account-aware trade planning and completed-session actuals review.

This is the batch helper's stock-only preparation path, distinct from the normal
Stats-first responsibility coordinator. Options data is collected and retained,
while option-strategy rows remain `NO_TRADE_STOCK_ONLY` placeholders. This path
does not authorize enabling options Strategy schedules or legacy traders.
Historical research documents do not become invented point-in-time features;
normal feature admission and availability timestamps still govern training.
Minute rows with both open and close undefined are disclosed as missing price
observations in the source inventory. Their native files remain intact. Partial,
nonfinite, nonpositive, or conflicting observed prices still fail validation.

If valid native coverage lacks a required prior close or enough observed pairs,
the price path explicitly records `UNAVAILABLE_REFERENCE_PRICE` or
`UNAVAILABLE_MINIMUM_SAMPLES`. The informational report may complete with
`direction_projection_status=UNAVAILABLE_PRICE_REFERENCES`; it provides no
chronological trade simulation, ending cash, or ending holdings. Available
independent opportunity estimates remain separate. Source, account, ownership,
forecast and numeric validation still apply. Live execution uses its existing
current-quote and actual-capital checks, not these unavailable estimates.

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

Use `finalize --plan ...` after a separate successful `train`; `run` includes
this step automatically. It verifies the complete native attempt and matching
trade plan before publishing its Gameplan/model references, validates in a fresh
candidate process, then activates while holding the existing session, trader,
overnight and publication locks. `production-baseline.json` preserves the prior
references. A failed candidate attempt restores only references proven to belong
to that batch; an independent publication change blocks automatic restoration.
The immutable candidate files and restoration receipt remain available.

The expected candidate publication contains **24 × N forecasts and 24 × N
options placeholders**, with **3 × N production OPRA cursors**, using that saved
candidate's research universe. The eleven-symbol September batch therefore
required 264 rows in each table and 33 cursors. Evaluate older six/seven-symbol
publications against
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
.\.venv\Scripts\python.exe -u -m datafetching.research_onboarding train --plan artifacts/analysis/research-onboarding-20260913/plan.json --resume-run DATASTORE_ROOT/ml/overnight-runs/EXACT_RECORDED_RUN
.\.venv\Scripts\python.exe -m datafetching.research_onboarding finalize --plan artifacts/analysis/research-onboarding-20260913/plan.json
```

The native resume retains completed stages, pinned sources, and its original
deadline. It creates a descendant attempt, and the batch automatically updates
`overnight-run.json` to that attempt. Use that latest binding for the next resume.
Do not rerun successful training to retry an unchanged downstream
failure. A completed bound attempt is reused. The same 04:00 deadline applies;
missing a deadline does not authorize intraday publication.

An explicit operator exception is distinct from normal recovery. The supported
`--deadline-exception EXAMPLE_EQUITY` accepts a recorded authorization bound to the exact
immutable Gameplan receipt, original action date/deadline and expiry. It permits
only resuming the stock planning/actuals tail, never retraining or selecting new
intraday forecasts. Original attempts and deadlines are retained; reports record
the exception and effective cutoff. Historical actuals still select only
estimates saved before their original opening. Scheduled tasks must not invent
an exception or treat one expired authorization as standing permission.

The following role assignments describe the **September 2026 supervisory
arrangement**, not installed October task states. Retain the explicit-selection,
single-owner and saved-universe rules; resolve present ownership through
[current system](CURRENT_SYSTEM.md). `DATASTORE_ROOT` in the historical command
above denotes that attempt's configured datastore.

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

**Historical September 13–14, 2026 outcome.** Original exceptions, provider
quotes, counts, model status and task observations below retain their event
dates. They establish neither today's membership nor current execution readiness.

The operator explicitly selected EXAMPLE_EQUITY, EXAMPLE_EQUITY, EXAMPLE_EQUITY, and EXAMPLE_EQUITY from the published
September 13 edition. Evidence and logs are under
`artifacts/analysis/research-onboarding-20260913`.
The saved metadata preflight totals 230,492,746,800 uncompressed billable bytes,
quotes $0, and requires 466,354,202,720 free bytes under the existing capacity
policy. The batch was **activated September 14, 2026**, following the user's
explicit exception for completing today's preparation after 04:00. Its native
attempt `20260914T111641.913351Z` completed the remaining planning and actuals
stages, preserving the previously fitted Gameplan and six completed stages.

All 100 planned symbol/schema requests completed, including native options
archives, plus FMP, Schwab, SEC and company archives (3,536 SEC submissions).
Shared CME/macro context was reused. Nine directional models trained; the
Gameplan has 264 forecasts and 264 stock-only options placeholders, and all four
independent enrichment fits are present. Learned enrichment qualifies only 1h
and 4h; daily/weekly sizing remains research. The selected manual Gameplan policy
uses separately verified directional forecast qualification; all 209 execution
windows passed its preflight. An eligible signal is not a promise of an order.

The final read-only audit is `completion-verification.json`; the account,
ownership and forecast preflight is `trading-readiness.json`. Symbol-path logical
sizes, including retained native/normalized/staging files, total approximately
70.8 GiB. This is not a measurement of physical disk growth. The original review
had no shared cash projection because EXAMPLE_EQUITY and EXAMPLE_EQUITY's 143- and 48-minute trailing
gaps exceeded its 15-minute allowance. The subsequent September 14 sparse-session
planning policy carries these verified same-session closing prices explicitly,
including historical planning closes. All 154 hourly planning points across the
11 symbols resolve in the validation at
`artifacts/analysis/sparse-session-planning-20260914/validation.json`. Native prices,
training labels and live quote freshness remain unchanged.

For future onboardings, inspect both native acquisition completeness and the
planning-reference audit. A completed OHLCV download need not contain every minute.
Apply the documented after-hours closing policy only within verified acquisition
coverage, disclose observed versus carried references and pair counts, and leave
uncovered or over-limit gaps unavailable. See [the planning policy](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

The preparation exception expires **05:00 PDT September 14 only**. The user also
authorized a manual late opening on that date before 05:00. The exact optional
CLI argument is `--late-opening-date 2026-09-14`, with `--run-session`,
`--target-horizon all` and `--sizing-policy gameplan-direction-current-market-v1`.
It permits one unconsumed 04:00 entry batch, retains original forecast IDs and
target ends, uses the persistent hourly entry-slot claim, and expires at 05:00.
It does not enable controls or start a worker. Ordinary hourly entries resume
at HH:01 (13:06 transition); future sessions keep the normal 04:00 opening.
Do not add the dated flag to recurring launch commands.

Seven related Scheduled task prompts were updated through the app and verified
against saved before/after snapshots. Their recurrence, active/paused state,
model, reasoning effort, project/thread target, and notification settings were
preserved. The two paused date-specific follow-ups were left intact. The final
prompt updates explain verified activation, explicit unavailable projections,
publication rollback, and the limited September 14 exceptions. See the schedule
snapshots in the evidence directory.
