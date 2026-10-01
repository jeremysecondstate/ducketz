# Cross-PC source backlog audit

Audit date: October 1, 2026, America/Los_Angeles. This is a sanitized,
read-only inventory of the original Atlas application checkout and retained
publication evidence. A separate fresh isolated Gameplan verification is dated
below. This work does not publish, install, merge, or deploy the application
changes listed below. Later infrastructure release evidence
belongs in the release/bootstrap record and may supersede this initial audit.

## Source and receipt baseline

- Atlas's application checkout is `main` at
  `0407388559d54612603380793fc26a98937697fc`. Its index is unchanged; 33 tracked
  files are modified. Untracked shared files and private evidence are listed
  separately below. No repository or searched ancestor `AGENTS.md` existed at
  the beginning of the audit.
- A fresh `git ls-remote --heads origin` read verified `jeremysecondstate/ducketz`
  main at that SHA, the nine Atlas publication branches below, Gameplan fallback
  at `be760388063a0fa3099c1cc445ba758be69ceb23`, and historical Scout bootstrap at
  `0460e21b4ea60acf0ab7071c5b397026226db515`. No published
  `codex/machine-local-symbols` ref appeared in this inventory.
- Scout's reported local branch `codex/machine-local-symbols` at
  `df2ab89af4819552576d068aea68b6b527c634c1` and reported 69 passed / 2 skipped
  helper baseline are user-relayed evidence. Atlas has not independently
  verified that checkout, its dirty bytes, task definitions, or runtime.
- Eighteen legacy ready JSON records exist. Nine have preserved commit/push
  receipts: eight have `handoff_published`, one has `pushed`. Nine records remain
  unconsumed. All are from the local Paper Improvement producer and name base
  `0407388559d54612603380793fc26a98937697fc`. A `ready: true` flag alone is not
  current publication eligibility.
- Git receipts contain 14 commit-notice entries and two supplemental pending
  notices. Inbox receipts contain seven retained bundle entries. API publisher
  receipts contain two historical `handoff_published` entries. These are
  distinct indexes with overlapping identities, not counts to add together.
- The legacy policy permits one ready record per courier wake. It requires an
  exclusive lock, atomic stage receipts, exact ownership/file/test bindings,
  isolated verification, normal owned-branch pushes, and remote SHA checks.
  The later API amendment prohibits shared-filesystem transport fallback. Its
  status is `pending_exhaustive_enumeration`; existing records show this
  transport blocker also held source publication. Source and notice delivery
  must become independent stages without erasing this history.

## Per-record disposition

Record IDs below are immutable correlation keys. Branch names for the first
nine rows are `codex/atlas/<record-id>`. Historical passing results are evidence
for the historical tested bytes, not a fresh pass for today's combined tree.
All nine published branches are **integration pending** against shared main.

