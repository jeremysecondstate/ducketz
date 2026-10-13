# Immutable Gameplan contracts and overnight history

Reviewed October 11, 2026 on Atlas. This is a mixed implementation and historical
reference. The current preparation owners and recovery procedures are in
[nightly operations](CURRENT_SYSTEM.md) and the
[Stats-first workflow](CURRENT_SYSTEM.md). The older **Loops
Overnight Gameplan (LOG)** task is paused on Atlas; its retained modules still
provide numerical stages. Its old prompt is not the current launcher.

The review used Ducketz HEAD `90704d21801f60426493bd6cc650a7fac38d4f61` **and
the separately inventoried working-tree changes**, including the Stats-first
coordinator, account adapter and UI readers. Source, configured schedules,
installed files, saved completion and a running process are different evidence.
See [RELEVANCE_REVIEW.md](RELEVANCE_REVIEW.md) for scope and recovery of this
document's full historical edition.

## Current preparation and ownership

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

Stats are scored before model review and training. Each PC's research list remains local. The combined plan uses original forecasts and one account balance; never add two independently sized cash plans. Preserve actual deadlines and publication times. Routine recovery is already authorized and must not fabricate data or backdate a late forecast.

Historical LOG attempts retain their original stage order and source contracts. They are reference history, not a command to restart the old supervisor.

## Operating day

The stock action window is 04:00–17:00 Pacific on supported exchange sessions.
Provider release clocks, stock sessions, listed-options sessions and actual
broker availability are separate. A completed options-session observation may
be hours old during overnight preparation and still be the latest source
observation; that does not satisfy live order-time quote checks.

Read membership from each PC's configured watchlist, and validate each saved
publication against its own frozen universe. Historical row counts must not
be used as today's membership. Additions follow
[research onboarding](RESEARCH_SYMBOL_ONBOARDING.md) and
[symbol onboarding](SYMBOL_ONBOARDING.md).

## Sequential stages

`ml/overnight_runtime.py` retains both `LEGACY_STAGE_ORDER` and
`STATS_FIRST_STAGE_ORDER`. In the Stats-first route, Loop A close acquisition
and declared stock history precede `gameplan_stats`. The coordinator stops that
segment, verifies Stats and obtains its bounded review, then starts a distinct
training segment. Directional generation, evaluation, publication, optional
enrichment and local planning retain their own dependencies and receipts.

Stock-only preparation omits Strategy profit training/candidate generation and
publishes explicit non-executable option placeholders. It still checks required
OPRA history. The Stats-first route does not append the legacy account
preparation/actuals tail merely because a private account binding exists.
Keep actual package, synthesis and delivery outcomes. Atlas can finish the combined plan directly; returning it to Scout never creates a reciprocal acceptance prerequisite.

The old order—fetch, Directional generation, evaluation, fitting, successor
publication, enrichment, planning, then actuals—is retained only for its
historical attempts and compatibility interfaces. Do not start LOG to reproduce
that order for a new Stats-first session.

### Completed-session Gameplan results

`ml.gameplan_actuals_review` has a completed-session interface usable before
successor training, alongside its older successor-bound interface. Daily Stats
selects eligible original independent forecasts saved strictly before that
session's 04:00 opening and separately matches any saved planning estimates.
An authorized late publication does not make that forecast eligible for an
earlier opening. No eligible history produces an explicit no-history review,
not invented forecasts or zero accuracy.

Frozen target windows and actual observation times remain separate from the
review's creation time. Outcomes beyond the completed session remain pending;
mature windows without valid endpoints await data. Request coverage and actual
bar availability are different evidence. Missing observations are never supplied
from hypothetical planning prices or another source.

Daily Stats and the cumulative evaluator are distinct products and populations.
The Stats UI filters unpromoted rows, excludes pending/missing/neutral calls
from directional accuracy, and can include evaluated neutral rows in Brier
evidence. Raw-direction accuracy and the edition's binary probability target
must be named separately. Market-price outcomes are neither fills nor realized
account profit. Combined Stats retains both producer sources and sums eligible
counts; it does not average percentages or manufacture missing results.

### Account-aware trade-plan review

Forecast publication and planning are distinct immutable artifacts. A local
trade plan binds the selected forecast receipt and includes `trade-plan.parquet`,
`planning-price-path.json`, `direction-ledger.json`, report, manifest, receipt
and readable `Gameplan.md`. Combined planning uses the original producer
forecasts and prices with one coherent account snapshot; it does not add two
cash balances. See [joint capital](JOINT_ACCOUNT_CAPITAL.md) and the current
[exchange contract](CURRENT_SYSTEM.md).

**Projected Trade Quantity** is a standalone whole-share capacity alternative;
it is not a collection of simultaneous orders. **Direction Based Trade Qty**
belongs to one chronological conditional ledger. Cash, holdings, pending orders,
outside exposure and ownership must have explicit provenance. Planned sale
proceeds may fund later conditional purchases; only current broker-usable cash
and actual execution evidence can fund live purchases.

