# September 28 adaptive two-hour Paper rounds

The adaptive evaluation schedule is implemented and active. The first scored
round is experiment **`20260928-paper-two-hour-01`**, opened on **September 29
at 02:20:36.376708 UTC / September 28 at 19:20:36 PDT**, with verified equity
of **$43,877.58075527675**. Native lifecycle completion finished at
**02:22:58.385551 UTC**, and the durable cadence state advanced to this opening.
Its two-hour endpoint is **September 29 at 04:20:36 UTC / September 28 at
21:20:36 PDT**.

This replaces the earlier three-day improvement schedule. It does not change
the native five-minute candle/forecast recipe or authorize real-account orders.

## Competitive-round controls

The [operating contract](../PAPER_IMPROVEMENT.md) and
[cadence helper](../../../ml/hyperliquid_paper_cadence.py) start scored rounds at
two hours. A verified endpoint win increases the next round by exactly one hour:
**2 → 3 → 4 → 5**, continuing without a configured ceiling. A loss or tie keeps
the current duration. Unavailable, late and carry-in comparisons are explicitly
unscored and do not advance the interval.

The result uses the common-mark Paper equity edge and excess return after
trading costs. A win requires positive finite values plus verified matching
accounts/opening, complete empty external-flow histories, native comparability
and the native winning flag. Both portfolio endpoints must cover the full round
and arrive within one five-minute candle after its deadline; observation skew
and account-read identity checks remain enforced. A late app wake cannot claim
performance at an earlier missed deadline.

Each invocation completes at most one cycle. Early invocations leave the round
running; duplicate assessments and completed rollovers cannot advance twice.
Final assessments are immutable. Interrupted work resumes its recorded cycle
instead of creating replacement archives or openings. A winning recipe receives
the next longer challenge; scored losses/ties require a documented investigation
and evidence-backed improvement under the existing qualification, model-count
replacement, cost and risk constraints. A short-round win is not evidence of
durable profitability.

`_operations/paper-improvement-cadence.json` records the active experiment,
opening identity, duration, due time and history. `assess` records the endpoint;
`advance` binds the next verified completed opening. These commands do not edit
the app schedule themselves.

## Purchase confirmation and preserved carry-in

The user asked to finish buying Clear Pond spot holdings before the fresh
mirror. Initialization waited for their completion message, retained in
[purchase confirmation](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/purchase-confirmed.json)
at **02:16:21 UTC**. The user made the real purchases; this workflow made no
real-account orders or transfers.

The prior `20260928-paper-5m-01` run predated the committed two-hour ladder, so
it was adopted as an **unscored carry-in**. Its comparison captured Paper at
**02:16:20 UTC** and completed actual-account reads at **02:16:30 UTC**:

| Measure | Captured result |
| --- | ---: |
| Paper ledger equity | $43,694.92446083596 |
| Paper equity at common marks | $43,694.52660672262 |
| Actual accounts at common marks | $43,866.119675878945 |
| Common-mark Paper edge | **−$171.59306915632624** |
| Excess return over the opening baseline | −0.390543 percentage points |
| Simulated fees / turnover | $68.5181748385 / $121,654.15003 |
| Fills / virtual transfers | 120 / 0 |

The native comparison verified the mirror baseline, zero external flows and
comparability, with **9.79 seconds** observation skew. The negative result is
retained evidence; it is not labeled a scored two-hour loss. Actual holdings
can evolve through the user's trading, separately from external cash flows.

The complete stopped `_paper` and `_models` trees were preserved under
`C:/DATASTORE/hyperliquid/_paper_archives/20260928-paper-two-hour-01`:
**638 files / 391,737,063 bytes**, with checked SHA-256 manifest
`19ea5902dd879c3f18d7a8308a7ee775316ebe04652785e0ac923af1ac2e9586`.
The data coordinator and retained five-minute history continued independently.

- [Captured comparison](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/comparison.json)
- [Archive verification](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/preservation-verified.json)

## Recipe retained for the first scored round

The [recorded decision](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/decision.json)
retains all four active estimator families: logistic regression, ExtraTrees,
HistGradientBoosting and MLP, at normalized weights **40/20/20/20** and
calibration **C=0.1**. The recipe remains **5m/h1**, candidate fitting every
300 seconds of source-candle progress, uncapped chronological 70/15/15 splitting
with horizon purges, and the existing qualification and Paper risk gates.

At the initial read-only review, only about **34 matured forward outcomes per
market** were available. There were 116 fills and $67.72 fees through 02:09:26
UTC; approximately **43% of those fees occurred in the first ten minutes** of
fresh-mirror inventory adjustment. These observations support investigating
costs and stability, but do not justify an arbitrary architecture or weight
change before establishing the first fair timed round. Startup fees remain in
the competitive score. No family was removed or replaced in this cycle.

