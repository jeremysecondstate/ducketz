# Completed base Loop A provider audit

Audit time: 2026-09-12 04:34 UTC. Cycle
`20260912T040658.065355Z-pid68344` completed at 04:24:41.475106 UTC with
`failure_count=0`, seven symbols and providers Databento, FMP, FRED, Schwab and
SEC. Both native cycle receipts agree. The bounded audit read 43 saved error
metadata files and found zero rows timestamped during this cycle. All seven
technical calculations report zero failures and zero not-ready skips.

The full saved evidence, source hashes, scope counts, observation/receipt times
and diagnostic records are in
[loop-a-provider-audit.json](C:/dev/ducketz/artifacts/analysis/overnight-20260912/loop-a-provider-audit.json).
The audit uses only local files and does not query providers or the broker.
It snapshots only the log prefix through base-cycle completion, because the
same log continues with OPRA maintenance. OPRA and subsequent XNAS history are
outside this audit boundary and are not classified as absent or failed.

| Provider/scope | Verified result and limitation |
|---|---|
| Databento equities | All recorded requests succeeded: 1m, 1s, 1h and 1d continuation for seven symbols, bounded through September 12 00:00 UTC. This is operational EQUS.MINI evidence; the XNAS target source remains separate. Sparse observed endpoints retain their timestamps. |
| FMP corporate | 17 saved normalized corporate scopes per symbol, except SNDK has 16 because no `stock_splits` dataset exists. Six mutable scopes per symbol contain fresh receipts; unchanged statement rows retain their earlier receipts. The zero-failure cycle does not establish that SNDK historically had no splits. |
| FMP commodity proxies | CLUSD, BZUSD and NGUSD all have fresh cycle receipts and September 11 market timestamps. The derived energy context remains old: its latest saved row was received September 2. The retained clock-skew diagnostic is dated September 2 and says 9.471 seconds versus the five-second limit; it is not a new provider failure. The current cycle reports an FMP local advisory, and the old problematic evidence still prevents the all-history derived calculation. |
| FRED | GDP, CPI, UNRATE and FEDFUNDS were fetched during the cycle and all carry native `CURRENT` liveness status. GDP's latest observation is April 1, with 164 days age against its 240-day quarterly limit; the three monthly series end August 1, 42 days against 90-day limits. These are cadence-qualified current-revised observations, not proof of historical vintage availability. |
| Schwab | All seven quote files contain fresh 04:19:41.940212 UTC receipts. The log records seven option-quality publications, with 1,588–2,972 contracts per symbol and saved September 11 decision times. These are completed-session observations and do not establish future execution quote freshness. |
| SEC | Every symbol's filing index has cycle receipts. AMZN and SNDK each added one newly fetched filing text; other saved texts retain their prior receipt dates. The native extractor skips already-processed acceptance timestamps, so reuse is expected. |
| CME requests | Six requests succeeded: context/contracts OHLCV, BBO and MBP. Fresh flat normalized BBO and OHLCV reach September 11 near 21:00 UTC. Both MBP captures hit the 5,000-row limit and explicitly mark `request_limit_saturated=true`; successful transport does not prove complete book coverage. |

The scheduled orchestrator omits new Schwab price-history requests through
`run_cycle(... include_schwab_price_history=False)`. Existing Schwab bar files
are retained calculation inputs, not newly refreshed coverage. COST therefore
still has five intraday Schwab bar scopes while the older symbols also retain
daily, weekly and monthly history. This is a configured skip, not a current
fetch failure. FMP's corporate lane skips `sec_filings_search_symbol` because
the separate SEC lane owns discovery. The option-market target decision is
`MONITOR_ONLY / MARKET_CLOSED_IDLE`, with the next eligible target on September
14 at 13:45 UTC; the closed-session readiness skip is intentional.

The CME advisory has a more specific local cause than its eight-day age suggests.
The native derived-context reader prefers existing partitioned event files
whenever present, excluding tonight's updated flat normalized files. Its selected
partitioned BBO ends September 4 at 05:08:57.977083319 UTC for NQ, and its last
complete five-root OHLC hour is September 3 20:00–21:00 UTC. The exact NQ row
rejected in that candidate hour is:

- Market time: September 3 20:59:58.285261587 UTC.
- Bid/ask: 29,505 / 29,508.5; recorded receive time 22:00 UTC and local receipt
  22:12:07.094558 UTC.
- Source:
  [partitioned NQ BBO evidence](C:/DATASTORE/pools/cme/events/databento/context/bbo-1m/normalized/date=2026-09-03/hour=20/events.parquet).
- Current flat NQ BBO instead reaches September 11 20:59:59.036 UTC in
  [current normalized BBO](C:/DATASTORE/pools/cme/CME_CONTEXT/cme_context_bbo-1m/databento/normalized/CME_CONTEXT_cme_context_bbo-1m.parquet).

This establishes a local source-selection mismatch; the eight-day diagnostic
does not demonstrate an eight-day provider outage. It also does not establish
that using the current files would qualify the calculation. Current September
11 observations are already more than seven hours old at the overnight
calculation, versus the unchanged 15-minute calculation-time gate, and the fresh
MBP requests are limit-saturated. The derived CME context remains unavailable
for current qualification. No quality threshold, input file, source pointer or
production code was changed, and no retry or resume is called for while the
pipeline remains healthy.
