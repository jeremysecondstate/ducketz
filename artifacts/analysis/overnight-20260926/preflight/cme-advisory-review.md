# Current CME advisory review

The new September 26 **04:25:18.323646 UTC** diagnostic is a continuation of the known optional cross-asset context limitation. It rejects the same **September 3 21:00 UTC** candidate and NQ BBO source, whose recorded staleness has increased from 21 days to **22 days 07:25:19.248342413** against the unchanged **15-minute** maximum. Severity remains `advisory`, type `CmeCrossAssetQualityError`, input policy `persisted_rows_only`, and `provider_rows_preserved=true`. The exact message changes with elapsed age; it is not byte-identical to yesterday's message.

All six current CME scopes returned successful native fetch records. The context MBP request succeeded on its second attempt after one transient 504. Current normalized captures contain every configured symbol with the expected continuous/raw-symbol identity and `GLBX.MDP3` dataset:

| Scope | OHLCV rows | BBO rows | MBP rows | MBP request saturated |
| --- | ---: | ---: | ---: | --- |
| Continuous context | 4,499 | 310 | 4,996 | True |
| Literal contracts | 3,600 | 248 | 4,996 | True |

Both MBP requests retain five window shrinks and approximately 35–38 seconds of observed book events near September 25 20:14 UTC. Normalized counts below 5,000 do **not** override the explicit saturation flag or establish complete book coverage. BBO expanded its empty initial range twice and contains observed events through approximately September 25 20:59:59 UTC. No configured symbol is missing from these current captures.

The literal scope remains ESZ6, NQZ6, CLX6 and GCZ6. Fresh raw BBO instrument IDs match all four corresponding continuous symbols. No raw-contract configuration correction is indicated. The context reader still prefers persisted event partitions to the flat captures: inventory remains **18 OHLCV, 290 BBO and 295 MBP partitions**. This review inspected their paths/counts/timestamps, not their retained payloads. The fresh flat captures therefore do not silently repair the stale partition-backed candidate.

No new actionable data defect or reason to restart the pipeline was found. Preserve the optional derivation rejection, capped-book exclusion, source identities, original provider rows and quality gates. This is not a successful derived-context or completed-provider-stage claim. No provider/broker calls or production changes were made.

Detailed diagnostic comparison, capture metadata, mappings, source hashes and native log prefix are in `cme-advisory-review.json`. The bounded read-only helper is `review_cme_advisory.py`.
