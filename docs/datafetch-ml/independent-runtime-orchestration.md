# Independent market-data and model runtimes

The recommended topology is six independent processes. CME/L2, shadow option
pricing, Schwab options, and options-strategy work are deliberately outside the
critical paths of Loop A and directional Loop B. These commands document the
topology; they do not claim that the Pricing supervisor has been deployed.

## Artifact ownership

| Process | Sole authoritative writes |
| --- | --- |
| `datafetching.cme_runtime` | `pools/cme/events/databento/**`, CME successful-query cursors, immutable five-minute L2 snapshots, and the existing hourly CME cross-asset feature artifact |
| `datafetching.options_runtime` | Immutable Schwab raw-chain, normalized-contract, and option-quality snapshot directories and their pointer; integrated receipts record their Pricing barrier proof, and the process also maintains the legacy monthly option mirrors |
| `datafetching.orchestrate` (Loop A) | Equity bars and quotes, non-CME shared macro data, fundamentals, technicals, signals, and immutable all-symbol `loop-a/bar-readiness/**` receipts |
| `ml.option_pricing_runtime` | Immutable shadow target outcomes, pricing samples, predictions, evaluations, compact surfaces, monitoring, reports, copied models, `ml/option-pricing-target-latest/run.json`, and `ml/option-pricing-latest/run.json` |
| `ml.option_pricing_fred` (bounded import) | Immutable FRED/ALFRED provider responses, vintage manifest/receipt, append-only macro-vintage rows, and derived point-in-time FEDFUNDS release features; never current-revised historical claims |
| `ml.prediction_runtime` (Loop B) | Immutable directional sample, prediction, evaluation, monitoring, and intelligence runs plus `ml/latest/run.json` |
| `ml.strategy_runtime` | Immutable strategy candidates, audits, reports, and copied model artifacts plus `ml/strategy-latest/run.json` |

External CME and Options modes are the default. Do not run an inline writer and
its independent runtime against the same datastore. A shared exclusive writer
lock rejects that configuration before an inline request can write an owned
artifact. Dead-process lock files are reclaimed on restart; a live owner is
never displaced.

## Recommended startup

Before the continuous processes, refresh the bounded operational proof and
ensure a causal current-rate receipt exists. The second command uses the latest
already-fetched FEDFUNDS value only for later targets; every subsequent Loop A
FRED fetch refreshes it automatically.

```powershell
python -m ml.option_pricing_admin --datastore-target pc operational-preflight
python -m ml.option_pricing_admin --datastore-target pc capture-current-rate
python -m ml.option_pricing_admin --datastore-target pc readiness
```

`readiness` is read-only and exits 6 while evidence gates remain blocked.

Open one PowerShell terminal for each continuous command:

```powershell
# 1. Complete-history CME collection. One request at a time is the conservative default.
python -m datafetching.cme_runtime --datastore-target pc --max-concurrency 1

# 2. Loop A. CME and option-chain work stay external by default.
python -m datafetching.orchestrate --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --cme-mode external --options-mode external `
  --providers databento fmp fred schwab sec --interval-minutes 15

# 3. Shadow Pricing. Closed sessions are write-free monitor-only cycles.
python -m ml.option_pricing_runtime --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --interval-minutes 15 --phase-offset-minutes 1 `
  --bar-readiness-mode required `
  --bar-readiness-timeout-seconds 120

# 4. Schwab options. It requires Loop A's exact all-symbol readiness receipt.
python -m datafetching.options_runtime --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --interval-minutes 15 --phase-offset-minutes 2 `
  --pricing-barrier-timeout-seconds 150 `
  --bar-readiness-mode required

# 5. Directional Loop B. Start after Loop A has published one COMPLETE generation.
python -m ml.prediction_runtime --datastore-target pc --provider databento `
  --watchlist datafetching\watchlist.txt `
  --horizons 1h 4h 1d 1w --feature-profile loop-a-all-v1 `
  --model-family logistic --calibration platt --round-trip-cost 0.001 `
  --interval-minutes 15 --phase-offset-minutes 5

# 6. Strategy processing. Shadow diagnostics never change ranks or orders.
python -m ml.strategy_runtime --datastore-target pc `
  --interval-minutes 60 --phase-offset-minutes 10 `
  --pricing-mode shadow