| Record ID | Source state / historical test result | Disposition |
|---|---|---|
| `20260930T093128Z-c25bd39a-f8e2-44e5-a3be-30c21f207c23` | `fe31c3ae292abb0a69abbd405226695009c62a78`; 550 passed; tests completed 2026-09-30T09:23:07Z; handoff published | Already published. Retain five-family research note. Model configuration subsequently superseded; do not restore this snapshot as current defaults. |
| `20260930T103427Z-e276565f-ced3-44cd-9ff6-b4e78e754d52` | `c492a3787419bcd82624b229bff94ea531791f97`; 722 passed / 1 skipped; 2026-09-30T10:28:51Z; handoff published | Already published. Exploratory Paper/strict Powder separation is a prerequisite for later Paper settings. Integrate shared code together; select later accepted configuration explicitly. |
| `20260930T114605Z-9736caaa-062a-4f3e-b4fe-aedd7b04e606` | `1cb281e954e13926cd9e3af2da4121bb128b71ec`; 895 passed / 1 skipped; 2026-09-30T11:40:54Z; handoff published | Already published. Includes prerequisite separation files; volatility sizing is part of subsequent lineage. Avoid applying full older config over the current model/Paper candidate. |
| `20260930T130129Z-70c29fee-adac-41da-a274-b16254e54ce4` | `aff044e6369ebe96b1b391880205902f8ac2c11d`; 1,035 passed / 1 skipped; 2026-09-30T12:55:02Z; handoff published | Already published; 30% bullish spot setting subsequently superseded. |
| `20260930T161326Z-e355ea8c-dde8-49e8-acc9-c964c416f975` | `7f32acf8a4ab846ce0c89b7be9c8ac3d4bda5d27`; 726 passed / 1 skipped; 2026-09-30T16:10:05Z; handoff published | Already published; 0.18 saturation subsequently superseded. Tests explicitly used dependency bytes beyond bare base HEAD. |
| `20260930T172312Z-84399bc4-9153-46fe-a0b6-60a5bdaeb5a4` | `ee80dc35b1f4afc398c1bc9286d9d8e618c6ae18`; 726 passed / 1 skipped; 2026-09-30T17:17:49Z; handoff published | Already published; 0.21 saturation subsequently superseded. |
| `20260930T204825Z-efbc0546-3c6d-4cf4-a638-ae068aba55fb` | `1804f62cfb11db035879f0c034f8cd325e248625`; 726 passed / 1 skipped; 2026-09-30T20:44:34Z; handoff published | Already published; retain research note; 25% bullish spot setting subsequently superseded. |
| `20260930T215932Z-721ebf7c-962e-4f67-8d96-4146dce1fdff` | `8f0862e419cf915575817e0e337bd054a1b06a0c`; 726 passed / 1 skipped; 2026-09-30T21:55:50Z; handoff published | Already published; retain saturation research note and verified receipt. Later composite Paper config must retain reviewed supersession. |
| `20260930T231127Z-29c9762f-1433-4063-8011-226cc06a4783` | `a7b37f15627fa03c78dc0e05ee5b099fd7d3fe9f`; 726 passed / 1 skipped; 2026-09-30T23:05:03Z; pushed | Already published source, pending notice delivery. Retain 20% trial history; do not repeat commit/push or restore superseded config. |
| `20261001T023749Z-ad3e2ae0-dd5e-4aa8-81fe-a1ca55871464` | No commit receipt; reports 750 passed / 1 skipped at 2026-10-01T02:33:31Z | Needs renewed dependency review/testing. Model config is superseded. `ml/hyperliquid_paper_review.py` and its test still match recorded bytes and contain an independent preflight-recovery fix worth retaining in the integration candidate. |
| `20261001T034759Z-382349d2-7e54-42b9-a295-d98bd6803b25` | No commit receipt; reports 750 passed / 1 skipped at 2026-10-01T03:43:03Z | Superseded model configuration; preserve record/evidence and link replacement. Do not reconstruct queued bytes from current source. |
| `20261001T045954Z-952a800a-f3c5-4e6e-9d25-8782a89267a6` | No commit receipt; reports 750 passed / 1 skipped at 2026-10-01T04:56:17Z | Superseded calibration configuration; preserve history, hold publication. |
| `20261001T061446Z-8df54053-23c3-4897-b907-d7fd320ff3ac` | No commit receipt; reports 750 passed / 1 skipped at 2026-10-01T06:11:29Z | Superseded model configuration; preserve history, hold publication. |
| `20261001T125900Z-7475cc81-4cb4-44a5-9f40-59a320e218ed` | No commit receipt; reports 841 passed / 1 skipped at 2026-10-01T12:54:16Z | Superseded calibration configuration. Model/Paper config and policy-test fingerprints differ now. |
| `20261001T141554Z-dc423d79-0972-4afa-8f8f-78481acd2c70` | No commit receipt; reports 841 passed / 1 skipped at 2026-10-01T14:11:37Z | Superseded model weighting. Model/Paper config and policy-test fingerprints differ now. |
| `20261001T153134Z-df5aab0f-039a-474c-90d9-94ffb5035f83` | No commit receipt; reports 841 passed / 1 skipped at 2026-10-01T15:27:19Z | Superseded model weighting snapshot; later calibration changed the same file. |
| `20261001T164805Z-c9d2c29b-5225-4b8f-a219-fb04a726e24f` | No commit receipt; reports 841 passed / 1 skipped at 2026-10-01T16:43:13Z | Owned model config matches current bytes. Needs fresh combined verification because Paper config and policy-test dependencies changed afterward. |
| `20261001T181012Z-e855e525-5b7d-410d-91cd-d34bb9fce7c9` | No commit receipt; reports 841 passed / 1 skipped at 2026-10-01T18:03:51Z | Ready for dependency reconciliation, not standalone publication: both owned files and all recorded fingerprints match now, but 15 dependencies outside its owned set differ from or are absent in its recorded base. Build and test the full reviewed candidate first. |

The last record owns only `configs/hyperliquid-paper.json` and
`tests/test_hyperliquid_paper_policy.py`. Its 15 base dependency gaps are:

