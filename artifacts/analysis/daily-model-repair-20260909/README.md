# Daily model repair and operating qualification

The original September 9 daily model combined 75% histogram gradient boosting
with 25% MLP. It selected identity calibration on purged development data. On
the separate final assessment, its Brier score was 0.253475397 and log loss was
0.700754830, versus training-base-rate scores of 0.249812839 and 0.692772856.

A fixed regularized-logistic grid, C = 0.001 / 0.01 / 0.1 / 1, was registered
before new assessment scoring. It used the same immutable, source-bound daily
cohort, train-only admitted features, original chronological partitions, and
existing purged calibration selector. C = 0.001 won on selection log loss:
0.695594772 versus the original best candidate's 0.709783905. The estimator was
refitted on train plus selection and locked at 2026-09-09 05:54:37.824865 UTC.
No final-assessment outcome selected its family, C, or calibration.

The locked candidate was assessed once at 05:55:29.796191 UTC. It improved
Brier to **0.250872584** and log loss to **0.694902389**, with ECE 0.045346955
across 976 rows and 51 decision clusters. It still failed the original strict
baseline-outperformance policy. That original result is retained unchanged in
`locked-candidate-assessment.json`; no further parameter search used this
assessment.

After the candidate was locked and these assessment results were known, the
operator authorized a prospective operating policy for new independent-stock
publications: Brier at most baseline + 0.005 and log loss at most baseline +
0.01, while retaining varying directional probabilities, calibration quality,
minimum decision-cluster support, exact fitted symbol/route history and source
identity checks. Under this explicit v2 policy, the locked candidate qualifies.
Its errors remain **0.001059745 Brier and 0.002129533 log loss above baseline**.
Qualification therefore does not mean measured or statistically demonstrated
baseline outperformance.

The automated native daily training now evaluates the same fixed C grid on
development selection only. The selected model's real final assessment and
versioned operating policy determine each new publication's status. Readers
and champion retention recompute the numerical criteria, reject altered
tolerances and inconsistent flags, and preserve strict v1 semantics for
immutable earlier reports. This work does not alter orders or live execution
controls.

Evidence: `preregistration.json`, `development-selection-result.json`,
`locked-development-candidate.joblib`, `locked-candidate-assessment.json`, and
`operating-policy-v2-qualification.json`. Source cohort SHA-256:
`6991e42f48cb95393bf5bf5d045d9f9dc8463f0590553be20da742d2d6f58467`.