```

Use `--datastore <temporary-path>` during development and migration tests. None
of these commands requires deleting, resetting, or bootstrapping an existing
datastore. Add `--once` to any command for one bounded cycle. Press `Ctrl+C` to
stop a continuous process; the current atomic artifact remains authoritative.

The CME command explicitly loads `DATABENTO_API_KEY` from the repository-root
`.env`. An environment variable already set in the launching shell takes
precedence; the `.env` loader never overwrites it.

## Cadences and phase offsets

The CME defaults are `ohlcv-1m=60s`, `bbo-1m=15s`, and `mbp-10=5s`, with UTC
phase offsets of 1s, 2s, and 0s respectively. Override individual values with
repeated options such as:

```powershell
python -m datafetching.cme_runtime --datastore-target pc `
  --cadence mbp-10=5 --phase-offset mbp-10=0 `
  --overlap-seconds mbp-10=2 --chunk-minutes mbp-10=5
```

The provider's default MBP latest window is only 30 seconds. It is used only to
establish an initial endpoint. Thereafter the runtime retains complete event
history from its `queried_through` cursor, with a safety overlap, while also
publishing latest-state snapshots. It is not treated as a historical
continuation window.

Complete-history MBP streaming requests default to a 250,000-record safety cap
(about 92 MB of uncompressed MBP-10 records). A response that reaches the cap
is not treated as complete and is not allowed to advance the cursor. The
runtime first splits its symbol set, then its exact time range, and retries
until every child request is unsaturated. Override the cap only when needed:

```powershell
python -m datafetching.cme_runtime --datastore-target pc `
  --record-limit mbp-10=250000
```

This preserves complete history while preventing Databento's provisional
recent-data size estimate from turning a small recovery slice into a nominal
greater-than-5-GB streaming request.

Pricing defaults to every 15 minutes at UTC phase +1 minute and Options defaults
to phase +2 minutes. Loop A defaults to 15 minutes; Loop B defaults to phase +5
minutes, and Strategy defaults to hourly at phase +10 minutes. A shared XNYS
calendar decision owns all three Loop A/Pricing/Options targets. Eligible targets
are exact completed quarter-hours strictly after the regular open through the
official regular close, inclusive. The first normal-session target is therefore
09:45 America/New_York. Holidays, weekends, DST, and early closes come from
`exchange_calendars`; unsupported extended hours never become prospective
targets.

For an actionable target, Pricing waits monotonically up to 45 seconds for Loop
A's exact all-symbol readiness receipt. Receipt arrival ends the wait immediately
and the real readiness/observation clocks become the prediction clock. A deadline
miss publishes immutable `TARGET_BAR_NOT_READY` exactly once; later readiness can
never replace it or create a retroactive prediction. Options separately waits up
to 45 seconds for the verified Pricing target authority. A Pricing miss remains a
verified, noncreditable terminal outcome. A missing Pricing barrier is recorded as
`TIMED_OUT` (or `MISSING` with zero wait), but Options still requires Loop A
readiness and never infers bar readiness from Pricing.

When no eligible target exists, Pricing and Options report
`cycle_mode=MONITOR_ONLY` and `target_state=MARKET_CLOSED_IDLE`, explain the
calendar reason, and print the next eligible phase time. Pricing does not append a
target outcome or full research generation. Options does not authenticate to
Schwab, request a chain, or write decision-clock errors. Thus repeated closed
cycles do not grow the target chain or treat a closure as a lost evidence
opportunity. `TARGET_ALREADY_OBSERVED` remains distinct: it means a verified
Options receipt already owns an otherwise actionable target, such as after a
runtime restart.

## Three clocks and causal selection

CME data keep three separate concepts:

- the exact provider event timestamp, stored at nanosecond precision and never
  rounded or overwritten;
- `fetched_at`, the local receipt/availability clock (there is no redundant
  `received_at` alias);
- `snapshot_for`, the clean derived boundary. L2 boundaries are aligned to
  `:00`, `:05`, `:10`, and so on.

An event enters a five-minute snapshot only when both its provider event time
and `fetched_at` are no later than `snapshot_for`. A late-arriving old event
therefore cannot be inserted retroactively into an already immutable bucket.
Snapshots record event age, receipt age, and quality status.

An Options receipt also separates `snapshot_for` from `available_at`.
`snapshot_for` is the cycle's exact expected completed Databento 1m boundary;
`available_at` is when the local Schwab response completed. Realized-volatility
context is capped at the last successfully committed Loop A generation. An
active or failed Loop A cycle does not hide or replace that prior committed
cutoff, and the Options process never waits for the active cycle.

New receipts additionally separate `request_started_at` from
`receipt_published_at`. The former is the causal quote cutoff; the latter is
sampled in the atomic Options publication path. `pricing_barrier.observed_at`
records when Options checksum-verified the exact Pricing target receipt.
Prospective credit requires both Pricing publication and barrier observation no
later than `request_started_at`; an embedded prediction timestamp cannot
establish that ordering by itself.

Directional Loop B uses its completed Loop A generation time as the causal
input cutoff. It reads checksum-verified option receipts with
`available_at <= cutoff`; an in-progress generation is invisible. Existing
option freshness semantics are unchanged:

