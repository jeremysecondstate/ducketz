# System functionality

## System-wide operating model

The current scheduled design is a single sequential overnight owner. The older
eight recurring modules remain reusable implementation stages, but they no
longer run continuously or coordinate a live day through independent timer
phases.

```text
17:05 PT
  Loop A close fetch + OPRA append
    -> Loop B features/directional authority
      -> cumulative saved-Gameplan evaluation
      -> four-horizon Options Strategy profit training
        -> Strategy candidates
          -> immutable next-session gameplan

04:00–17:00 PT
  verified gameplan -> clock-rotating Duckets forecast display
                    -> exact-once, stock-only hourly execution
                    -> optional advisory/paper decisions

next 17:00 PT
  completed day -> revisit every saved forecast -> successor Gameplan
```

The Scheduled owner reads progress/errors at least once a minute, repairs
verified failures, and resumes failed stages. A ten-minute health watch adopts
abandoned work using a renewable supervision claim. Healthy training can run
until 04:00 Pacific on the next exchange session. Saturday reviews saved
Gameplans from September 4 and retains longer forecasts until they mature.

The exact command, schedule, artifacts, and failure rules are defined in
`NIGHTLY_GAMEPLAN.md`.

## Ingestion and canonical storage

Loop A's overnight close cycle is the provider/data owner. It updates the six
symbols `AAPL AMZN GOOG MU NVDA SNDK` and retains the existing source boundaries:

- Databento `EQUS.MINI` is canonical operational equity OHLCV under `stocks`.
- Schwab equity history remains a secondary provider record.
- Databento `XNAS.ITCH` remains cold provider provenance. It is not blindly
  timestamp-merged into `EQUS.MINI` because audited overlapping bars were not
  exact duplicates.
- Native EQUS bars supersede same-timestamp 1-minute-derived rows; genuine
  derived gaps remain.
- OPRA raw DBN, normalized Parquet, manifest, and receipt are one partition's
  lineage, not four independent copies.

Production OPRA history is exactly `definition`, `cbbo-1m`, and `ohlcv-1h` for
all six parent symbols. Loop A performs a provider estimate/preflight, bounded
incremental fetch, atomic partition publication, and cursor advance before
model work. Its `completed_through` value is exclusive.

Other OPRA schemas remain research history. They are not described as current
and do not block production gameplan publication.

## Completed-session versus execution freshness

Two clocks are intentionally separate:

| Context | Required freshness |
|---|---|
| Overnight training/planning | Complete newest finished market session |
| Future live execution | Current tradable quote for the exact frozen legs |

When markets are closed, the newest legitimate option quote can be hours old.
That is the desired final observation for overnight fitting and planning. The
system must not reject it merely because wall-clock time continued after the
close.

At execution time, hours-old evidence is not sufficient. The only authorized
future behavior is to revalidate the exact preselected legs and execute or skip.
Contract substitution, strategy switching, and intraday re-optimization would
mutate the plan and are prohibited.

## Directional forecasts

The gameplan freezes 24 rows per symbol:

- fourteen `1h` anchors from 04:00 through 17:00 PT;
- four `4h` anchors at 04:00, 08:00, 12:00, and 16:00 PT;
- five `1d` forecasts for D+1 through D+5;
- one direct `1w` forecast spanning five eligible sessions.

This is 144 rows across six symbols. Historical intraday labels use exact
one-minute stock bars. Daily labels use the explicit daily components. The
weekly label is trained directly rather than fabricated as the mean of the five
daily probabilities.

Intraday route labels name forecast endpoints. The lightweight reader therefore
uses the next frozen endpoint rather than consuming the current-hour route after
its target window has matured: the 05:00 action wake reads `1h@06:00`, the 08:00
wake also reads `4h@12:00`, and the 16:00 wake reads `1h@17:00`. The 04:00 wake
additionally records the precomputed opening-gap checkpoints. No new forward
intraday route begins at the 17:00 close.

The v2 artifact stores this mapping explicitly as `action_anchor_local` beside
the existing `forecast_anchor_local`, and the reader verifies the expected route
set before writing a decision receipt.

Each group fits both histogram-gradient boosting and an MLP neural network. A
later chronological selection cohort chooses the baseline, neural model, or a
fixed blend. Calibration and assessment are separate, and boundary overlap is
purged. Model status remains explicit if assessment does not pass.

## Options Strategy predictions

Options Strategy prediction is not a stock-direction alias. It has its own
profitable-outcome model and exact candidate/leg identity.

