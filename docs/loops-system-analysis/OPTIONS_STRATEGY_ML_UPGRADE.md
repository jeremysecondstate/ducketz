# Options Strategy ML upgrade

## Metric meanings

- **Direction Up (ML)** is Directional Loop B's calibrated probability that the underlying finishes up over the selected horizon. It is a price-direction forecast, not the probability that an option strategy is profitable.
- **ML Profit Probability** is the separately calibrated probability that the exact candidate produces strictly positive net profit after observed bid/ask cash flows and configured contract fees. It remains null until the Strategy model has enough causally eligible training, calibration, and assessment decisions and the candidate has complete Pricing evidence.
- **Scenario Coverage** is the weighted fraction of local price scenarios that are favorable under the payoff/Greek approximation. It remains available as a research fallback, but it is deliberately not labeled or copied as a probability.
- **Expected Return** is modeled separately from probability and is bounded by the candidate's declared maximum loss/profit mechanics.

## Implemented model path

The profitability path retains the histogram-gradient classifier as the tabular baseline and adds an MLP neural-network challenger. Model choice occurs on a purged chronological tail carved from the training partition. Calibration and assessment rows are not used for that choice. Candidate neural weights of 25%, 50%, 75%, and 100% are compared with the baseline by equal-decision-weighted log loss; neural influence is admitted only after the configured improvement margin. The selected raw estimator is refit on all training rows and then Platt-calibrated on the separate calibration partition.

Loop B's calibrated direction probability and candidate direction alignment are now direct Strategy features in addition to their prior-derived effects. The Discover table exposes Direction Up separately so an unavailable Strategy-profit model no longer hides the already available price-direction forecast.

## Production readiness observed on 2026-08-20 UTC

The inspected Strategy generation had thousands of completed observed-BBO outcomes but reported zero Pricing-eligible rows because the canonical replay reader revalidated mutable annual macro and operational bar files against historical checksums. Seven such files had legitimately advanced. The reader now trusts the replay's immutable receipt, manifest, and output checksums and treats the sealed source inventory as point-in-time provenance. Incremental replay generations bind the prior immutable replay artifacts instead of relabeling their old rows with today's mutable inputs.

A read-only catalog load after the change produced 433,917 verified offline OPRA Pricing rows with no catalog errors. A read-only sample across twelve existing 1-hour outcome artifacts found 47 fully Pricing-eligible rows out of 1,584 candidates. This proves that the false corruption gate is removed; it does not prove that the minimum 252/63/63 decision cohorts have matured, that the neural challenger will be selected, or that any strategy is profitable.

## Safety boundary

The upgrade does not automate order submission, lower the causal cohort requirements, use assessment data for model choice, substitute Scenario Coverage for calibrated probability, or allow offline OPRA replay to authorize a live candidate. Existing v6 candidate files remain readable during the v7 transition; new Strategy publications use the v7 model policy.
