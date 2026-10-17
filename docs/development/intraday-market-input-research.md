# Receipt-timed FMP research supplement

`ml/intraday/market_input_research.py` adds pure request and extraction helpers
for 13 distinct research endpoints (14 variants), nine technical indicators and
four macro series. The catalog has 27 sources and 43 numeric fields. It uses
the existing `FmpCorporateDataProvider._get_json` authentication path through
the owning collector. Importing the module performs no network calls.

The common endpoint dispatch was reviewed against Scout PR #205 commit
`28ef18efe477c43375a363dc436f5780ea1a1494`, specifically
`market_collection_extended.py` and `market_collection_fmp_context.py`. This
supplement imports no peer symbols, fitted models, training populations or
operating configuration. The indicator request period is a documented provider
context definition, not a change to a trader's selected model parameters.

## Requests and features

| Source suffix (`fmp_`) | Endpoint | Reviewed numeric features |
|---|---|---|
| `estimates_annual`, `estimates_quarter` | `analyst-estimates` | Nearest future fiscal period: EPS, revenue and EPS analyst count |
| `rating_snapshot`, `rating_history` | `ratings-snapshot`, `ratings-historical` | Overall provider score, bounded 0–5 |
| `target_summary` | `price-target-summary` | Last-month average target and count |
| `target_consensus` | `price-target-consensus` | Consensus, high and low target |
| `grade_changes` | `grades` | Eligible received events, upgrades and downgrades over seven days |
| `grade_history`, `grade_consensus` | `grades-historical`, `grades-consensus` | Analyst count and bullish fraction; all five count operands required |
| `dcf`, `dcf_levered` | `discounted-cash-flow`, `levered-discounted-cash-flow` | Provider valuation and valuation/reference-price minus one |
| `research_news`, `press_releases` | `news/stock`, `news/press-releases` | Eligible received seven-day event count and newest event age |
| `sec_filings` | `sec-filings-search/symbol` | Eligible received 30-day filing count and newest event age |
| `technical_sma/ema/wma/dema/tema/rsi/adx/williams/standarddeviation` | Corresponding `technical-indicators/*` | Latest eligible completed 15-minute provider indicator |
| `macro_gdp/cpi/unemployment/federal_funds` | `economic-indicators` | Latest received GDP, CPI, unemployment rate or federal funds value |

`research_specs(symbol, now, include_global=False)` returns `(source, endpoint,
params)` jobs. Symbol jobs use one exact equity, a single page of at most 100
rows where the endpoint supports pagination/limits, 7-day news and 30-day filing
windows. Endpoints without a supported request limit retain their response;
extraction rejects responses exceeding 5,000 rows. No complete-pagination claim
is made. Reaching a page limit is recorded explicitly.

Technical requests use `timeframe=15min`, `periodLength=10` and a 14-day date
lookback. Macro requests use one inclusive 90-day slice, within the documented
API bound. Collect global jobs once per cycle and share their exact response
receipt with each equity consumer; do not request one macro copy per equity.
If that GDP response is explicitly empty, `empty_macro_fallback_spec(source,
params, payload)` supplies one preceding, nonoverlapping 90-day request. It
rejects marked/error responses, malformed windows and all other series. The
collector allows at most one fallback, links the first receipt, preserves both
raw responses, and uses the second response's actual receipt time. An empty
fallback remains missing; it must not recursively expand the history. If the
launch budget expires, the collector retains the pending fallback for its next
ordinary pass instead of repeating the first empty request.

The helper exposes `SOURCE_FIELDS`, `SOURCE_TTL_SECONDS` and
`SOURCE_REFRESH_SECONDS`. Requested refresh is 15 minutes for technicals,
news, press releases, grade changes and SEC filings; six hours for the remaining
research and macro sources. Receipt TTL is respectively 30 minutes, two hours
for event sources, and 48 hours for the rest. These are collector policies,
not claims about provider update frequency. The owning collector must rotate
unattempted jobs and record deferrals within its launch budget. A 120-second
budget does not guarantee every source refreshes on every cycle.

## Timing, validity and retention

Persist each full raw response immutably with actual request and receipt clocks
before calling `extract_context(source, payload, received_at, symbol=...,
request_params=...)`. It returns `features`, `status`, `event_at` and compact
`evidence`. `extract_features` is a convenience wrapper. Raw provider fields
outside this reviewed feature catalog remain stored; they are not automatically
numeric model inputs. A derivation failure must retain the raw response.

Availability always begins at actual receipt. Fiscal and estimate period dates
and macro period dates never establish publication or historical availability.
Current macro responses may include later revisions; they cannot reconstruct
historical vintages. An explicit future publication is rejected. Publication
fields without a usable clock are rejected when supplied. Naive provider
publication clocks use America/New_York for this US-equity scope; that
conversion is an implementation assumption, not proof of a historical release.

Technical bar openings use the New York exchange clock, with daylight-saving
handling, and must align to 15 minutes. The bar must finish by receipt and its
end must be within 30 minutes of receipt. Ambiguous/nonexistent local clocks,
future bars and conflicting latest-bar records are excluded. Provider warmup,
adjustment and session definitions remain independent of native price training
populations. No indicator is used as a substitute price observation.

Every extractor rejects explicit synthetic, no-trade and source-empty markers
before numeric projection. A supplied marker must be boolean `false`; strings,
numbers and null do not prove an observation. Nonfinite values, invalid domains
and incomplete ratio operands remain missing. Duplicate exact rows are counted
once. Conflicting latest snapshots produce unavailable features. Counts refer
only to eligible records in the received page; an empty page does not create
zero-activity features. Error evidence contains exception type, HTTP code and
category, never exception text, authenticated URLs or API keys.

Offline regression checks:

```text
python -B -m pytest tests/test_intraday_market_input_research.py -q -p no:cacheprovider
```

The suite exercises every adapter, marker rejection, receipt/publication
separation, finite feature domains, future/unfinished/stale bars, DST, response
bounds and sanitized error handling. Passing these checks establishes parser
behavior, not provider entitlement, successful collection or loaded runtime.

Primary references: [FMP Stable endpoint catalog](https://site.financialmodelingprep.com/developer/docs),
[economic indicator date bounds](https://site.financialmodelingprep.com/developer/docs/stable/economics-indicators),
[standard-deviation endpoint](https://site.financialmodelingprep.com/developer/docs/stable/standard-deviation),
[provider data and indicator caveats](https://site.financialmodelingprep.com/faqs?code=data).
