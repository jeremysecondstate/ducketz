# Loop C immediate-run and evidence-gated rollout

## Boundary

Loop A and the existing Loop B production owners do not wait for this rollout.
The pooled sequence computation begins as soon as its one scheduler-valid
stage-13/stage-14 run verifies, and its Loop B and Options Strategy report seams
may consume that verified shadow publication immediately.

Loop C also does not wait eight weeks before running. It starts observe-only on
the first open-session hourly wake after both prerequisites exist:

1. a verified `SHADOW_ONLY` pooled-sequence publication; and
2. all four strict current inputs: an explicit unexpired operator risk approval,
   fresh reconciled read-only portfolio state, fresh reconciled read-only broker
   state, and an independently issued unexpired halt control.

Every Loop C result remains a research proposal, no-trade, review, or halt
record with `orders_enabled=false` and `orders_placed=0`.

A research proposal now freezes an exact generated Options Strategy as a paper
entry. Its daily lifecycle is materialized by `ml.loop_c.paper_ledger`; that
ledger never chooses a candidate, contacts a broker, or grants authority. The
eligible labels are `1d` One-Session and `1w` Remaining-Week Aggregate. The
weekly aggregate is dynamic rather than always five sessions.

## Activation sequence

The first-publication path is governed by the current checksum-valid scheduler
handoff. Do not infer missed work from a wall-clock date or catch up a stage.
When the guardian selects the normal sequence, these are the eligible windows:

| Milestone | Pacific schedule | Meaning |
| --- | --- | --- |
| Freeze eligible evidence | After the completed 13:00 regular close | The completed session becomes available to the overnight lane. |
| Stage-13 preregistration | 01:42 | The current handoff freezes the exact source generation, checksums, cohort, cutoff, metrics, and bound. |
| Stage-14 encoder run | 02:42 | The receipt-bound trainer may fit and publish only `SHADOW_ONLY` output. |
| First Loop C observation | First eligible open-session hourly wake | It runs only after the sequence publication and all four inputs validate. |
| Daily paper ledger | 00:17 Tuesday-Saturday | It checks the prior market day's exact paper entries and newly mature outcomes. |
| Evidence-floor review | No sooner than the 40th completed observed session | Missing observations or any failed gate move review later. |

The stage-13 and stage-14 times are eligibility windows, not promises. A
guardian-selected incident or a different higher-value bottleneck correctly
defers the encoder rather than bypassing the scheduler contract.

## Operator-input preparation

The checked-in read-only Schwab adapter produces the current portfolio and
broker files; they must not be hand-authored:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.schwab_snapshot `
  --datastore-target pc `
  --compact
```

It reuses the Schwab Duckets authentication, persists identifier-free balances,
positions, working orders, and aggregate trade-history evidence, and binds both
current snapshots to one checksum-valid receipt. Then calculate a pending
proposal from the same observation:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.risk_proposal `
  --datastore-target pc `
  --compact
```

That command writes under `controls/loop-c/proposals` and never copies an
approval into the canonical current directory. The four eventual current paths
are:

```text
C:\DATASTORE\controls\loop-c\current\risk-approval.json
C:\DATASTORE\controls\loop-c\current\portfolio-snapshot.json
C:\DATASTORE\controls\loop-c\current\broker-snapshot.json
C:\DATASTORE\controls\loop-c\current\halt-control.json
```

The operator must explicitly accept or revise every model-binding field,
numeric risk limit, horizon-specific predictive gate, approval expiry,
identity, and rationale. The proposal may derive conservative values from
account equity and may use account history only to ratchet the pending loss
budget down. It cannot claim that Loop C itself works, raise risk, tune a model,
or approve itself. The independent halt control also requires an issue time,
expiry, state, and identity.

Promote only a reviewed immutable proposal through the checked-in issuer. The
issuer requires the operator identity, rationale, explicit halt state, and the
Friday 17:00 Pacific lease expiry; verifies the proposal manifest and exact
model binding; and archives an immutable issuance receipt before atomically
refreshing the canonical controls:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.operator_controls `
  --datastore-target pc `
  --pending-risk-approval <reviewed-risk-approval.pending.json> `
  --approved-by <operator-identity> `
  --expires-at <friday-17:00-pacific-as-ISO-8601> `
  --rationale <operator-rationale> `
  --unhalt `
  --approve-observe-only `
  --compact
```

This grants only `LOOP_C_OBSERVE_ONLY`. It cannot enable, stage, submit,
replace, or cancel orders. A safe preflight is:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.runtime `
  --datastore-target pc `
  --decision-timestamp <current-UTC> `
  --risk-limits C:\DATASTORE\controls\loop-c\current\risk-approval.json `
  --portfolio-snapshot C:\DATASTORE\controls\loop-c\current\portfolio-snapshot.json `
  --broker-snapshot C:\DATASTORE\controls\loop-c\current\broker-snapshot.json `
  --halt-control C:\DATASTORE\controls\loop-c\current\halt-control.json `
  --validate-inputs-only `
  --compact
```

`READY_INPUTS` means the inputs may feed an observe-only run; it does not grant
trade or broker authority.

## Weekly operator-review lease

During the initial observe-only period, use a one-week approval lease that
expires Friday at 17:00 America/Los_Angeles. The expiry is a pilot governance
choice, not a permanent schema rule. Loop C fails closed at expiry, and neither
the hourly scheduler nor the weekly review may renew it.

At 09:00 Pacific each Saturday, the separate task defined in
[`WEEKLY_REVIEW_AUTOMATION.md`](WEEKLY_REVIEW_AUTOMATION.md) runs the checked-in
weekly review:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.paper_ledger `
  --datastore-target pc `
  --compact
```

It then runs the review:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.weekly_review `
  --datastore-target pc `
  --capture-schwab `
  --build-risk-proposal `
  --compact
```

The report distinguishes real account option closes, unattributed account
equity change, and Loop C counterfactual outcomes. It records the exact model
fingerprint, risk approval, policy, and threshold set used, and creates only a
new pending proposal. The operator then discusses hold/tighten/halt/test choices
and supplies a new explicit approval if satisfied. No scheduler-generated
report can approve itself or unhalt Loop C.

The ledger records every option leg's multiplier, exercise/assignment event,
signed BUY/SELL share change, and gross potential share obligation. Its daily
summary exposes total and maximum single-position open obligations. Paper status
guarantees that no real exercise or assignment occurs.
The modeled lifecycle closes exact legs at the target-window BBO no later than
their expiration session; missing exit evidence stays unresolved and is never
treated as a harmless zero-value expiration. Live option execution, earlier
exit buffers, and assignment controls remain outside this rollout.

## Evidence floor

The evidence clock starts at the first successful open-session Loop C
publication. `loop-c-options-1d-plus-observe-evidence-gate-v2` requires all of the following
before an authority-expansion proposal may even be presented for review:

- 40 completed XNYS sessions with valid observations;
- 30 mature independent 1d option-paper clusters and eight non-overlapping
  weekly option-paper cohorts;
- 20 reconciled observations, two successful halt drills, and one rollback
  drill;
- passing preregistered calibration, interval-coverage,
  cost/slippage/latency/missing-data, symbol/regime, publication-integrity, and
  paper-broker-reconciliation gates;
- zero deterministic-gate violations and zero orders.

Passing changes the status only to `ELIGIBLE_FOR_OPERATOR_REVIEW`. The checked-in
evaluator always returns `authority_expansion_allowed=false` and
`automatic_promotion_allowed=false`. Any ranking or order authority is a
separate production change requiring direct user approval.
