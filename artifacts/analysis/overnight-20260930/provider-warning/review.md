# Current provider-warning triage

Reviewed: 2026-09-30T05:41:03.568976+00:00. Native Loop A completed.

9 Databento warning occurrences identify September 30 as degraded. That UTC day was still in progress at acquisition, and the warning concerns current-day CME context extending after the completed September 29 stock-session boundary. The saved evidence does not establish that incomplete-day publication is the sole cause; the vendor quality warning remains explicit.

All six CME requests succeeded and persisted their configured symbol scopes. OHLCV context/contracts contain 2,389/4,079 current rows; BBO contains 300/240. Both MBP captures contain 5,000 rows and remain explicitly limit-saturated. Adaptive effective request bounds are recorded; successful delivery is not full-range completeness or model eligibility.

The separate retained CME derived-context rejection matches the previous candidate, policy and 15-minute tolerance; only the stale NQ BBO age advances with receipt time. Its native guard remains enforced. Eleven technical summaries reported zero failures.

No provider calls, retries, production edits, configuration changes, gate changes or order actions were made by this triage. Final source/coverage audits remain separate.

Evidence: final-triage.json, latest.json, immutable warning-review-*.json snapshots and cme-derived-advisory.json.
