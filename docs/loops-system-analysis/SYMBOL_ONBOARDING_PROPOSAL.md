# COST onboarding proposal

This is the historical planning record. The user subsequently authorized
implementation on September 6, 2026. The reusable workflow and current evidence
are documented in [SYMBOL_ONBOARDING.md](SYMBOL_ONBOARDING.md); the estimates and
repository findings below describe the earlier review, not the current state.

Original review status, September 6, 2026: no COST market data had been downloaded,
no training had been run, and no production code, universe, pointers, controls,
or scheduled tasks had been changed for that review.

## Recommendation

Create a small, resumable onboarding command around the existing fetchers and
overnight stages. Use COST as its first real caller. Match the existing symbols'
data families and retained historical coverage, then admit COST to a future
seven-symbol Gameplan only after validation. Keep one current production
scheduler and one owner per data family.

The expected added fetched history is about **3.2 GiB**, with a practical
**2.8–3.8 GiB** planning range. Features and model outputs are additional.
The narrower current default bootstrap is about **1.8 GiB** of Databento history,
but has shorter coverage than the accumulated histories of the six existing symbols.

## What exists and what is missing

- `datafetching.main` already accepts one arbitrary equity symbol. It fetches
  operational Databento bars, FMP data, SEC filings and Schwab market evidence.
  The `full` and `incremental` profile names are aliases of continuation.
- `datafetching.options_history` already supports `--symbols COST`, metadata
  preflight, budgets, resumable publication, and bootstrap cursors.
- `datafetching.databento_cold_start` and the underlying archive writers provide
  historical archive acquisition. Shared CME must not be re-bootstrapped merely
  because a new equity is added.
- There is no unified onboarding lifecycle that carries a candidate from
  discovery through verified data, model generation and production admission.
- `datafetching/watchlist.txt` and `ml/universe.py` are separate authorities.
  The latter enforces an exact six-symbol production universe.
- `ml/nightly_gameplan.py:427` rejects a symbol count other than six.
- Stock-trader training also contains fixed six-symbol lists. The live trader's
  symbol contract derives directly from the shared Loops universe.
- The active saved **Loops Overnight Gameplan** task explicitly requires 144
  forecasts and 144 options intents. That instruction and its operating document
  must become `24 × configured symbol count`.
- Rolling Forecasts uses six columns at wide widths. Seven-symbol display and
  older Gameplan reading need explicit verification.

## Proposed lifecycle

1. **Plan.** Register COST as pending with equity symbol COST, OPRA parent
   COST.OPT, verified venue/calendar metadata and an effective activation session.
   Produce a reviewable manifest of providers, schemas, exact date windows,
   expected records, quoted provider fees, estimated stored bytes and disk reserve.
   Preserve separate operational EQUS.MINI and XNAS.ITCH archive identities.
2. **Fetch.** Acquire only COST-specific missing data through existing writers.
   Reuse shared macro/CME data. Acquire the selected OPRA history and first current
   option snapshot. Retain native DBN, normalized Parquet and publication evidence.
   Never manufacture earlier prospective Schwab snapshots.
3. **Validate.** Check symbol mapping, nonzero expected rows, natural-key uniqueness,
   timestamps/session completeness, checksums/receipts, and all production
   symbol/schema cursors. Verify bars, corporate actions, fundamentals/filings,
   options identity and model-facing coverage. Record explicit missing-data states.
   A partially bootstrapped symbol remains pending.
4. **Build and assess.** Run the existing dependency sequence for the candidate
   seven-symbol generation: Loop A calculations → Directional Loop B → saved
   Gameplan evaluation → Strategy profit training → Strategy candidates →
   overnight Gameplan. Verify 1h, 4h, daily/D+1–D+5 and weekly routes. Preserve
   existing promotion rules; a populated model or forecast does not imply
   assessment success, and options may correctly remain NO_TRADE.