Fresh fitting can change qualification without changing family membership.
At final health verification, BTC and ETH qualified; HYPE and ZEC did not.
All four models continued publishing their labeled forecasts, and unqualified
forecasts remained excluded from signal allocation.

## Verified opening after the purchases

The new opening retained **10 positions**, with one opening cycle, four equity
rows and **zero fills, decisions, fees, funding, transfers and opening P/L**.
The native source reconciliation and independent accounting check passed.

| Account | Opening equity |
| --- | ---: |
| Alex | $7,251.00623924576 |
| Jeremy | $5,151.6198538094395 |
| Clear Pond | $31,474.954662221553 |
| **Total** | **$43,877.58075527675** |

Clear Pond's verified spot quantities included the completed real purchases:

| Spot asset | Opening quantity |
| --- | ---: |
| BTC | 0.2509938624 |
| ETH | 1.0002993 |
| HYPE | 30.04912963 |
| ZEC | 2.005004647 |

Clear Pond cash was **$2,569.10990727**. These are the source quantities mirrored
at opening, not a promise that subsequent Paper allocations retain them.
Spot opening entries use the observed opening marks, not historical purchase
costs. Sequential account reads and marks can produce small valuation differences
from a contemporaneous display; no balancing cash adjustment was introduced.

- [Native opening/source verification](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/opening-verification.json)
- [Independent opening accounting and inventory](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/opening.json)
- [Retained public source reads](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/opening-public-account-reads.json)

## Schedule, health and completion

The app confirmed **Hyperliquid Paper Improvement ACTIVE**, every two hours,
using **GPT-6 Astra / Ultra**. The schedule handoff was confirmed at
**02:20:54.810822 UTC**, **18.44 seconds after the opening**. The durable endpoint
remains opening plus exactly two hours; app activation offset and scheduler
jitter do not redefine the performance interval. The saved handoff records a
119-second maximum app jitter allowance. Endpoint validation still rejects
observations beyond the five-minute scoring allowance.

**Operations Watch remains ACTIVE every 30 minutes**, using GPT-6 Luna / Extra
High. Its task remains health observation and permitted recovery, separate from
the adaptive model-improvement scorecard. Scheduled operation requires the local
PC/app and project to be available.

Final health at **02:22:41.738919 UTC** passed every configured `5m/h1` slot
with no warnings, and verified committed Paper advancement to **02:22:24.574124
UTC**. Owner identities were coordinator **36524**, models **62460**, and Paper
**77256**. Powder remained off. Independent first-trading replay passed with
seven cycles, 28 equity rows and eight fills while preserving the opening.

The later health snapshot showed **$43,840.16844127309** equity,
**−$37.412314003663596** P/L and **$28.3663291595 simulated fees**, with zero
funding. These startup trading observations are separate from the unchanged
zero-cost opening. Native completion at **02:22:58.385551 UTC** was followed
by cadence advancement: the active experiment is the new opening,
`carry_in_unscored=false`, duration two hours, due **04:20:36 UTC**.

Validation comprised **63 passed / 1 skipped** lifecycle, comparison and cadence
tests, followed by **36 passing cadence tests** after endpoint and exclusion
guard review. These are overlapping validation groups, not 99 distinct tests.

- [App schedule handoff](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/schedule-handoff.json)
- [First-trading accounting](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/first-cycle-ready.json)
- [Final health](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/final-health.json)
- [Completed lifecycle record](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/operation.json)

## Prior Watch opening-hash incident resolved

Before rollover, the prior Watch memory carried an apparent `initial_positions`
hash mismatch. Read-only reconciliation of the same nine ordered prior opening
rows reproduced both hashes exactly:

| Serialization | Bytes | SHA-256 prefix |
| --- | ---: | --- |
| Native `json.dumps(rows, sort_keys=True)` | 5,433 | `79ac73dc1f0dd683` |
| Compact separators `(',', ':')` | 5,290 | `18679b862588e064` |

The decoded row content was identical, and all four native opening hashes
matched the accepted receipt. The discrepancy was JSON separator whitespace,
not changed holdings or opening accounting. Receipt comparisons must use the
same canonical helper and serializer; independent verifier formats must not
be substituted silently. No ledger correction or recovery was needed, and no
excluded experiment was read.

[Hash reconciliation receipt](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-two-hour-01/opening-hash-reconciliation.json)
preserves the full hashes, source paths and method.
