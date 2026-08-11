# Loop-native causal BSGP implementation report

Audit baseline: 2026-08-11 UTC. Datastore: `C:\DATASTORE`.

This report records implementation and verification evidence. It does not
promote a candidate, open a lockbox, authorize trading, or turn an offline
reconstruction into prospective evidence. No Databento `timeseries.get_range`
call, paid OPRA download, provider request from training, process stop, or
process restart occurred.

## Implemented boundary

Policy v3 uses committed Schwab source/target receipts for exactly
`NVDA GOOG MU AAPL MSFT AMZN META TSLA CAT SNDK`, CALL and PUT. The unchanged
Black-Scholes prediction remains the control value. A separately versioned
sidecar can carry a pooled finite-basis GP residual correction, while Strategy
rankings, candidate selection, order construction, and order payloads continue
to use the existing control behavior.

The model is loaded and fully verified before the fast publication only when
`published_at < prediction_created_at`. Materialization and fitting run in a
separate locked local worker after fast publication. A matured outcome must
precede the trainer cutoff, and a newly published generation is eligible only
for a later target. Invalid, expired, stale, uncalibrated, unsupported, or
missing models reduce to Black-Scholes without blocking Options.

Paid OPRA remains a guarded legacy v2 reader/import lane and optional external
benchmark. No old OPRA artifact is relabeled as Schwab evidence.

## Actual Schwab inventory

The full dry-run verified committed receipts and contract semantics through
trainer cutoff `2026-08-11T23:59:00Z`. The natural key was
`(symbol, snapshot_for)` and the earliest valid receipt was selected.

| Symbol | Publications | Natural targets | Duplicates | Regular targets | Regular duplicates | Sessions | Contract rows | Max publications/target |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NVDA | 461 | 60 | 401 | 44 | 49 | 4 | 1,109,137 | 223 |
| GOOG | 461 | 49 | 412 | 45 | 388 | 4 | 796,452 | 229 |
| MU | 461 | 61 | 400 | 44 | 48 | 4 | 1,508,712 | 223 |
| AAPL | 461 | 56 | 405 | 45 | 55 | 4 | 1,103,310 | 215 |
| MSFT | 460 | 51 | 409 | 45 | 283 | 4 | 1,145,156 | 229 |
| AMZN | 460 | 47 | 413 | 45 | 401 | 4 | 864,876 | 229 |
| META | 460 | 48 | 412 | 45 | 401 | 4 | 1,505,492 | 229 |
| TSLA | 460 | 50 | 410 | 45 | 67 | 4 | 1,499,534 | 216 |
| CAT | 460 | 47 | 413 | 46 | 412 | 5 | 1,050,060 | 229 |
| SNDK | 460 | 59 | 401 | 44 | 45 | 4 | 1,122,626 | 223 |
| **Total** | **4,604** | **528** | **4,076** | **448** | **2,149** | - | **11,705,355** | **229** |

These counts are publications and physical rows, not independent evidence.
Regular sessions are distinct XNYS session dates per symbol.

## Materialization result

The final current-code read-only/dry-run used trainer cutoff
`2026-08-11T23:59:00Z` and completed in 1,246.5527294 seconds wall time;
materializer time was 1,244.9442896 seconds. Python-tracked peak allocation was
72,445,144 bytes. The observed process peaks were 1,313,464,320 bytes working
set, 2,595,483,648 bytes paged memory, and 7,999,836,160 bytes virtual memory.
The dry-run intentionally created no materialization path, pointer, receipt, or
artifact hash.

The 528 selected natural targets span `snapshot_for` clocks from
`2026-08-04T20:00:00Z` through `2026-08-10T20:30:00Z`; their selected receipt
clocks span `2026-08-05T14:02:06.222839Z` through
`2026-08-10T20:58:57.856397Z`. Every selected receipt predated the dry-run start
at `2026-08-11T09:49:27.9317635Z`, so the end-of-day audit cutoff admitted no
future receipt.

Offline bootstrap considered all 528 natural targets and materialized zero
rows. Eighty targets were non-regular; all 448 regular targets failed
`TARGET_REQUEST_NOT_AFTER_EMULATED_PREDICTION`. The older receipts do not prove
that their request/readiness state existed before a reconstructed prediction
time. That clock cannot be shortened or invented, so this history is not
bootstrap training evidence under the implemented policy.

An earlier, less strict diagnostic reconciled 5,293 legacy-v2 prediction rows,
13 surfaces, twelve symbol/side routes, and one distinct session. Those counts
are retained below only to make the migration audit legible. They are not v3
samples or prospective-session credit.