5. **Activate.** Commit the ready universe and its compatible publication together
   at a permitted next-session boundary. Each run records its universe snapshot,
   so changing membership cannot invalidate a resumed six-symbol run or older
   immutable plans. COST adds 24 forecasts and 24 intents, making 168 of each
   and 336 combined rows. Verify UI visibility, evaluation and next daily fetch.
6. **Measure and report.** Persist a receipt for every stage and resume from the
   failed stage. Record storage before fetch, after fetch, and after feature/model
   publication. Attribute writes to COST paths and its generation outputs so
   unrelated background growth is not charged to COST.

Use one authoritative symbol registry with a pending/active state and make the
watchlist and model consumers derive their scopes from it. This should be a thin
wrapper over current writers, not another fetch implementation or recurring loop.
A real read-only planning mode should avoid datastore publication side effects;
the existing history preflight CLI publishes metadata receipts.

## Daily operation and trading boundary

The active model is the 17:05 Pacific sequential overnight task plus its health
watch, daytime frozen-plan consumers and Saturday evaluation. Older recurring
supervisors remain retired. Update the existing task's count checks and operating
contract; do not create a separate recurring COST task.

Forecast membership currently also expands the stock trader's allowed universe.
The implementation should distinguish forecast admission from live-trading
eligibility, so the requested forecasting onboarding has an explicit execution
scope. Preserve existing trading controls and options NO_TRADE behavior.

## Storage estimate

Measured DATASTORE baseline: **178.5449 GiB** (191,711,088,282 logical bytes across
154,291 files). Free space on its volume: **1,327.42 GiB**. One GiB is 2^30 bytes.

Read-only provider metadata was requested for COST and COST.OPT. Estimates were
calibrated against 17,811 existing partition manifests across all six symbols,
using stored native DBN + normalized Parquet + manifests/receipts per row.

| Fetched historical family | Current default bootstrap | Coverage matching retained peers |
|---|---:|---:|
| Production OPRA: definition, ohlcv-1h, cbbo-1m | 0.87 GiB | 1.56 GiB |
| Other retained OPRA research schemas | 0.84 GiB | 1.53 GiB |
| Separate XNAS.ITCH equity archive | 0.06 GiB | 0.06 GiB |
| Databento history subtotal | **1.78 GiB** | **3.15 GiB** |

Operational EQUS.MINI bars, corporate/filing data and the initial live snapshot
are a smaller additional allowance, not individually measured COST downloads.
Allow approximately **2.8–3.8 GiB added after the peer-coverage fetch**, bringing
the measured baseline to roughly **181.3–182.3 GiB**, excluding unrelated growth.
This is an estimate, not an exact future disk delta or a fixed upper bound.

Technical features, outcome caches, models and new immutable run artifacts are
outside that fetch subtotal. Recent six-symbol examples were about 26 MiB for
a Loop B publication, 42 MiB for a Strategy training report/outcome generation,
22 MiB for Strategy publication and 1.6 MiB for the Gameplan itself; separate
models and reusable caches add more. Do not divide all historical ML storage by
six to estimate one onboarding run.

For the peer-coverage historical scopes, provider billable size totals about
6.44 GiB. The existing 2× size + 5 GiB capacity formula implies about 17.9 GiB
free-space headroom; round to **20 GiB available for the operation** rather than
treating that as expected permanent growth. Initial scope byte budgets must be
planned explicitly: the largest peer-history scopes exceed the history CLI's
default 2,000,000,000-byte per-run allowance.

All current-default metadata quotes were USD 0. The peer-history quote was about
USD 0.0124 in total, from the older XNAS imbalance window; the OPRA scopes were
USD 0. These are provider quotes for the observed windows, not charges incurred,
and should be refreshed before execution.

