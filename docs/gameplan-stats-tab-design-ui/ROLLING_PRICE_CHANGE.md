# Rolling 24-Hour Price Change

The main Gameplan Stats execution column shows current market prices directly
beneath Order details. The order table, filters, counts and detail records keep
their saved-evidence meaning. The former broker-observation and coverage footer
remains available by hovering over Order details or the execution status.

Membership comes from this application's production watchlist, using
`production_watchlist_path()` and `read_symbols()`. A local watchlist takes
precedence over the shared default. Candidate-run environment overrides, the
combined Gameplan universe, saved orders, review dates and UI filters never
define this panel's membership. Atlas and Scout use the same source with their
own production profiles; model recipes and trader controls are untouched.

## Calculation and source

Each refresh captures a single aware UTC instant `T`. For every member:

```text
change_pct = 100 * (latest_price_at_or_before(T)
                    / latest_price_at_or_before(T - 24 elapsed hours) - 1)
```

The adapter requests fresh native Schwab one-minute history with extended hours
enabled through the existing `DataFetchingSchwabSession`. Both endpoint prices
come from the same response, on the same provider basis. Quotes and conventional
previous-close change fields are not used. One-minute timestamps label candle
starts; their closes become eligible at start plus one minute. The reported
observation time is that completion bound, not an inferred last-trade time.
Only completed, finite, positive-price, positive-volume candles qualify.

The existing FMP connection supplies a fresh `splits` response for each member
on every refresh. A confirmed empty response is valid; failed, missing or
malformed split evidence makes the member unavailable. The shared split
validator back-adjusts older prices only when boundary prices confirm the
reported factor, preserves a factor already reflected in native history, and
rejects ambiguous boundaries and unexplained large discontinuities. FMP ex-dates
are civil dates interpreted at 04:00 Eastern, preserving the preceding evening's
winter extended session. Prices are comparable on the resulting current split
scale. This is a price return without dividend or total-return adjustment;
provider omissions remain a source limitation.

FMP documents the [split endpoint and factor semantics](https://site.financialmodelingprep.com/how-to/how-to-track-stock-splits-and-corporate-actions-with-a-free-api).

## Closures and staleness

The installed `exchange_calendars` XNAS schedule supplies session dates,
holidays, DST and early closes. The conservative native-history envelope is
04:00–20:00 Eastern on full sessions and 04:00–17:00 on scheduled 13:00 early
closes. Other shortened sessions use their regular exchange interval. The
early-close extension follows Schwab's [holiday trading FAQ](https://www.schwab.com/stocks/extended-hours-trading).

At each cutoff, the latest eligible completed candle is selected. A fresh,
successful provider response may contain sparse extended-hours trades; the
absence of a trade near 20:00 does not by itself mean the feed is stale. The
refresh path assesses missing time against the regular exchange session, with
a ten-minute allowance. Extended-hours observations remain eligible prices;
their absence does not consume that allowance. Actual price timestamps remain
visible in the tooltips, so a 16:11 trade carried into the evening is identified
as that actual observation rather than a new 20:00 price.

Both cutoffs may legitimately use Friday's final observation during a weekend,
with an explicit closed/as-of status and actual Friday timestamps. Missing
Friday afternoon regular-session evidence remains stale after the weekend.
Evidence older than the bounded ten-calendar-day history request is never
considered fresh. This policy requires a new successful server request on each
refresh; old saved files cannot establish fresh coverage. There is no
interpolation, synthetic price, missing-as-zero substitution or unlabelled
multi-day carry.

## Display and refresh

The full equal-weight average uses all unrounded member percentage changes and
is available only when every member has a valid pair. Display rounding is two
decimal places. Coverage accompanies an unavailable average; the UI does not
display a partial average. Green, red and neutral colors represent positive,
negative and exact-zero changes. Stale and unavailable rows are explicit.
Tooltips expose actual prices, completion bounds, candle starts, UTC cutoffs,
source, split basis and the closure policy. A compact Pacific as-of label and
current-price/session-independent context remain visible for historical Stats
sessions.

The Refresh button inside the rolling-price panel requests fresh prices only,
without reloading saved Stats predictions, orders or history. It shows
Refreshing… while the request runs and becomes available again after success
or failure. Initial load, the page-level Refresh and the existing five-minute
visible-tab timer also request new evidence. A separate daemon price lane has
its own request ID and never waits
for saved prediction/order/history readers. Three reader threads bound request
fan-out; existing Schwab history and FMP calls each use ten-second HTTP timeouts.
A 45-second batch deadline returns explicit unavailable rows for unfinished
members, rejects late replies, and prevents new queued calls after expiry.
The Tk thread only applies matching replies, and destruction invalidates all
price requests. Saved prediction/order results can finish while prices load.

Offline verification injects both market providers. Native Tk fixtures at normal,
medium and narrow widths include complete coverage and stale/missing coverage.
No live data call, trading, training, schedule change or worker restart is part
of verification. Installed GUI source takes effect at the next normal launch;
fixture rendering alone is not evidence of a loaded desktop application.