```text
configs/hyperliquid-models.json
configs/hyperliquid-powder-policy.json
configs/hyperliquid-powder.json
docs/hyperliquid-system-analysis/OPERATIONS_WATCH.md
docs/hyperliquid-system-analysis/PAPER_IMPROVEMENT.md
ml/hyperliquid_paper_cadence.py
ml/hyperliquid_paper_forecast_evidence.py
ml/hyperliquid_paper_review.py
ml/hyperliquid_paper_runtime.py
tests/test_hyperliquid_paper_exploration.py
tests/test_hyperliquid_paper_exploratory_cadence.py
tests/test_hyperliquid_paper_review.py
tests/test_hyperliquid_paper_runtime.py
tests/test_hyperliquid_paper_view.py
tests/test_hyperliquid_powder_view.py
```

The changed view tests also require ownership review of their application
services/UI dependencies; being present in a producer's fingerprint map does
not transfer ownership to that producer.

## Gameplan: full shared dependency readiness

`codex/cross-horizon-bearish-fallback-20260930` at
[`be760388063a0fa3099c1cc445ba758be69ceb23`](https://github.com/jeremysecondstate/ducketz/commit/be760388063a0fa3099c1cc445ba758be69ceb23)
contains the complete 23-file change, not just the two UI modules. The current
Atlas application contains all 23 files with content equivalent to that Git
commit after CRLF/LF normalization. Fifteen tracked files differ from raw Git
blob bytes only by checkout line endings; the eight newly added files match
raw Git blobs exactly. Exact byte claims must retain that distinction.

The preserved candidate manifest binds 176 supporting dependency files. All
176 hashes still match Atlas's current raw local bytes, and all 176 are present
in `be76038` with matching content after line-ending normalization (95 also
match raw Git blob bytes). Its post-installation
verification binds 218 files including source/tests; all 218 still match.
The candidate manifest hash also matches the post-installation receipt.
Historical tests report **678 passed, zero failed, zero skipped** over 27
focused test files, completed `2026-10-01T00:20:10.265057+00:00` (September 30,
5:20 p.m. Pacific). An earlier isolated candidate run completed
`2026-09-30T20:40:45.908654+00:00`. This audit checked evidence/hash continuity;
it did not run those tests again.

The source delta covers frozen prospective policy/receipt validation, donor
selection, daily and per-donor caps, atomic reservations and fill accounting,
source revalidation before submission, cash/share projection, saved trade-plan
metadata, read-only session review, and UI donor attribution. Regression
coverage includes concurrency, pending/unknown orders, prior-day carryovers,
source mutation, old publications, Pacific DST, invalid attribution, normal
sale priority and fixture quote-clock assumptions. Dependencies include the
common calendar/contracts/artifact readers, direction policy, fixed-horizon
budget, broker abstractions, publication and quote recovery, source selection,
forecast preparation, market-data modules and existing UI helpers.

This is the strongest existing candidate for a reviewed integration PR. All
23 changes must move together, with the common dependency set checked at the
actual integration base. A read-only GitHub query found no existing PR in any
state for this branch at audit time. The preserved receipt says source installation was
completed on Atlas. It does **not** prove the current in-memory trader/UI
version, source installation on Scout, a common symbol profile, or main
integration. Existing symbol lists/data/ledgers stay local. A two-profile
offline test and Scout byte report are still required for cross-PC parity.

### Fresh verification: October 1, 2026, 12:31 p.m. Pacific

The complete 27-file offline fixture suite was rerun in a separate managed,
clean worktree at exact commit `be760388063a0fa3099c1cc445ba758be69ceb23`.
It passed **678 tests, with zero failures/errors/skips and nine existing
NumPy/joblib deprecation warnings**, in 115.45 seconds. The run began
`2026-10-01T19:29:21.550497+00:00` and evidence completed
`2026-10-01T19:31:18.237848+00:00`. All 176 supporting dependencies matched the
candidate commit, all 767 captured shared source/test/config/document hashes
were unchanged afterward, and the full 23-file base-to-candidate diff passed
`git diff --check`. The worktree remained clean. A verification guard recorded
zero blocked network/private-state/operational-process attempts and confirmed
130 imported modules came from the candidate, with zero original-checkout
application imports. Tests used mocks and tiny synthetic estimator/calibration
fixtures in temporary directories; no operating model training, provider or
broker connection, runtime control, source deployment, or task change occurred.
The full output and exact command/hash maps remain in private local evidence;
the test log SHA-256 is
`a09f745231f243c913fdd7211b3edc0517527358dd629ec918bd1f369bbafcc3`.
Complete candidate/dependency review found no new integration blocker. This
supports preparing the reviewed Gameplan PR against the verified main base;
merge approval, Scout adoption and runtime-version evidence remain separate.

## Per-file disposition

`G` means already published in the 23-file Gameplan commit above, with main
integration pending and historical tested-byte continuity verified. `P` means
already published in the Paper/Powder separation lineage, with current raw
bytes matching its legacy record, and main integration pending. `F` means
fresh candidate verification or explicit ownership reconciliation is required.
No unreviewed dirty file is included merely because it shares a directory.

| Path | State | Next action / ownership |
|---|---|---|
| `app/ui/gameplan.py` | G | Integrate with donor-attribution data contract. |
| `app/ui/gameplan_data.py` | G | Integrate with fallback policy and accounting validation. |
| `ml/gameplan_cash_ledger.py` | G | Keep projection and live donor-cap contract together. |
| `ml/gameplan_trade_planning.py` | G | Keep policy/receipt bindings with publisher and reader. |
| `ml/gameplan_trade_review.py` | G | Shared report explanations. |
| `ml/gameplan_trade_snapshot.py` | G | Shared allocation identity hashing. |
| `ml/nightly_gameplan.py` | G | Common prospective policy, independent of local symbols. |
| `ml/stock_trader/cross_horizon_fallback.py` | G, untracked on main | Shared pure policy and immutable-publication reader. |
| `ml/stock_trader/fallback_review.py` | G, untracked on main | Shared read-only reviewer; generated reports remain local. |
| `ml/stock_trader/gameplan_direction_engine.py` | G | Shared ranking, attribution and source binding. |
| `ml/stock_trader/horizon_ledger.py` | G | Shared schema/accounting code; database stays local. |
| `ml/stock_trader/independent_runtime.py` | G | Source integration requires existing runtime deployment lane. |
| `tests/test_cross_horizon_fallback_ledger.py` | G, untracked on main | Atomic quota/concurrency/carryover tests. |
| `tests/test_cross_horizon_fallback_policy.py` | G, untracked on main | Policy/date/receipt/DST tests. |
| `tests/test_cross_horizon_fallback_review.py` | G, untracked on main | Read-only review tests. |
| `tests/test_cross_horizon_fallback_runtime.py` | G, untracked on main | Mocked execution/source-mutation tests. |
| `tests/test_gameplan_fallback_planning.py` | G, untracked on main | Projection/UI/publication tests. |
| `tests/test_gameplan_quote_clock_recovery.py` | G | Historical fixture assumptions corrected in published candidate. |
| `tests/test_gameplan_trade_snapshot.py` | G | Allocation identity assertion. |
| `tests/test_stock_only_gameplan.py` | G | Legacy preparation fixtures. |
| `docs/loops-system-analysis/CROSS_HORIZON_BEARISH_FALLBACK.md` | G, untracked on main | Shared policy explanation. |
| `docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md` | G | Shared policy link and prospective boundary. |
| `docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md` | G | Shared preparation/reader contract. |
| `configs/hyperliquid-powder.json` | P | Preserve strict referenced policy; local execution authority remains separate. |
| `configs/hyperliquid-powder-policy.json` | P, untracked on main | Shared strict defaults; split local fields as specified below. |
| `ml/hyperliquid_paper_cadence.py` | P | Shared prospective evidence admission. |
| `ml/hyperliquid_paper_forecast_evidence.py` | P | Shared source/qualification evidence validation. |
| `ml/hyperliquid_paper_runtime.py` | P | Shared exploratory Paper behavior; no runtime restart in this audit. |
| `tests/test_hyperliquid_paper_runtime.py` | P | Include in combined isolated candidate. |
| `tests/test_hyperliquid_paper_exploration.py` | P, untracked on main | Include with separation implementation. |
| `tests/test_hyperliquid_paper_exploratory_cadence.py` | P, untracked on main | Include with cadence/evidence implementation. |
| `docs/hyperliquid-system-analysis/PAPER_IMPROVEMENT.md` | P | Shared behavioral contract; local authority must be bound separately. |
| `configs/hyperliquid-models.json` | F | Current model weights/calibration match 16:48Z record; Paper dependencies moved. Paper producer owns lineage; use latest reviewed state. |
| `configs/hyperliquid-paper.json` | F | Current 10% bullish allocation matches 18:10Z record; requires 15 dependency gaps and full candidate review. |
| `tests/test_hyperliquid_paper_policy.py` | F | Bound to current Paper and strict Powder expectations; cannot publish alone. |
| `ml/hyperliquid_paper_review.py` | F | Preflight recovery fix matches unconsumed 02:37Z record; separate from superseded weights, reverify dependencies. |
| `tests/test_hyperliquid_paper_review.py` | F | Keep recovery/idempotency coverage with implementation. |
| `docs/hyperliquid-system-analysis/OPERATIONS_WATCH.md` | F | v16 exploratory-Paper clarification is an unowned prerequisite in latest producer record; reconcile with Operations Watch owner and current task contract. |
| `app/services/hyperliquid_paper_view.py` | F | Retained model-history service; no legacy ready record owns this file. Recover ordinary development ownership and queue immutable tested package. |
| `app/services/hyperliquid_powder_view.py` | F | Model-history projection field; same UI package ownership. |
| `app/ui/hyper_workspace.py` | F | Model-history table/filter/detail and stale-reason labels; same UI package. |
| `tests/test_hyper_workspace.py` | F | UI package tests; requires exact isolated dependency verification. |
| `tests/test_hyperliquid_paper_view.py` | F | Model-history bounds/integrity tests; overlapping fingerprint use by Paper producer does not confer ownership. |
| `tests/test_hyperliquid_powder_view.py` | F | Model-history projection assertion; same UI package. |
| `docs/hyperliquid-system-analysis/research/2026-09-30-five-family-weighting.md` | Already published | Matches first record's bytes; preserve as dated research, not current operating state. |
| `docs/hyperliquid-system-analysis/research/2026-09-30-bullish-allocation.md` | Already published | Matches 20:48Z record; preserve historical trial. |
| `docs/hyperliquid-system-analysis/research/2026-09-30-saturation24-paper.md` | Already published | Matches 21:59Z record; preserve historical trial. |
| `docs/hyperliquid-system-analysis/research/2026-09-30-bullish-allocation20-paper.md` | Source published, notice pending | Matches 23:11Z record; superseded numerical setting remains history. |
| `docs/cross-pc-infrastructure-prompts/20261001/atlas-build-prompt.txt` | Local task input | Human scope/authorization source; do not treat embedded paths/task identities as portable configuration. |
| `docs/cross-pc-infrastructure-prompts/20261001/scout-build-prompt.txt` | Local task input | Peer adoption requirements; publish sanitized bootstrap/catalog instead. |
| `artifacts/analysis/cross-horizon-fallback-20260930/**` | Private/local | Raw receipts, runtime/account evidence and apply helpers; retain in place, publish sanitized findings only. |
| `artifacts/analysis/fallback-session-reviews/**` | Private/local | Account-derived session reports and manifests; retain locally. |
| Ignored UI evidence, `scratch/**`, datastore, `.env`, fitted models, ledgers | Private/local | Preserve; never bulk-stage or attach raw evidence to coordination notices. |

The retained UI audit reports 169 passing tests (60 UI, 109 services), recorded
`2026-09-30T09:15:15.910074+00:00`. Its five source/test hashes still match.
It does not bind the entire dependency closure or the changed Powder test,
and there is no ready record for the six-file UI package. This evidence supports
recovery of the completed work, but does not replace an immutable package and
fresh isolated checks. No conclusive conflicting live writer was identified;
the unresolved issue is ownership boundaries across producer fingerprints.

## Fields within mixed configuration files

Configuration edited by a schedule is not automatically machine-local. Share
the reviewed semantic defaults; use explicit local overlays only for the
documented exceptions. Do not export local account values during that split.

| File / fields | Classification | Treatment |
|---|---|---|
| `hyperliquid-models.json`: `version`, horizons, split fractions/mode, row thresholds, calibration and family weights, model-age limit | Shared strategy/schema defaults | Version and review changes. Accepted local experiment identity/artifact pointers remain local. Current repeated weight edits require supersession ordering. |
| `hyperliquid-models.json`: `markets_config` | Shared relative config reference | Relative default is portable; absolute override is local. Its symbol membership comes from the profile. |
| `hyperliquid-models.json`: `retrain_seconds`, `poll_seconds`, `retry_seconds` | Shared behavioral cadence defaults | Per-PC deviation requires a documented operational exception; task ownership stays local. |
| `hyperliquid-models.json`: `model_threads` | Shared default with explicit machine-resource override | Hardware tuning may differ through profile; do not silently fork strategy behavior. |
| `hyperliquid-paper.json`, `hyperliquid-powder-policy.json`: `version`, admission, entry/exit/saturation bands, volatility/sigma sizing, gross/risk caps, bullish split, utilization, minimum/rebalance thresholds, fees/slippage, stop, freshness limits, reserve/transfer thresholds | Shared strategy/safety defaults | Preserve Paper/strict-Powder distinction. A symbol-specific difference needs named-symbol rationale; changing common defaults stays reviewable source. |
| Same files: `data_root` | Machine-local path | Resolve through local profile; exclude actual path values from portable profile/catalog. |
| Same files: `initial_cash.*` | Machine/account seed state or explicit fixture data | Local account-specific seed values stay local; only clearly synthetic generic example defaults belong in shared examples. |
| Same files: `seed_mode`, `mode` | Shared schema semantics plus local activation choice | Share supported/default semantics; never infer permission to mirror/reseed or run either mode from a source update. |
| Same files: `model_config` | Shared relative reference | Absolute paths and chosen active model/artifact namespace are local. |
| `hyperliquid-powder.json`: `paper_config` | Shared strict-policy reference | Must continue to reference strict policy, not exploratory Paper. |
| `hyperliquid-powder.json`: `max_order_notional`, `poll_seconds` | Shared safety/cadence defaults | Local owner may impose tighter explicit limits; changing source grants no live authority. |
| `hyperliquid-powder.json`: `mode` | Shared configuration kind, local runtime ownership | A `powder` schema value does not activate a task, process, account, or order writer. |
| `hyperliquid-markets.json`: `symbols`; stock watchlist/universe selectors | Per-PC symbol profile | Preserve each PC's explicit membership; exclude from assertions of identical configured universe. Shared algorithms/tests remain identical. |
| `hyperliquid-markets.json`: `output_root` | Machine-local path | Resolve locally. |
| `hyperliquid-markets.json`: schema, interval, collection/retry/repair defaults | Shared defaults | `max_parallel_updates` may have a documented resource override; symbol-specific feed exceptions require explicit profile entries. |

The existing application still contains mixed files. This audit classifies the
required split; it does not claim that all application loaders already support
those overlays. Infrastructure profile adoption must not rewrite the running
application's symbol or account configuration.

## Delivery and integration actions

Three known retained bundles need provenance-preserving delivery under the
explicitly selected new transport:

| Bundle | Kind / source | Remaining stage |
|---|---|---|
| `20260930T233549Z-3eac8fb97d0f4e909b90f2bb5c30ce5e` | Paper 20% source notice, `a7b37f1` | Delivery only; source commit/push already succeeded. |
| `20261001T003037Z-e0f98cd554ab4ad7bbd86de4af6079fc` | Gameplan deployment-status update, `be76038` | Delivery only; retain original notice identity/digest and reported-stage provenance. |
| `20261001T073129Z-e0ca7f4ebb1f4ba6a1567f5f60c412c6` | Gameplan rollout/preparation update, `be76038` | Delivery only; do not replay deployment or source publication. |

1. Publish/install the infrastructure candidate separately. Preserve original
   ready, inbox, Git and API receipt stores and all immutable sealed bundles.
   Retry only unfinished delivery; resolve unknown outcomes by querying exact
   identity/digest before creating another publication.
2. Prepare the existing complete Gameplan branch for reviewed integration into
   main, checking the actual target-base dependencies and the 27-test suite in
   an isolated candidate. Include two different synthetic symbol profiles.
   Main merge and running application deployment remain separate decisions.
3. Recover the Hyperliquid UI package's producer identity and immutable final
   bytes. Run bounded offline service/UI checks against its full dependencies,
   then queue/publish that complete six-file change with factual test evidence.
4. Consolidate Paper/Powder separation, preflight recovery and the latest
   accepted model/Paper settings into a dependency-complete reviewed candidate.
   Preserve older commits/notices as historical lineage; mark superseded
   unconsumed records without deleting them or replaying older full configs.
   Reconcile Operations Watch documentation ownership before including it.
5. Obtain Scout's exact adopted infrastructure SHA, local profile-preservation
   report, task reconciliation and shared/dirty byte fingerprints at a common
   reviewed source revision. Compare actual shared bytes independently of HEAD.
   Scout's helper baseline alone cannot establish application/task parity.

The initial inventory did not newly certify any legacy ready record. Gameplan
now has complete published source and fresh isolated verification above;
Paper's newest record has a current producer test binding but incomplete base
dependencies; UI has historical checks but lacks a completion package. These
are distinct reasons to retain work, not grounds to omit universal changes
from the eventual shared application.