The v3 whole-chain re-verification requires the exact completed underlying bar
in an immutable Loop A readiness receipt and every causal input receipt to
remain checksum-verifiable from the original Pricing generation. The final
prospective reconciliation evaluated 5,309 legacy prediction rows. Its status
ledger was: 2,442 `COMPLETE`, 2,750
`TARGET_ALREADY_OBSERVED_BEFORE_PREDICTION`, eight
`STALE_PRE_PREDICTION_QUOTE`, and 109 `TARGET_QUOTE_INVALID`. A later duplicate
receipt never rescues a target whose earliest natural receipt was already
observable before the prediction.

The 2,442 otherwise complete target matches still could not become v3 samples:
all reported `SOURCE_SAMPLE_RECEIPT_MISSING` under the stronger source chain.
Fourteen source generations reference an input file whose historical checksum
no longer verifies, and eleven lack an inventoried immutable Loop A readiness
receipt for the exact target. The complete scan verified 882 consulted
source-lineage files and returned zero rows and zero sessions. This is
fail-closed exclusion, not an assertion that the old artifacts were fabricated
or that their later market outcomes are wrong.

Because no row survived, source/target quote-clock coverage, source/target
receipt-clock coverage, underlying-readiness coverage, and rate/carry input-kind
counts are all exactly zero. Current-revised historical rate use remained
false; no missing rate or carry value was substituted.

| Legacy-v2 diagnostic route | Reconciled rows | Surfaces | Sessions (not v3 credit) |
| --- | ---: | ---: | ---: |
| AAPL/CALL | 388 | 1 | 1 |
| AAPL/PUT | 390 | 1 | 1 |
| AMZN/CALL | 346 | 1 | 1 |
| AMZN/PUT | 351 | 1 | 1 |
| GOOG/CALL | 305 | 1 | 1 |
| GOOG/PUT | 306 | 2 | 1 |
| MSFT/CALL | 538 | 1 | 1 |
| MSFT/PUT | 537 | 1 | 1 |
| MU/CALL | 771 | 1 | 1 |
| MU/PUT | 771 | 1 | 1 |
| NVDA/CALL | 295 | 1 | 1 |
| NVDA/PUT | 295 | 1 | 1 |

All twenty required v3 routes are explicitly retained as missing. Offline rows
never increment prospective counts. With zero verified v3 rows and sessions,
there is no train/calibration/assessment partition and no actual-datastore model
generation was published. Current-revised historical rate use remained false.

## Immutable policy and readiness artifacts

The v3 migration was published conservatively with all gates `NOT_PROVEN` until
the deployed capture chain exists. It records paid OPRA as optional, all twenty
routes, `candidate_frozen=false`, `lockbox_open=false`,
`production_authorized=false`, and `automated_action_allowed=false`.

| Artifact | Relative path | Payload/semantic hashes | Receipt checksum |
| --- | --- | --- | --- |
| v3 policy | `ml/option-pricing-loop-native-eligibility-policies/20260811T095504.565874Z` | semantic `ba709e60705ac7d598dab9479c4cddd7a3bb1e1af4b357d8816a40c3698254eb`; file `71b870cd10f9fb23c971506f1623945d140fbbee0de3f73319ebbcdb32cf3739` | `bd99a20b5057e39c4d503dee43464b704e882f30107c0fd84d2ed9e5c5f93ab8` |
| v3 report | `ml/option-pricing-loop-native-eligibility-reports/20260811T095504.565876Z` | semantic `98dfcd08b0f48d68a318413bc5f21eb52d5c6ff8feda39c30107240513c12151`; file `f4ac4c892a32224c69ea45cdf9066b1654b199adc79077a6a0c9a14b5ff0cccf` | `3085270c0086c42349ba147bafd85f23872e69dee25c4c44c781da04e2a85005` |
| Operational PASS | `ml/option-pricing-operational-readiness/20260811T095424.966364Z` | report `85f3afdc2c7aae4b73bf9a802ff3b29f5ab501b7973d46912a58a0b44bcd1900` | `5e9d40b43ad65262be7218ec4ee873020439a74b42883927101713a8b3f40cfd` |
| Strategy NOT_PROVEN | `ml/option-pricing-strategy-evidence/20260811T095443.794152Z` | report `3de4e15a28e29b508a1b8cea5a34558ae8fde147b3ce172c6ce85f0118d6e15c` | `57e12035bb7af93a3178494759ce38110456117b8eb05330e51dc4898ce0a2b1` |

Operational preflight passed dependency lock, `pip check`, capacity, current
publication lineage, runtime benchmark, retention, and all six CLI smoke tests,
including the new local worker. Strategy evaluation found zero verified paired
candidates and zero sessions. It remained `NOT_PROVEN`, with rankings and order
construction unchanged and no order payload created.

The preflight had 1,557,745,725,440 free bytes. Its representative runtime
benchmark processed 281,372 samples, 5,309 predictions, 110 surfaces, and 5,309
evaluations in 200.6019474 seconds with 64,373,609 bytes Python-tracked peak
memory.

