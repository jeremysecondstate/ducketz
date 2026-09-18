# September 18 YG quality follow-up

Registered at 07:48 UTC, September 18, before any revised final assessment. The operator explicitly requires all YG models/predictions to pass their existing checks and the September 18 Gameplan tab to show the completed YG. Original opening deadline: 11:00 UTC / 04:00 Pacific. Root supervision: 6e44dc66-6525-404e-8bd4-722061db5d97.

The preceding YG failed hourly retained-information and weekly Brier/log-loss checks. Those assessment outcomes are already known. They establish failure, not a license to search assessment scores. Candidate choices in this follow-up use training/development data only, and the reused final assessment is not represented as fresh unseen evidence.

Fixed changes to evaluate:

1. Raw-direction calibration selection must enforce the same directional-information eligibility on development predictions that final promotion requires. Reject constrained/flat calibrators before ranking by development log loss. Recheck the chosen family after refitting on the full calibration partition; any fallback must be a development-eligible family and reported explicitly. Legacy targets retain prior behavior. The final numerical quality gates stay identical.
2. Extend the same already specified logistic C grid (0.001, 0.01, 0.1, 1) to weekly raw-direction selection. The weekly cohort is smaller and uses the same correlated features, yet currently has only C=1 alongside complex tree/neural candidates. No additional grid or assessment-based parameter search is authorized by this plan. Preserve existing development log-loss ranking, partitions and candidate families.
3. Activate the revision only when all four directional reports and every one of the 264 saved forecast rows are promoted with exact symbol/route fitted support. Optional enrichment remains separately assessed and does not become a manual-policy prerequisite. Preserve OG and prior YG artifacts and comparison identities. PREPARING blocks new trading instructions; only a fully verified qualifying replacement may become ACTIVE.

After relevant tests and development evidence, use one native publication/enrichment/trade-planning tail with the same raw-direction target and XNAS source. Inspect each reported issue during execution. Do not retry a failed numerical assessment unchanged or select another model after viewing its assessment. A distinct verified implementation defect would require its own evidence and repair record.

No trader, order, schedule, risk-control or provider acquisition change is part of this follow-up. Live source/account/quote/ownership and order identity checks remain in place.

## Development-only follow-up registered 07:52 UTC

The weekly fixed C grid did not change its selected family; native retraining on that change alone would reproduce the preceding weekly failure. Before any revised final assessment, examine two independent, fixed development proposals once:

- Uniform probability shrinkage: each existing weekly candidate's probability is mixed with the positive rate estimated from TRAIN only, with retained-model weights 0.25, 0.5, 0.75 and 1. No zero/epsilon weights. Reconstruct HGB/MLP using TRAIN only because the saved final model includes selection rows. Rank by the existing selection log loss.
- Causal source-clock context: add exactly hours after the source session's regular close and hours from source-bar completion to next action start, derived from existing saved timestamps. Existing predictors conflate regular-close and late-session feature snapshots despite a large observed train/development shift. Compare the same weekly families/grid using unchanged TRAIN/selection boundaries.

These are independent studies of low-complexity probability regularization and omitted causal context, not a combined parameter sweep. Full development results, including failures, are retained. Choose a justified improving proposal using development log loss and availability evidence before running revised final assessment; do not combine or expand the grids after viewing assessment. An unchanged or worse development result is not a reason to run another identical final assessment.

## Final candidate policy locked 07:55 UTC

The source-timing proposal lost: its best development log loss was 0.709806570467, versus the unchanged candidate's 0.703697924502. Do not integrate those features.

The standalone weekly probability-shrinkage grid improved development log loss to 0.7011636448 and Brier to 0.2537161150. The winning candidate is the HGB/MLP blend with 25% neural weight, retaining 50% of its probability deviation from the TRAIN base rate. Selection probabilities remain varying (0.43714–0.70953, standard deviation 0.05659). This is a development score improvement, not an accuracy or future-profit claim.

Integrate exactly the fixed four shrinkage weights uniformly across the nine existing weekly raw-direction candidate families. Rank original selection log loss, preferring unshrunk weight 1 on an exact tie. Selection uses TRAIN base rate; the selected estimator's final refit uses TRAIN plus selection base rate. Apply only to raw weekly models; preserve legacy behavior and other horizons' candidate sets. Record the wrapper, base family, chosen weight, base rate, policy and all development metrics. No zero or epsilon shrinkage, source-feature combination, altered partition, fabricated data, or changed final gate.

Run one revised native publication/enrichment/trade-planning preparation after tests. Final assessment remains a verification under unchanged operating criteria; because this dataset has been inspected previously, successful assessment is not independent proof of prospective improvement. If it fails, retain that result and do not search its assessment outcomes.
