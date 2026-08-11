# OPRA underlying no-trade-minute protocol proposal

Status: **PROPOSED / INACTIVE / NOT APPROVED**  
Policy ID: `opra-underlying-no-trade-asof-v2`  
Version: `2.0.0-proposal`  
Semantic policy SHA-256: `c2daf73aa7265fc9fb584c4f1ec08364d62f0bb6d83b980b01abeaa1dadf70d8`

This proposal does not change the active audited rule. The current planner
continues to require an exact completed Databento `ohlcv-1m` bar ending at every
scheduled target. It therefore remains 504/504 for NVDA, 504/504 for MU, and
503/504 for GOOG. The unresolved target is the bar ending
`2026-04-10T19:00:00Z`. A direct provider check returned neighboring bars but no
trade bar for the 18:59:00–19:00:00 UTC minute. Databento documents that OHLCV
prints no record when no trade occurs in an interval.

The machine-readable proposal is
[`opra-underlying-no-trade-proposal-v2.json`](opra-underlying-no-trade-proposal-v2.json).
Its hash covers only the canonical `policy` object, so any semantic change
requires a new hash and invalidates a future approval.

## Option A: causal last-trade carry-forward

Use the latest strictly prior trade close only when all of these are proven:

1. the exact completed one-minute bar is absent;
2. a checksummed provider query proves that the target minute contained no
   trade rather than a local ingestion gap;
3. the prior trade and target are in the same exchange session;
4. the prior bar ends strictly before the target and is no more than 60 seconds
   stale;
5. price and adjustment policy are valid and unchanged; and
6. no halt, session boundary, or corporate-action boundary intervenes.

The next bar may be consulted only to audit that the minute is genuinely empty.
It may never choose the source value, affect the carried price, or widen the
window. The receipt must persist the source trade/bar clocks, exact staleness,
selection method, source checksum, no-trade query checksum, and approved policy
hash.

Advantages:

- causal at the target and free from look-ahead;
- preserves the existing last-trade/close meaning of underlying price;
- uses already acquired underlying history and changes only one known missing
  minute under the present evidence; and
- a strict 60-second ceiling prevents an open-ended stale-price fallback.

Risks:

- the last trade may be stale relative to the quote market;
- an unobserved price move during the minute can change moneyness membership;
- halt/corporate-action proofs must be explicit; and
- approving the rule changes the eligibility contract, so prior reports cannot
  be relabeled under it.

## Option B: exact historical BBO midpoint

Acquire consolidated historical top-of-book evidence and use the midpoint only
when bid and ask are finite, positive, uncrossed, causally timestamped at or
before the target, and no more than one second old. Prefer event-time
consolidated `mbp-1`/`cmbp-1`; a provider `cbbo-1s` interval whose end equals the
target can be evaluated if its quote age can still be proven. Never select a
quote from after the target.

Databento documents that BBO/CBBO interval records contain the last best bid,
offer, and sale, that the interval timestamp is its end, and that no record is
printed if neither a trade nor a BBO update occurs. It also documents internal
forward-fill behavior when only one of trade or BBO changes. Those semantics
must be recorded rather than treated as an exact fresh quote.

Advantages:

- a fresh consolidated midpoint is more contemporaneous than an older trade;
- bid/ask quality and quote age are directly measurable; and
- it provides a strong sensitivity check on whether the carried trade changes
  the eligible strike set.

Risks:

- it changes the underlying input from last trade to quote midpoint for one
  cluster, creating mixed semantics unless adopted as a complete versioned
  policy;
- interval-sampled BBO can itself contain provider forward-filled fields;
- an exact consolidated quote may not exist at the boundary; and
- historical BBO is a separate acquisition with unknown cost until metadata
  estimation and therefore needs its own maximum-spend authorization before a
  paid request.

## Recommendation and approval sequence

Recommend Option A as the proposed primary rule because it preserves last-trade
semantics and is strictly bounded to 60 seconds. Require Option B as a
sensitivity check before approval: compare the carried price, fresh
consolidated midpoint, absolute/relative difference, and the resulting eligible
contract set. If the BBO evidence is absent, stale, crossed, or changes contract
membership materially, the cluster remains `NOT_PROVEN` pending an explicit
decision; the protocol does not silently fall through.

Activation requires all of the following after the comparison exists:

- explicit operator approval naming the exact policy hash;
- a new active eligibility-policy version and new receipt schema;
- implementation tests for the 60-second boundary, 61-second rejection, session
  and halt rejection, corporate-action rejection, no future-bar selection,
  checksummed missingness proof, and immutable replay/tamper failure;
- full 504/504 rescan for each symbol plus contract-set sensitivity results;
- refreshed OPRA cost plan and authorization bound to the new policy hash; and
- regeneration of downstream evidence without reinterpreting old artifacts.

Until then, GOOG remains 503/504, all dependent gates remain `NOT_PROVEN`, and
no candidate, lockbox, automated action, or production authorization is allowed.