| Directional horizon | Maximum option age |
| --- | ---: |
| `1h`, `4h` | 2 hours |
| `1d` | 1 day |
| `1w` family | 3 days |

Strategy may run later. Its manifest binds the exact authoritative Loop B
record, every selected option receipt, and the stock BBO source files. It never
adds files to or changes the source Loop B directory.

## Atomic publication

Each option generation is first written into a private staging directory. Raw,
normalized, and surface Parquets are checksummed; `manifest.json` and
`receipt.json` are written only after all three succeed. The directory is then
renamed into its immutable name and the per-symbol pointer is atomically
replaced. Receipt-aware readers ignore staging directories and directories
without a receipt. The legacy monthly mirrors are compatibility outputs, not a
commit signal.

Loop B follows the same immutable-run plus receipt plus atomic-pointer pattern
at `ml/latest/run.json`. Pricing has its own receipt-chained authority at
`ml/option-pricing-latest/run.json`, a fast target authority at
`ml/option-pricing-target-latest/run.json`, and Strategy has a separate authority at
`ml/strategy-latest/run.json`. Pricing publication never advances either other
pointer. Directional publication completes before Strategy begins and remains
valid if Strategy is slow or fails.

## Target-scoped open-market state machine

Every scheduled Pricing and Options cycle freezes one quarter-hour identity `T`
before processing any symbol:

1. Loop A finishes the Databento watchlist lane first. Its provider-completion
   callback verifies the exact completed Databento 1m bar ending at `T` for every
   symbol, freezes each selected close and row checksum, and atomically publishes
   `loop-a/bar-readiness/<T>/readiness.json` plus `receipt.json`. This occurs
   before unrelated FMP/FRED/Schwab/SEC work and before fundamentals, technicals,
   and signals, so it does not inherit Loop A's observed long calculation tail. A
   partially updated watchlist never gets a readiness receipt. Loop A continues
   the remaining lanes and separately publishes its full `COMPLETE` generation.
2. Pricing consumes that immutable all-symbol receipt in integrated mode and
   publishes one immutable target outcome containing causal samples, predictions,
   and per-symbol terminal statuses. Valid outcomes include predictions,
   `TARGET_ALREADY_OBSERVED`, `NO_ELIGIBLE_CONTRACTS`,
   `TARGET_BAR_NOT_READY`, `PRICING_FAILED`, and mixed terminal results. The
   target pointer is receipt-chained and checksum verified.
3. Options waits up to its configured deadline for the same `T`. A verified
   prediction, skip, or terminal failure satisfies the publication barrier, but
   only prediction-bearing and mixed outcomes can set
   `prospective_credit_allowed=true`. A missing, invalid, or timed-out phase does
   not block Schwab collection; the Options receipt records the miss.
4. Pricing continues its full immutable generation, lineage, eligibility, and
   health work after the target authority. This shadow research tail does not
   hold Options, Loop B, or Strategy open.

The Pricing writer lock prevents overlapping cycles. If a complete tail ever
runs across one or more later actionable boundaries, the runtime publishes an
empty, immutable `PRICING_TIMED_OUT` target outcome for each missed eligible
target before scheduling the next live cycle. Closed-session wall-clock
boundaries are filtered by the same calendar decision and create no target
artifacts. Late skip receipts are audit evidence only and can never create
prospective credit or be replaced by a backfill.

`TARGET_ALREADY_OBSERVED` remains the no-backfill guard: a target Options receipt
was visible before prediction creation. `NO_ELIGIBLE_CONTRACTS` means a strictly
earlier source receipt existed but every contract failed the unchanged causal
feature contract. Newer all-stale source receipts are checked and retained in
lineage but cannot mask an older causal receipt that passes that same contract.
Neither state is a crash. Source option snapshots and quotes remain strictly
earlier than `T`, and all consulted source/target receipts remain checksum
verified.

The narrow readiness boundary is an additional Loop A-owned artifact, not a
replacement for Loop A completion. Loop B still locks and consumes the last
COMPLETE Loop A generation. Strategy still consumes its existing Loop B cutoff
and receipts. CME remains independently owned and is not consulted by the new
barrier.

## Publication clocks and health interpretation

The runtime preserves distinct clocks for Loop A cycle start,
`bar-readiness.ready_at`, Pricing computation start, target authority
publication, full-generation authority publication, eligibility generation,
eligibility publication, health checking, Options request start, Schwab response
availability, and Options receipt publication.