New signal-driven plans use above-50% bullish, below-50% bearish and exactly-50%
neutral instructions, with saved abstentions preserved. The selected holding
policy accumulates bullish purchases and sells on eligible bearish instructions;
forecast expiry measures accuracy and does not itself liquidate those holdings.
Historical fixed-duration plans retain their own exit semantics. Bounded
longer-horizon sales require the separately frozen
[fallback policy](CROSS_HORIZON_BEARISH_FALLBACK.md).

Planning prices use observed prior-close-to-clock relationships and a conditional
median ±20-basis-point working range rounded outward to cents. Wider historical
percentiles are separate stress evidence, not confidence guarantees. Current
trade-plan publication uses **504 exchange sessions** of lookback; the generic
price-band helper's default remains 120. At least two valid pairs are required.
The range is never a live execution limit or a reason to reject a current price.

The bounded planning-only closing-reference policy may carry the same session's
last observed close from at/after regular close to 17:00 for no more than 240
minutes, measured from minute completion, only with complete verified source
coverage. Synthetic minutes are explicitly marked, retain the actual observation
time, use constant OHLC/zero volume and represent an assumption, not new provider
data or proof that no trade occurred. Historical pairs disclose carried closes.
Training targets, observed archives, evaluation and actuals receive no synthetic
rows; their five-minute endpoint rules remain intact.

Newer derivations bind `cash-aware-gameplan-trade-planning-v4`, price-band/path
v3 and `sparse-session-planning-reference-completion-v2`. Older versions keep
their saved semantics. Missing valid references can produce a complete
informational forecast view with `UNAVAILABLE_PRICE_REFERENCES`, unavailable
quantities/projection and no fabricated cash ledger. This is not full execution
readiness. An informational refresh reuses the original source, snapshot and
cutoff; it cannot fetch a new account, retrain or extend a deadline.

## Historical training integration approved September 23, 2026

This dated change introduced `--archive-history` and the
`xnas-archive-prior-session-features-v1` contract. Its source rules remain useful:
verified daily/hourly history supplies causal features, while all four horizon
targets retain their exact declared minute endpoints. A new source/feature
contract cannot silently reuse an incompatible champion. Existing attempt flags,
source pins and deadline remain frozen on resume.

Prefix acquisition requires its exact cost, included-range, disk and native
source checks and preserves current cursors. Daily/hourly aggregation requires
actual minute observations and verified interval coverage. Warmup, gaps, split
and discontinuity exclusions remain explicit. Overlapping seconds/minutes must
agree; identical resolutions do not create duplicate examples. Undefined prices
are excluded with evidence. Optional sizing-feature exclusions neither impute
market observations nor remove directional targets.

The dated 03:30 target was a preparation buffer before the ordinary 04:00 bound,
not evidence of completion or a current independent task schedule.

### Permanent stock history delivery fallback

The implemented fallback changes XNAS.ITCH delivery, not target identity. It
tries available Historical evidence first and admits bounded same-dataset Live
replay only under existing native access and complete request/mapping/replay
evidence. Access denial does not authorize another dataset or purchasing a
license. Verified replay partitions remain separately receipted and reusable;
they do not advance Historical cursors or fill missing bars. Historical coverage,
entitlement and actual endpoint availability still need their own evidence.

### Explicit stock-only preparation

Stock-only describes omitted options preparation, not weakened stock training
or a claim that options calibration succeeded. The publication retains `24 × N`
stock rows and the same number of `NO_TRADE_STOCK_ONLY` option placeholders,
without legs, candidates or Strategy execution authority. Optional learned
sizing qualification is separate from the explicit fixed-budget or manual
Gameplan policy. Resume compatibility and onboarding require their original
recorded scope; old commands are not permission to change an attempt.

## September 18 Yung Gameplan deployment

The September 18 selection of YG over OG is historical deployment provenance.
Its surviving versioned contract is `raw-price-direction-v1`: strictly positive
observed raw return is the positive class. Missing historical target metadata
retains the older cost-adjusted OG meaning. Champions and evaluations must not
cross these identities. Costs/profitability remain separately measured.

Development-only candidate selection, probability-information checks, purged
chronological partitions and explicit promotion policies remain relevant.
Current v2 baseline tolerances (+0.005 Brier/+0.01 log loss) do not establish
outperformance. The dated selection/revision records preserve original sources
and deadlines; neither their historical commands nor this document authorizes a
new activation. The September 14 late-opening and onboarding exceptions were
also date-specific and have expired.

## September 30 prospective cross-horizon fallback

New qualifying publications on/after October 1 bind the exact supported policy
in forecast JSON, manifest configuration and receipt. Older publications without
it keep prior behavior. Own-horizon sales remain first; longer-horizon fallback
has one donor per forecast, weighted disjoint quotas, a 50% daily symbol ceiling
and 50% per donor ceiling. Reservations/unknowns consume capacity until terminal
evidence resolves them. The [full policy](CROSS_HORIZON_BEARISH_FALLBACK.md)
retains its September 30 decision date; it is not a claim about today's worker.

## OPRA production-history contract