The overnight Strategy-profit stage now covers `1h`, `4h`, `1d`, and `1w` and:

1. reads the current Loop B samples and causal features;
2. verifies every required production OPRA cursor;
3. constructs historical candidate outcomes from nearest causal OPRA
   `cbbo-1m` entry and exit snapshots; `1h` requires exact CBBO, while older
   `4h`/`1d`/`1w` targets may use explicitly labeled conservative hourly
   fallback evidence where the shorter CBBO archive does not reach;
4. trains histogram-gradient and MLP challengers with purged chronological
   train/selection/calibration/assessment boundaries;
5. promotes only receipt-compatible horizons that pass the declared offline
   gate;
6. generates exact candidates for the next action date; and
7. freezes one options intent beside every forecast route.

Completed-session quotes are valid planning evidence. The gameplan records
whether a fitted candidate exists and freezes its exact stock/option legs,
source Strategy generation, candidate key, model score, and quote lineage.

Equity options do not inherit extended-hours tradability merely because their
underlying stocks trade from 04:00 to 17:00 PT. The forecast grid remains intact,
but an option intent outside an executable option session stays non-executable
until a valid same-leg quote/session gate can pass.

## Publication and immutability

The only final authority is the checksum-verified pointer:

`C:\DATASTORE\ml\nightly-gameplan-latest\run.json`

The selected immutable generation contains forecasts, options intents, prior
directional evaluations, four model reports/artifacts, a human-readable
gameplan, manifest, and receipt. Publication is atomic and prohibited after
04:00 for the target action date.

The next close-cycle evaluates matured directional rows from the prior
generation in one pass. Because the source plan never changes intraday, forecast
evaluation remains reproducible. Options intents are not reported as realized
P/L without exact-leg revalidation/execution evidence; a future unexecuted
mark-to-market evaluation must be explicitly labeled counterfactual.

## Daytime behavior and order authority

The Duckets `Rolling Forecasts` tab reads the immutable gameplan directly. It
shows the active frozen `1h` and `4h` target windows, D+1 in the ordinary daily
card, and all five daily components plus the direct weekly prediction in the
remaining-week card. It refreshes on the next wall-clock hour plus five seconds,
so hourly and four-hour changes require no data fetch or model rerun. Each
displayed route includes the matching frozen options intent, Strategy name and
profit probability when available, pricing lineage, and explicit `NO_TRADE` or
same-leg-revalidation reason.

`ml.gameplan_executor` remains the broker-free paper reader.
`ml.gameplan_stock_trader` is the active stock-only adapter. It validates the
same pointer/action date and hands the current forward 1-hour signal plus
promoted longer-horizon context to the established Schwab risk engine. It
requires both stock activation controls and `--execute`, revalidates current
broker state and quotes, and enforces exact-once submission. At a 1h/4h
overlap, opposite actionable directions veto a new entry; agreement never
creates more than one shared-risk order per symbol.

Options activation remains outside this deployment. The stock adapter cannot
construct or submit an option order.

## Scheduling and failure behavior

- `Loops Overnight Gameplan` runs at 17:05 PT on weekdays.
- The old hourly monitor/adaptive trainer is retired.
- The standalone OPRA maintainer and overlapping paper/adaptation schedules are
  paused.
- All legacy daytime trader schedules remain paused. The gameplan hourly stock
  owner and its 13:05 transition wake are active.
- The Saturday read-only operator review remains independent.

Each nightly stage runs once, sequentially. Failure preserves the prior final
pointer and records the failed stage. No automatic retry, process-stack restart,
historical deletion, broker mutation, or guessed pointer repair is authorized.

## Observed September 4 publication

The first new-architecture gameplan froze at 03:59:44 PT and published its
atomic pointer at 04:00:09 PT. It contains the required 144 directional rows and
144 options-intent rows for all six symbols. Directional `1h`, `4h`, and `1d`
groups passed their assessment gates; the weekly direction group remained
research-only. Strategy-profit `1h`, `1d`, and `1w` passed independently; the
65-window `4h` profit model failed its assessment and remained research-only.

The underlying Strategy authority contains 3,840 fitted candidates, all using
valid OPRA BBO legs timestamped `2026-09-03T20:00:00Z`. The final plan contains
no entry instruction: every options intent records an explicit `NO_TRADE`
reason, and the receipt records zero orders. The nine-second first-run timing
miss is not normalized away; the inference loader was narrowed to the last
eligible completed-session snapshots and a final pre-pointer 04:00 check was
added for subsequent runs.
