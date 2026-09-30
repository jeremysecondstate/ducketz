# September 29 source / September 30 action verification

The native root is `C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z`; its verified completed Loop A cycle is `20260930T040724.527431Z-pid60120`. The initial helper preparation incorrectly inferred the virtualenv launcher PID 61720. `cycle-binding-review.json` preserves that correction and the initial preparation; final helpers derive the saved cycle identity from its matching native COMPLETE log and saved receipt. The expected action date and original hard deadline are September 30 at 04:00 Pacific (`2026-09-30T11:00:00Z`). Provider daily coverage ends exclusively September 30. Preparation aims for 03:30 Pacific without extending a healthy completed fit.

These helpers write only this verification directory. They never fit models, acquire provider data, snapshot a broker account, claim supervision, reconcile ownership, mutate production, start a trader, or submit orders. The root operator separately owns the supervision claim and active health review. Preflight separately verifies controls, schedules, trader identity and ownership preservation.

`watch_reviews.py` observes native stage reports every 20 seconds, runs bounded report/row arithmetic when publication and sizing are complete, and dispatches the final audit only after the eight recorded stages and native receipt are COMPLETE. It stops for diagnosis on a native failure or audit error. A resumed native descendant requires explicit verified ancestry adaptation; the watcher never starts a replacement.

The final command is:

```powershell
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260930/verification/run_final_checks.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z
```

Do not duplicate a running or completed reconstruction. The sequential runner records each command, time and logs in `audit-runner.json` and stops on the first failed helper.

1. `verify_completed_run.py` verifies native ancestry/log receipts, 264 forecasts/intents and 209 entry rows across eleven symbols, all 33 OPRA cursors, source-day XNAS coverage, exact reconstruction of archive features/cohorts/current probabilities, sizing admission, snapshots, chronological cash/share conservation, all planning prices and synthetic evidence, cumulative evaluation against saved universes, and original prior-session actuals.
2. `verify_yg_completion.py` reproduces saved estimator inference and assessment metrics, partitions, symbol/route support, development-only selection/calibration, and exact promotion arithmetic without fitting.
3. `audit_provider_completion.py --hash-current` verifies the exact observed Loop A cycle, all five provider scopes, 33 OPRA source scopes and 66 raw/normalized hashes, exact zero cost/provider range/finite count/capacity evidence and bounded optional advisories. Historical delivery is expected; verified Live delivery or a successful resumed fetch needs source-specific adaptation.
4. `review_models.py` produces readable score/limit comparisons with the frozen September 29 publication `20260929T061339.593647Z` and separates fitted sizing from qualified sizing. It accepts honest failed quality statuses.
5. `audit_optional_pricing.py` verifies the 99 optional route gates and their numerator/fraction/threshold arithmetic, exact fallback features and 51 compact source bindings against the previous verified authority.
6. `review_loop_b_weekly_prefix.py` derives the Loop B run from the pinned Gameplan. Tuesday-source remaining-week evidence should contain Wednesday through Friday: 77 LIVE forecasts, all 99 intelligence routes, and 22 explicit nonapplicable d4/d5 rows. This separate dynamic output never changes the independent Gameplan's 264 rows and five-session weekly windows.

`audit-environment-baseline.json` captures 268 Python/doc/watchlist files during Loop A before model/publication stages. Stock-pipeline sources and contracts are unchanged from the prior audit; only `ml/hyperliquid_paper_policy.py` differs from that prior baseline. The new baseline must never be silently overwritten. Any later source change stops auditing until exact bytes and dependency relevance are reviewed. `preparation.json` records source and adapted helper hashes; `helper-review-tests.json` verifies bounded readiness, syntax, dates and numerical guard behavior.

Archive verification covers raw/normalized sources and request headers, feature/date/sample/exclusion reconstruction, exact minute endpoints and second/minute consistency without duplicate examples. It does not independently decode every raw second DBN record. Original cursor snapshots are self-hashed and semantically bound to the extension manifest; the native receipt does not separately bind the outer cursor-snapshot file. Required missing prefixes are recomputed at runtime; zero new requests with verified existing coverage is valid.

Current contracts in native code/docs use `stock-direction-50-v2`, signal-driven holding without scheduled expiry sales, planning v3 and `sparse-session-planning-reference-completion-v2` with up to 240 same-session minutes after regular close. These differ from older automation wording. Audits report the actual immutable policy, preserve original observations and the five-minute training/actuals rule, and change no policy or gate. Explicit unavailable price references remain unavailable.

## Completed verification

Native work completed all eight stages at 06:30:57Z. Both bounded model reviews passed. The initial final-runner guard stopped before any heavy check because a concurrent Hyperliquid cadence edit changed one captured file. `reviewed-isolated-code-drift.json` and `.diff` bind the exact original/current bytes and the independent nine-check, 169-module native dependency review. The original baseline and stopped `review-watch.json` remain preserved; `first-audit-attempt` contains its exact copy. Only the reviewed unrelated file hash is admitted.

The complete 12-section reconstruction ran once, from 06:34:46Z to 06:48:05Z. All seven final helpers then completed successfully at 06:50:01Z. `audit-runner.json` is the successful final receipt. `audit-findings.json` and `.md` bind the results and limitations, including actual observed gaps, model scores, source exclusions and the unresolved cause of the current CME vendor quality warning. No native stage was restarted and no training was repeated. Do not rerun the completed September 29 source session.