The target outcome receipt supplies prediction availability. The full generation
retains and verifies that first authority rather than inventing an earlier time.
Its report records `immutable_files_completed_at`; the later `publication.json`
`published_at` is sampled inside the receipt/pointer publication path after
staged verification. Eligibility `generated_at`, eligibility receipt
`published_at`, and health `checked_at` are each sampled at their own event.
For pre-barrier legacy generations, reconciliation uses the later immutable
receipt-file availability as a conservative migration proof. New evidence also
requires the exact target-outcome run path and receipt checksum recorded in the
Options receipt.

Console timing includes `cycle_mode`, target state, exact target or `NONE`, reason,
next eligible cycle, readiness time, Pricing terminal outcome and authority time,
Options barrier verification versus terminal outcome, whether Schwab was called,
request start, response, and receipt availability. Pricing prints current-target
rows and new prospective prediction/evaluation deltas separately from cumulative
carried research inventory. Identical symbol outcomes are grouped unless
`--per-symbol-detail` is selected.

Pricing health records stage timings for preflight, target authority, research
preparation, generation publication/lineage, and the post-publication
eligibility/health tail. Evidence-stagnation time counts only eligible regular
option-market windows, not nights, weekends, holidays, or post-close idle.
Expected closure creates neither `MISSED_PHASE` nor stagnation alerts, while
lineage, model, OPRA, partition, disk, latency, and other genuine problems remain
actionable. `NOT_PRODUCTION_ELIGIBLE` and `automated_action_allowed=false` are
unchanged. `maximum_cycle_seconds=600` applies to the complete Pricing cycle; the
bar-readiness and Options barrier waits are separate bounded liveness budgets.

## Restart and delay behavior

- CME advances its cursor to every successful query endpoint, including a quiet
  range with zero events. Restart begins at the previous successful endpoint
  minus the configured overlap. Large gaps are chunked, persisted partitions
  are deduplicated, and a failure leaves the cursor before the failed range.
- CME OHLCV history is partitioned daily; BBO and MBP history is partitioned
  hourly, so a small continuation never rewrites an unbounded Parquet.
- An orphan CME or Options staging directory is not authoritative. The last
  valid pointer remains readable. A committed CME L2 directory whose pointer
  update was interrupted republishes that pointer on restart.
- An orphan Loop A readiness or Pricing target staging directory is invisible.
  A target outcome receipt without pointer reachability cannot satisfy Options.
  Restart preserves the last verified pointer; a retry publishes a new immutable
  directory while the orphan remains non-authoritative evidence.
- Once a target outcome owns `T`, a repeated Pricing invocation returns that
  authority instead of replacing or backfilling it. The Pricing runtime lock
  prevents overlapping cycle owners.
- Options retries or skips independently. Loop A continues with fundamentals,
  technicals, and signals, and Loop B reuses the newest causally eligible
  committed option receipt subject to the unchanged freshness limits.
- Strategy retries the current Loop B source until it publishes a matching
  Strategy receipt. A slow Strategy run cannot hold directional predictions
  open.
- If CME is delayed, Loop A and Loop B continue. CME-derived features are used
  only when their existing hourly point-in-time freshness contract permits.
  That contract and calculation version were not changed.

## Compatibility modes

Inline modes exist only for controlled compatibility operation:

```powershell
python -m datafetching.orchestrate --datastore-target pc `
  --cme-mode inline --options-mode inline --once
```

Stop the independent CME and Options processes before using that command. The
writer locks deliberately reject two owners. `--skip-cme` remains available,
but is unnecessary in the recommended external mode.

Pricing `--once` uses the safe all-symbol readiness receipt by default. For a
controlled standalone compatibility run without a Loop A coordinator, pass
`--bar-readiness-mode exact`; it still binds every symbol to one explicit `T`
and fails closed instead of reusing an older quarter-hour. Options `--once`
remains bounded and can use `--pricing-barrier-timeout-seconds 0` for immediate
standalone fallback evidence.

## Console timing output

Structured timings use compact one-line `START` and `END` records by default.
They omit null fields but retain the UTC correlation time, stage, symbol,
provider/schema, request range, attempt, row count, operation, status, and
elapsed milliseconds. For JSON Lines ingestion, set the format before starting
a process:

```powershell
$env:DUCKETS_TIMING_FORMAT = "json"
python -m datafetching.cme_runtime --datastore-target pc
```

## Concurrency and performance check

CME accepts only `--max-concurrency 1` or `2` and defaults to 1. The repository
fixture benchmark verifies that two workers remain bounded and shortens four
independent delayed requests. A real-provider comparison should retain the
default of one unless operational 504 rates show that two is safe.

Run all local hot-path benchmarks without touching the operational datastore:

```powershell
python benchmarks\benchmark_orchestration_hotpaths.py
```

The benchmark compares legacy ID-generating analytical reads, dictionary/JSON
continuation upserts, and repeated full-frame strategy receipt scans with their
new analytical, vectorized, and indexed paths.
