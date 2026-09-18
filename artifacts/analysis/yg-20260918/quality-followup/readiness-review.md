# September 18 YG quality-readiness review

Written 2026-09-18T07:56:41.574626+00:00.

This review concerns the first YG publication `20260918T072555.813034Z`, before the authorized quality revision. It does not claim that a replacement has passed or been activated. Inspection used saved files and read-only app adapters; no broker calls, fitting, production handoff or UI control occurred.

## Exact scope and current result

“All predictions pass” means all four directional model reports pass their recorded promotion gates, every one of the 264 rows has `PROMOTED` status and positive fitted support for its own symbol and route, and the complete source-bound preparation is available. Passing does not make the 55 context/outlook rows entries. There remain 209 execution windows across eleven stocks.

| Horizon | Total rows | Execution rows | Context rows | Current quality result |
|---|---:|---:|---:|---|
| 1h | 154 | 143 | 11 | Not promoted: flat calibration |
| 4h | 44 | 44 | 0 | Promoted |
| 1d | 55 | 11 | 44 | Promoted |
| 1w | 11 | 11 | 0 | Not promoted: Brier and log-loss checks |
| Total | 264 | 209 | 55 | 99 promoted; 165 not promoted |

The fixed v2 gates require at least ten assessment decision clusters, expected calibration error at most 0.15, retained variation in both calibration and assessment probabilities, a non-flat calibration with both classes represented, Brier no greater than its training baseline plus 0.005, and log loss no greater than baseline plus 0.01. Qualification under these tolerances does not assert baseline outperformance. Each source/report/model/cohort must retain matching identities and actual fitted artifacts.

The 1h calibrated probability is a constant 0.4955640050697085 in calibration and assessment. Its quality failure is directional information, despite acceptable probability-error scores. The 1w assessment-minus-baseline gaps are 0.0123296247077816 Brier and 0.0267662018050007 log loss, exceeding the two tolerances. These are the two unresolved quality results in this publication. See the [saved model review](C:/dev/ducketz/artifacts/analysis/yg-20260918/model-review.md).

## Source and fitted-support limits

All 264 forecasts have positive exact-symbol and exact-route fitted support; there are zero `RESEARCH_NO_TARGET_HISTORY` rows. There is no current missing-route gate that training must first resolve. Minimum fitted rows for one route are: 1h 2 (TWST), 4h 18 (TWST), 1d 1 (TWST), and 1w 6 (TWST). These sparse counts are disclosed evidence limits; they are not a license to copy another route's history.

Thirteen forecast routes have no observations in their own held-out assessment slice: CROX 1h at 04:00, 05:00, 15:00 and 16:00; CROX 4h at 12:00; all five CROX daily routes; CROX weekly; TWST 1h at 04:00 and 15:00. Their pooled horizon model still has 63 assessment decision clusters, and the existing directional gate does not require a per-route assessment minimum. Training alone cannot invent missing venue observations or turn zero route-specific assessment rows into local validation evidence.

The native five-minute endpoint rule excluded 61,813 hourly, 19,584 four-hour, 33,765 daily and 6,734 weekly candidate labels. These exclusions are retained; the permitted planning carry does not enter model labels. The current informational trade plan separately has 154/154 available hourly prices and the disclosed seven-minute CROX planning anchor. These planning prices do not repair absent training/actuals observations.

## Manual strategy, optional enrichment and display

The currently selected manual policy consumes saved instructions through `gameplan_execution.execution_frame`/`execution_preflight`. Its ready status checks the selected deployment and readable instruction identities/windows; it does not mean every model is promoted. Its read-only preflight returned `READY`, 209 execution windows, pinned to the first YG. The optional learned-enrichment models have zero qualified scopes and are not required by this manual strategy. Legacy fixed and learned policies retain their separate model gates.

The current direction contract is `stock-direction-50-v2`: probability above 0.50 is bullish, below 0.50 bearish, and exactly 0.50 has no edge. The manual holding policy accumulates bullish shares and sells own-horizon shares on bearish instructions without automatic expiry sales. Actual cash, holdings, reservations, quote/session/ownership checks and reliable order identities remain live requirements. This review performs none of those broker operations.

Read-only app-adapter checks succeeded: the Gameplan adapter loads “Yung Gameplan (YG)”, September 18, 264 forecasts, eleven stocks, ten conditional actions and a COMPLETE projection from trade plan `20260918T072910.923004Z`. The Rolling Forecasts adapter loads 264 routes and eleven weekly snapshots with no pending symbols. It correctly warns about hourly flat calibration and each weekly validation failure. Rolling Forecasts can display uncalibrated raw scores for a flat model with that warning; the Gameplan review preserves its saved published probabilities. Adapter readiness is verified; no claim is made about the currently visible desktop frame without a separate UI inspection.

## Implemented safe quality revision

The previous API allowed only initial `prepare` and `activate`: `prepare` rejected an existing deployment, and `activate` required PREPARING. The authorized extension is now [prepare-revision](C:/dev/ducketz/ml/gameplan_deployment.py), exposed as:

```text
python -m ml.gameplan_deployment prepare-revision --action-date 2026-09-18 --reason "Operator-authorized YG quality revision"
```

This operation requires an ACTIVE matching deployment before the original 04:00 Pacific deadline. It freezes the original OG/OG trade references, prior active YG/native/trade/enrichment references and previous registry receipt. The prior files remain immutable. It compares the registry and all three selected-source pointers before publishing PREPARING; independently advanced references abort the handoff. PREPARING uses the existing guard to reject new instructions while the replacement is prepared.

Revision activation requires a new YG publication and new source-bound native, trade and enrichment outputs, all native preparation stages complete under the original deadline, matching action date/universe/source/forecast windows, unchanged retained history and `require_all_directional_models_promoted=true`. The gate numerically validates all four promotion reports, verifies fitted artifacts with the existing native verifier, and checks all 24 rows per configured stock are promoted with matching positive symbol/route support. Optional enrichment must be correctly fitted and bound to the new source; its qualification is not required. Activation retains OG for evaluation and prior YG as immutable history.

Validation: 65 handoff/execution tests passed, including successful revision activation, both model-failure types, unsupported/mismatched fitted support, unchanged prior evidence, old-YG reuse rejection, source/registry races, deadline/reason checks and unchanged owned-exit behavior. No production `prepare-revision` or `activate` was executed by this review.

The separate [quality output verifier](C:/dev/ducketz/artifacts/analysis/yg-20260918/audit_outputs_quality.py) accepts explicit `--native-run` and `--gameplan-run`, refuses the first YG, writes only this quality-followup directory, and checks the four immutable baseline groups plus all-model/264-row qualification and cash/share/source conservation. It has been syntax-checked and is awaiting the new candidate; the earlier 345-check output review is preserved.