## Simulated generation n-1 to target n

A temporary, automatically removed simulation loaded a fully verified earlier
generation and published Pricing before a simulated Options receipt:

```text
trained_through       2026-01-07T16:02:00Z
model published       2026-01-09T00:00:02Z
prediction created    2026-01-09T16:01:00Z
Pricing published     2026-01-09T16:01:05Z
Options receipt       2026-01-09T16:01:06Z
```

The temporary generation hash was
`314762b5ebf494f37f9888fe0bc2733dd63bf6e1cf920bedc53166dfff14b02b`;
its manifest checksum was
`5e2c9979e1e8ba4654011bf4de56aac5870131b47c9bbfd7ec73b0fa39ced401`
and model checksum was
`cc71d237ef72e817dee4b0d75c96e13707075761abdf517e662a1330727ebd45`.
All six shadow rows were `BSGP_SHADOW_READY`; all six raw residuals were
nonzero and four survived the American-bound/shape projection as nonzero
constrained corrections. The baseline canonical JSON SHA-256 before and after
inference was identically
`37b3c1ed0de7618534ceea779e9edeec47f67146b5ffe5012cbef75116b0bde8`.
The separate shadow canonical JSON hash was
`788a3f984bcb0d44996544944792e260832f0fc5c16fcc100b6b51279c54a175`.

## Live restart follow-up

With explicit operator approval, all six runtime roles were restarted on
2026-08-11 during the regular session, preserving their prior arguments. Logs
are under
`C:\DATASTORE\runtime-logs\loop-native-restart-20260811T160228.2069648Z`.

The first v2 terminal publication for target `2026-08-11T16:15:00Z` exposed an
empty-frame verifier defect: pandas/pyarrow could not merge two empty
Arrow-backed Black-Scholes columns. The immutable directory was completed, but
verification failed before its atomic pointer write, so it remained unreachable
and Options could not treat it as authority. The verifier now skips the
value-comparison merge only when both already scope-verified frames are empty;
a regression test covers this terminal no-row case.

After recycling only the compatible Pricing/Options pair, target
`2026-08-11T16:30:00Z` published a verified empty v2 shadow sidecar at
`2026-08-11T16:31:46.398802Z`. Its authoritative path is
`ml/option-pricing-target-outcomes/1786465800000000000-1786465905857701000`,
manifest checksum is
`29adf1f85a0b802832c0bfd5aaecb78d6ad7ca804c65153a9ef8b65f46123667`,
and receipt checksum is
`f7ae3a478bbdeb9b367ca6fd08f1138d20b8748aaed42a27f560f978e7b343f5`.
Its terminal status is `TARGET_BAR_NOT_READY`, both baseline and shadow row
counts are zero, and `automated_action_allowed=false`. Options verified that
terminal barrier and granted no prospective credit.

The full Pricing publication committed at `2026-08-11T16:35:01.115253Z` under
`ml/option-pricing-runs/20260811T163100.160478Z`. It recorded
`BASELINE_FALLBACK_NO_MODEL`, zero sidecar rows, no OPRA requirement, zero
external provider requests, and a post-publication local worker cutoff of
`2026-08-11T16:31:46.398802Z`. The worker was still running at handoff and had
not published a materialization or model pointer.

This restart proves the fail-closed v2 publisher/reader and nonblocking worker
ordering live, but not a valid prediction sequence. Loop A's last verified
readiness target was `2026-08-11T16:00:00Z`, ready at
`2026-08-11T16:14:28.540903Z`; no exact readiness receipt existed for the later
Pricing targets. No prediction or prospective session was manufactured.

## Verification and conclusions

The combined audited suite passed 130 tests. Python compilation, CLI help,
`pip check`, `git diff --check`, causal artifact verification, readiness,
operational preflight, Strategy evaluation, actual-datastore dry-run, and the
generation chronology simulation passed their applicable checks. The only
warnings were preexisting line-ending notices and NumPy/joblib deprecation
warnings.

- **Capture-ready:** not yet established on the live processes. The code and
  policy are ready and the compatible Pricing/Options publisher-reader pair is
  running, but the restart observed only a fail-closed terminal publication
  because exact Loop A readiness had not advanced to the Pricing target. No
  live nonterminal prediction-to-Options sequence or v3 materialization/model
  chain has completed yet.
- **Research-gate eligible:** no. No legacy prospective row survives the v3
  readiness/input-lineage contract, all twenty routes have zero v3 sessions,
  offline bootstrap has zero causal rows, and comparator/calibration evidence
  is absent.
- **Production authorized:** no. Required gates, candidate, closed lockbox,
  Strategy evidence, and explicit operator authorization have not passed.

No new regular-session Loop A -> Pricing -> Options sequence occurred during this
audit window, so none was manufactured or backdated.