Databento's `get_billable_size` measures **uncompressed binary billing bytes**.
It does not measure compressed downloads or final DATASTORE occupancy, despite
the local variable/receipt label `estimated_download_size_bytes`.
See [Databento Historical API](https://databento.com/docs/api-reference-historical).
Actual stored size must be measured after acquisition.

## Coverage differences and current readiness issue

The normal bootstrap uses 20 calendar days of minute CBBO, one day of second
CBBO, 100 days of definitions, 1,825 days of hourly options bars and 2,555 days
of daily options bars, plus other schema-specific windows. Existing production
minute CBBO begins July 27, definitions begin May 7, and hourly options bars
begin August 16, 2021 for the long-history symbols. The peer estimate uses
those starts. Research scopes match their retained historical windows rather
than silently extending all research history to the present.

As observed during this review, Historical OPRA metadata ends exclusively at
**September 4, 2026**, meaning through September 3. Existing production cursors
cover September 4 through verified Live replay. COST has no such replay receipt.
The estimate excludes that not-yet-available Historical session. Activation must
wait for valid Historical catch-up or an actually available exact-session replay;
a current quote is not a substitute.

## Implementation verification

The eventual change should prove:

- seven-symbol scopes agree across fetch, pricing, Strategy, Gameplan, UI and
  monitoring, with 168 forecasts and 168 intents;
- old six-symbol plans and in-progress run manifests remain readable;
- interrupted onboarding resumes without duplicate partitions or pointer damage;
- incomplete COST data cannot become active or break the current six-symbol plan;
- provider budgets fail closed and actual bytes are recorded at each milestone;
- explicit trading eligibility and existing assessment gates remain enforced.

Detailed metadata estimates, calibration and the inventory observation are saved in
[the planning evidence](../../artifacts/analysis/cost-onboarding-2026-09-06.json).

## Storage reconciliation after review

Rechecked September 6, 2026 in response to the question about dividing the
datastore's size by six. The metadata record counts below reproduced the prior
COST inputs. No market data was downloaded.

| Current DATASTORE allocation | GiB |
|---|---:|
| OPRA archive, staging and metadata | 69.46 |
| Shared CME archive and runtime/materialized data | 55.59 |
| US-equity archive | 3.85 |
| Stock bars, fundamentals, snapshots and features | 2.62 |
| ML artifacts | 29.33 |
| Retired predictions | 14.80 |
| Other files | 2.89 |
| Total | **178.54** |

Shared CME is reused when COST is added. Existing ML and retired-prediction
storage represents accumulated outputs, not an initial raw-data allowance for
every newly added stock.

| Existing symbol | OPRA GiB | Equity archive + stocks GiB | Total GiB |
|---|---:|---:|---:|
| AAPL | 6.42 | 1.00 | **7.42** |
| AMZN | 5.56 | 0.82 | **6.38** |
| GOOG | 3.92 | 0.69 | **4.61** |
| MU | 27.14 | 1.33 | **28.47** |
| NVDA | 9.71 | 1.60 | **11.31** |
| SNDK | 15.71 | 1.02 | **16.73** |

The actual average is **12.49 GiB**, but its spread makes it a poor standalone
estimate for a new symbol. The table measures retained files and does not assert
identical completeness for every peer.

Identical provider queries for OPRA `cbbo-1s`, August 14–18 inclusive, return:

- COST: **32,338,103** records.
- AAPL: **93,147,641** records.
- MU: **498,205,492** records.

Thus equal requested dates and granularity do not imply equal record counts.
Existing stored sizes for this schema are approximately 32.51–42.02 bytes per
row across the six symbols, including raw native files and Parquet plus metadata.
The weighted 39.272-byte ratio applied to COST gives about **1.18 GiB** for this
second-quote history. All selected COST historical families total approximately
**3.15 GiB** under the same method.

This supports the earlier **fetched-history** estimate. It does not establish
a 3.2 GiB all-in integration cost or validate a hard upper bound. The proposed
2.8–3.8 GiB range is a planning allowance, not a statistical confidence interval.
Report actual fetched-data growth and generated-artifact growth separately when
onboarding is eventually implemented.

Detailed reconciliation:
[storage reconciliation evidence](../../artifacts/analysis/cost-storage-reconciliation-2026-09-06.json).