The required `definition`, `cbbo-1m` and `ohlcv-1h` cursors cover each saved
universe's required completed session; `completed_through` is exclusive. Their
readiness remains relevant in stock-only source. Other retained OPRA schemas
have separate research roles. Finite Live replay must prove complete native
scope before advancing coverage. See
[OPRA history](../datafetch-ml/options-opra-history.md).

OPRA supplies historical BBO authority. Same-time auxiliary metadata does not
replace bid/ask, contract identity or quote timestamp. Older options studies
keep exact CBBO and modeled OHLCV outcomes distinct. Options intents remain
research/paper until their separately established authority permits otherwise;
future same-leg execute-or-skip checks are not current stock order authority.

## Frozen forecast grid

The independent contract has 24 rows per researched symbol: 13 forward hourly
rows plus one opening-gap research row, four four-hour rows, five daily rows
(one execution entry/four outlooks), and one weekly row. There are 19 execution
opportunities, not 24 required trades. See
[independent horizons](INDEPENDENT_STOCK_HORIZONS.md) for exact windows.

Route suffixes in older overnight path artifacts can name target endpoints;
their explicit `action_anchor_local` and saved target version govern dispatch.
Do not apply the older opening-gap/four-hour mapping to a newer independent
publication merely because route labels resemble one another.

## Immutable publication

Native generations retain forecast JSON, parquet forecasts/options intents,
model reports, fitted artifacts, cohorts and checksum-bound manifest/receipt.
Latest and dated selectors are distinct from those immutable generations.
Historical evaluation and producer exports preserve original forecast identities,
target windows, source contracts and actual publication times. A current pointer
is not permission to rewrite history or replace a session's selected source.

## Active overnight supervision

Use [nightly operations](CURRENT_SYSTEM.md) for current
responsibility ownership, dispatch, recovery and notification rules. A launcher
return, living PID or heartbeat is not completion. Inspect the saved stage
receipt and real CPU/I/O/log progress; healthy quiet fitting is not a timeout.
Preserve completed stages and original receipts. Changed source, configuration
or outputs require explicit review; recovery must not rewrite old evidence,
delete locks or replay unchanged deterministic failures.

The old LOG supervision claim/stop/recover/resume commands remain implemented
for their recorded attempts. Their historical one-minute checks and health-watch
cadences are not the current native task inventory. Onboarding keeps its
separate registered attempt and candidate scope. A missing retired-process PID
does not authorize restarting the former eight-process topology.

## Saved Gameplan evaluation

The cumulative evaluator retains receipt-verified editions and original scores,
revisiting `PENDING_MATURITY` and `MATURE_AWAITING_DATA` when eligible evidence
arrives. Its history-wide results are separate from daily Stats and broker
execution. The historical Saturday review is documented in
[weekly review](WEEKLY_REVIEW_AUTOMATION.md); Atlas's saved definition is paused
at this observation. Pausing does not make saved evaluations obsolete.

## Daytime consumers

The advisory `ml.gameplan_executor` has zero order authority. The stock consumer
and session worker have separate operator controls, selected-source/account
bindings, current broker evidence and persistent reservations. See
[stock runtime](STOCK_TRADER_RUNTIME.md) and
[stock automation](STOCK_TRADER_AUTOMATION.md). Current accepted-plan catch-up
nets due instructions against fills/reservations; the older blanket assertion
that a missed clock is always terminal does not describe that route.

## Duckets forecast UI

Gameplan and Stats read saved accepted publications. Shared/account mode must
not substitute a local-only projection for a missing or corrupt joint plan.
The reviewed readers can retain an accepted historical session while reporting
that the intended current selection is pending. Local-preparation verification
instead pins its exact local plan/Stats before combined adoption. Historical
selection and default latest selection are separate behaviors.

Missing forecast outcomes or planning references remain explicit. Readable
prices, raw/calibrated display choices and labels cannot change frozen model
status, forecasts, evaluation inputs or execution authority. Original UI
verification receipts establish their dated reader result, not today's loaded
modules or rendered desktop.

## Scheduler ownership

The active split responsibility workflow supersedes LOG scheduling. Atlas's
October 11 saved definitions and Windows readback are recorded in the relevance
review; Scout verifies its own definitions independently. A retained module or
paused task remains usable evidence without being an operating instruction.
The user retains trader controls; conditional guarded-launch authority requires
its own reviewed installed launcher and readiness evidence.

## Failure behavior

Missing/changed sources, invalid receipts, unknown submissions and unavailable
outcomes are distinct failures or limitations. Preserve the last valid
publication and exact failure evidence. A closed market is not itself stale;
covered requests with absent observations are not proof of incomplete download.
No document move or audit changes providers, training, order authority, controls,
workers or the independently established Loop C scope.

## First observed publication

The September 4 first-publication account remains historical evidence. It
recorded a nine-second publication-boundary miss, subsequent final-clock guard,
flat four-hour calibration, and corrected endpoint/action dispatch. The original
forecasts and reported statuses remain immutable; later fixes do not retroactively
relabel them. The [four-hour audit](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md)
preserves the diagnosis. Exact local receipts and trading details are retained
privately and in the recoverable original edition, not restated as current state.
