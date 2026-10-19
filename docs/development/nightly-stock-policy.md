# Permanent stock policy — October 19, 2026

Jeremy's October 19 direction applies permanently to every configured stock symbol on Atlas and Scout. It covers the combined 22-symbol Gameplan trader, Atlas's independent 15-minute trader and Scout's independent 15-minute trader. Nightly preparation, saved publications, the Gameplan display, Stats provenance and scheduled operating instructions retain this policy on subsequent nights.

## Direction and horizon decisions

| Saved directional probability | Current trade direction |
| --- | --- |
| At least 50.01% (`p >= 0.5001`) | BUY / Bullish |
| At most 49.99% (`p <= 0.4999`) | SELL / Bearish |
| Strictly between 49.99% and 50.01% | HOLD / No edge |

The equality boundaries are included. Apply the same cutoffs to each trader's own saved probability, including both 15-minute traders. Preserve each model's target definition and original numerical probability. An additional confidence threshold, model-assessment gate or probability-sized near-zero allocation must not suppress an otherwise valid directional instruction. Actual executable quantity still follows available cash, eligible unreserved shares, live quotes and the existing order controls.

The 15-minute traders retain their independent horizon decisions. Gameplan's 1-hour and 4-hour instructions can use eligible shares from any Gameplan horizon in their own execution state. Their cross-horizon sales have no donor-only hierarchy, fallback slot quota, daily donor cap, same-clock bullish-donor exclusion or ownership permission from another Gameplan horizon. The 1-day and 1-week ownership rules remain unchanged. Each trader retains its own state, orders, fills, logs and manual controls; another trader's ledger, health or acceptance is never an operating input or prerequisite.

## Gameplan, Stats and saved history

The Gameplan forecast/action tables and details omit the `Planning price` and `Execution mid` estimate columns. Live execution uses actual bid, ask and midpoint quotes, cash, holdings, order responses and fills. Historical planning scenarios retain their original observation times and conditional meaning; they do not set a permitted live execution price.

Save the current direction-policy identity and inclusive cutoffs with new forecast publications and task-produced policy summaries. Stats preserve original forecast probabilities, targets, saved direction labels, policy versions and observed outcomes. Evaluate a historical forecast under its frozen saved label; identify absent historical policy metadata as unrecorded. An operating policy change must not rewrite past predictions, turn an unobserved outcome into a result or reclassify prior performance under the new thresholds.

A policy correction to an already published Gameplan creates a new immutable artifact, preserves the original source forecasts and account observation time, and records the actual revision time plus all parent-plan identities. Retain every original publication and receipt. The correction's saved account basis remains historical; the manually started trader still reads current execution inputs.

## Every nightly run

Produce both local Gameplan research packages, score matured original forecasts, synthesize the two Gameplans once, publish the same combined Gameplan and Stats to Atlas and Scout, and independently prepare both 15-minute traders for manual start. Use the existing dated workflow, completion identities, schedules, private exchange and local bindings. Preserve completed work and resume unfinished work through real input or operating errors.

These duties remain permanently authorized under Jeremy's standing direction. Peer status, source publication, review metadata, another trader's state or a request for reciprocal acceptance must not delay nightly work. Preserve actual manual trader stops. Preparation never starts, restarts or places trades through a trader.

## Reusable scheduled-task instruction

Use this self-contained block in the applicable existing Atlas and Scout task prompts. Preserve each task's responsibility, model, cadence, native identity and local operating bindings when updating its instructions.

```text
Permanent October 19, 2026 stock policy: retain docs/development/nightly-stock-policy.md across every nightly run. For every configured stock symbol, the combined Gameplan trader and both independent 15-minute traders use their own saved probability with BUY at p >= 0.5001 (50.01%, inclusive), SELL at p <= 0.4999 (49.99%, inclusive), and HOLD only strictly between those cutoffs. Preserve the original target and probability. Do not reintroduce a confidence threshold, model-assessment gate or probability-sized near-zero allocation that suppresses a valid instruction. Gameplan 1h and 4h can sell eligible shares across any Gameplan horizon in their own execution state without donor hierarchy, fallback slot quotas, daily donor caps or same-clock bullish-donor exclusions; 1d and 1w ownership rules remain unchanged. Keep each 15-minute trader independent and preserve all three traders' own state, orders, fills, logs and manual controls. Omit Planning price and Execution mid estimate columns from Gameplan. Actual execution still uses current bid/ask/midpoint quotes, cash, holdings, order state and existing order controls. Save current policy metadata with new outputs; Stats retain and score original saved direction labels and observed outcomes under their recorded policy, identifying missing historical policy metadata as unrecorded. Preserve original publications and revision ancestry. Every night, complete the two Gameplans, their single synthesis and publication, combined Stats and independent 15-minute preparation under standing human authorization. Do not add approval requests, peer acceptance, source-transition waits or cross-trader prerequisites. Resume unfinished work, preserve manual stops, and leave every trader for Jeremy's manual start; preparation never starts, restarts or trades through a trader.
```

## Compatibility patch for installed nightly runbooks

These three runbooks already exist in the reviewed local operating checkout. They are absent from the publication base `98b5cfcf644c6776b1f598d5168a12ac4ee3acef`; the exact task-only patches below let an existing Atlas or Scout installation add the permanent policy references while preserving its other instructions. Main needs only this canonical policy document. The source hashes bind the reviewed initial and final local files; review and apply the individual hunks to a peer whose surrounding content differs. Diff lines use LF.

