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

## Earliest current schedule

This schedule assumes the current date of Saturday, August 29, 2026, no
blocking operational incident, Monday's session completes, stage 13 selects the
pooled encoder as the sole nightly bottleneck, the bounded run passes, and the
four operator inputs have been configured.

| Milestone | Earliest Pacific time | Meaning |
| --- | --- | --- |
| Freeze the eligible market evidence | Monday, August 31 after 13:00 | Monday becomes the completed session available to the overnight lane. |
| Stage-13 preregistration | Tuesday, September 1 at 01:42 | The current handoff freezes the exact source generation, checksums, cohort, cutoff, metrics, and bound. |
| Stage-14 encoder run | Tuesday, September 1 at 02:42 | The receipt-bound trainer may fit and publish only `SHADOW_ONLY` output. |
| First Loop C observation | Tuesday, September 1 at 06:42 | The first hourly wake after the 06:30 XNYS open; it runs only if all four inputs validate. |
| Earliest evidence-floor review | Tuesday, October 27 after the close | This is the 40th XNYS session from September 1. Missing observations or failed gates move the date later. |

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
expiry, state, and identity. A safe preflight is:

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

## Evidence floor

The evidence clock starts at the first successful open-session Loop C
publication. `loop-c-observe-evidence-gate-v1` requires all of the following
before an authority-expansion proposal may even be presented for review:

- 40 completed XNYS sessions with valid observations;
- 60 mature independent 1h clusters, 60 mature independent 4h clusters, 30
  mature independent 1d clusters, and eight non-overlapping weekly cohorts;
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
