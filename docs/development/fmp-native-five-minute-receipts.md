# Native FMP five-minute receipts

`datafetching.fmp_price_history_receipts.capture_native_five_minute_history`
captures a bounded inclusive New York date window for explicitly configured
equities. It reuses `FmpCorporateDataProvider` authentication and its stable
`historical-chart/5min` adapter. Importing the helper makes no network requests.
Each call accepts at most 64 symbols and one through seven calendar days; it
continues independent symbols when a provider request fails and reports safe
error types. It does not call account or order APIs or change a subscription.

The request explicitly selects `nonadjusted=true` and `extended=true`. These are
saved request settings: the returned chart has no independent field attesting
either setting. FMP's source identity does not establish consolidated market
coverage. The [official endpoint](https://site.financialmodelingprep.com/developer/docs/stable/intraday-5-min)
describes native five-minute OHLCV. Its [FAQ](https://site.financialmodelingprep.com/faqs?code=chart)
describes equity intraday dates as exchange local time, using the term EST.
This implementation interprets US-equity dates with `America/New_York`, including
daylight saving changes, and treats the timestamp as interval open. These
conventions are explicit in every receipt; original payload strings remain
available for later review. Split/session coverage cannot be proved solely by
the requested options.

Responses are retained under
`stocks/<symbol>/bars/5m/fmp/raw-history/<envelope-sha256>.json`. The immutable
envelope includes the exact request, actual request-start and response-receipt
clocks, payload SHA-256, native five-minute identity, timezone and response.
Complete fsynced bytes are published atomically without replacing an existing
receipt. Optional reused responses must have matching original request and
payload hashes, and retain their actual original clocks. Credential-shaped
responses are rejected; exceptions are reported without URLs or provider text.

`load_native_five_minute_history(root, symbol=...)` returns a dataframe, receipt
paths and compact inventory. It verifies both hashes and declared source
identity. Eligible rows have a parseable native date on a five-minute boundary,
finite positive OHLC, consistent high/low, positive finite volume, and a candle
end no later than the response receipt. Present synthetic, no-trade or
source-empty markers must be explicit boolean false. Invalid, zero-volume,
out-of-request and incomplete rows remain in the raw receipt but are omitted
from the projection. There is no filling or reconstructed volume. Missing feed
observations do not prove that no trades occurred.

The reader preserves dated revisions. Consumers apply their actual evidence
cutoff before selecting the latest revision and reject simultaneous conflicts.
Historical response receipt time is never backdated to the market event time.
New Gameplan source policies may use these observations for later reconstructed
labels; that does not make the response available to a past forecast or modify
older frozen source contracts. Each target must still use one source for both
endpoints and satisfy its existing boundary tolerance. This helper introduces
no dependency between independent traders.

Run the offline checks with:

```powershell
.venv/Scripts/python.exe -B -m pytest tests/test_fmp_price_history_receipts.py -q
```

The tests use fake adapters and temporary directories. They cover actual request
parameters, original receipt reuse, summer/winter timezone conversion, native
row filtering, revision preservation, empty results, integrity/source-identity
checks, atomic publication, safe independent failures and bounded request scope.