| File | Initial SHA-256 | Final SHA-256 |
| --- | --- | --- |
| `docs/development/nightly-operations.md` | `2d9c81d59e218f7adb8843ab52a299160e36f23c92d1179dc0cf79ab54a3b59c` | `0c6769fd75977824945cf389deca0f615ed28f43b90e4a3c3f1aba2d1d10fda9` |
| `docs/development/nightly-workflow.md` | `c8f39b74cc88e53b62ff84598d47f57ef026fea263b1ecafa8385550558fae72` | `3e4d57de10cecf62eac1ce6b3d1262b9366c235140dcef4360acde559e3ac02d` |
| `docs/development/independent-traders.md` | `d5ea48953f906941722bbeec507e41340e2eea9970f9bc051f4fc62f6eb03e10` | `39bbc192bc817f2658912b51d54c72ea588c24f4f60f93db09b688933b66bcad` |

Review confirmed that the patch preserves the standing independence, continuous nightly recovery, private-data, manual-stop and manual-start rules. It adds the same direction/horizon policy and saved-history instructions to every existing nightly responsibility without adding another pipeline or operational approval stage. No scheduler settings or application modules are changed by this patch.

Offline policy-boundary and execution fixtures passed on the final application source:

```text
.venv/Scripts/python.exe -B -m pytest tests/test_stock_direction_policy.py tests/test_oct19_intraday_direction.py tests/test_oct19_direction_planning.py tests/test_oct19_direction_execution.py -q -p no:cacheprovider
137 passed in 6.15s
```

The four policy/runbook documents also passed the existing offline Markdown hygiene check. Applying the zero-context compatibility diff to isolated original copies with `git -c core.autocrlf=false apply --unidiff-zero` reconstructed all three final files byte for byte. These are documentation and offline fixture results; each PC records its actual task-instruction installation separately.

```diff
--- a/docs/development/nightly-operations.md
+++ b/docs/development/nightly-operations.md
@@ -7,0 +8,2 @@
+
+The [permanent October 19 stock policy](nightly-stock-policy.md) also governs every nightly output and applicable scheduled-task prompt. The combined Gameplan and both independent 15-minute traders use inclusive 50.01% BUY / 49.99% SELL cutoffs. Gameplan 1h and 4h can sell eligible shares across any Gameplan horizon without donor quotas; 1d and 1w ownership rules remain unchanged. Gameplan omits the Planning price and Execution mid estimate columns. Stats preserve and evaluate original saved forecast labels and outcomes under their recorded policy, while new outputs retain the current policy metadata. Current quotes, cash, holdings and order state remain execution inputs.
@@ -20,0 +23,2 @@
+
+Retain the reusable scheduled-task instruction in [the permanent stock policy](nightly-stock-policy.md#reusable-scheduled-task-instruction) when updating these existing responsibilities. Apply it within each task's own scope while preserving its settings and independent trader boundaries. Policy persistence adds no approval, source-transition wait, reciprocal acceptance or automatic trader start.
--- a/docs/development/nightly-workflow.md
+++ b/docs/development/nightly-workflow.md
@@ -7,0 +8,2 @@
+
+Every run also follows the [permanent October 19 stock policy](nightly-stock-policy.md): inclusive 50.01% BUY / 49.99% SELL for Gameplan and both independent 15-minute traders, Gameplan 1h/4h autonomy across eligible Gameplan horizon holdings without donor quotas, and unchanged 1d/1w ownership rules. Keep the Planning price and Execution mid estimate columns out of Gameplan. Preserve original Stats labels, targets, probabilities and observed outcomes; record current policy metadata with new outputs.
@@ -16,0 +19,2 @@
+
+Retain this policy in the existing nightly scheduled-task prompts using the [self-contained task instruction](nightly-stock-policy.md#reusable-scheduled-task-instruction). Model assessment and a second confidence threshold do not suppress a valid direction. Actual executable quantities continue to use current quote, cash, holdings and order evidence. Historical publications remain immutable and policy corrections retain their source observation time, actual correction time and parent-plan identities.
--- a/docs/development/independent-traders.md
+++ b/docs/development/independent-traders.md
@@ -32,0 +33,22 @@
+
+## Permanent stock policy — direct human direction, October 19, 2026
+
+The [permanent stock policy](nightly-stock-policy.md) applies to all configured
+symbols, the combined 22-symbol Gameplan and both independent 15-minute traders.
+Each uses its own saved probability: BUY at or above 50.01%, SELL at or below
+49.99%, and HOLD strictly between those cutoffs. Preserve the original numerical
+probability and target definition. A confidence or model-assessment gate must
+not suppress an otherwise valid directional instruction.
+
+Gameplan 1h and 4h can sell eligible shares across any Gameplan horizon in their
+own execution state without donor hierarchy or fallback quotas. The 1d and 1w
+ownership rules remain unchanged. All three traders retain their independent
+state and manual controls. Real quotes, cash, holdings, orders and fills remain
+execution inputs.
+
+Gameplan omits the Planning price and Execution mid estimate columns. New
+outputs retain the current direction-policy metadata, and Stats preserve and
+evaluate original saved labels and observed outcomes under their recorded
+policy. Keep this policy in every applicable existing nightly task prompt using
+the [reusable instruction](nightly-stock-policy.md#reusable-scheduled-task-instruction).
+It adds no approval, peer acceptance, source-transition wait or automatic start.
```
