# Intraday provider plan and observation boundaries

Source identity includes provider, dataset, schema, endpoint and semantic request
parameters. A transport repair must not reuse a previously frozen model selector
for different observations. Per-PC recipes, model parameters and symbol lists
remain private. Applying shared source does not migrate a saved recipe or prove
which version a running process has loaded.

## CME depth

The reviewed Databento Standard plan includes rolling-month **Historical**
MBP-10 and MBO. It does not include those schemas over Live. Legacy Live depth
requests remain visible under their original identities as `UNSUPPORTED`; the
adapter rejects them locally before credentials, cached responses or a Live
client are consulted. Other entitled Live schemas keep their existing identity.

`market_collection_history.py` defines the separate
`databento-standard-historical-depth-v1` policy and
`historical_available_window` transport. The new request identity includes the
Historical endpoint and immutable bounds. It queries exact schema availability,
then selects the latest whole 15-minute window ending no later than the requested
quarter end or the provider's available end. The entire window must lie inside
the provider range and the rolling calendar month. Historical publication delay
is preserved as an explicit lag; an older window is never labeled current.

Before downloading, the documented metadata API must quote exactly zero dollars
and disclose nonnegative record and billable-byte estimates within the policy's
one-million-record and 256-MiB limits. Nonzero or invalid quotes block download.
Databento documents that count and billable-byte estimates may over-report for
windows that are not multiples of ten minutes. These estimates bound the request;
they are not an exact expected-record checksum for a fifteen-minute response.
Native decoded record count and actual compressed storage bytes are reported
separately. Completion means a successful, fully decoded bounded source response,
not independently established market-wide observation completeness.
Oversized windows remain `CAPACITY_BLOCKED`; they are not sampled into apparent
full-quarter success. A total 30-second network deadline and checked decoder
deadline, bounded native streaming, disk reserve and exact metadata validation
constrain collection. An over-limit, malformed, mismapped or truncated native
response cannot become complete evidence. The full retained DBN response has a
checksum; failures retain partial bytes with an explicit unsuccessful status.

The small context projection preserves only the first and last actual depth
record per instrument, together with the native observation count. This is not
a reconstructed MBO order book. Durable cache reuse verifies the native checksum
and keeps the original acquisition clocks. Requested and effective ranges,
provider availability, quote, count, bytes and receipt clocks remain distinct.
The production collector persists append-only receipts through its regular
queue and receipt index. Newly collected Historical identities remain outside
the current model contract unless a deliberate reviewed migration admits them.

See the reviewed plan inventory in
`docs/databento-plan/databento_standard_plan_data_access.md` and the primary
[Historical API reference](https://databento.com/docs/api-reference-historical).
Plan labels alone do not establish request-level entitlement or completeness.

## FMP and Schwab

Configured native commodity symbols use separate documented FMP `quote` requests
under `fmp-native-single-commodity-quote-v1`. A denial for one symbol cannot hide
another symbol's successful observation. The old bulk identity remains explicitly
disabled. HTTP 402 is an entitlement denial, not proof that the commodity has no
market observations. No fund substitution or fabricated previous-close quote is
permitted. FMP intraday chart date parameters use America/New_York calendar dates;
normalized event clocks are still filtered against the exact UTC window.

Schwab options book subscriptions use a separately bounded rotating subset of
the configured native option universe. Evidence distinguishes requested, planned,
submitted and acknowledged scopes. A subset is `PARTIAL`, never full-universe
coverage. The observed operational limit is a conservative collection bound,
not a statement of the provider's maximum entitlement. Equity, futures and L1
option service outcomes remain independent of book-service capacity failures.

## Observed model inputs

The context reader checks both canonical rows and retained native fields for
synthetic, no-trade and source-empty markers. Present markers must be explicitly
Boolean false. Actual bars require positive volume and valid OHLC geometry;
actual trades require positive observed size. Quotes do not require positive
trade volume. Missing feed rows never prove that no market trades occurred.
The context cache is versioned so vectors built before these checks are not
silently reused. Feature support must be established from the training fold;
an optional column absent from that fold cannot influence fitting or validation
through an inference-time value.

Offline tests cover exact old/new identities, local transport rejection, causal
historical windows, zero-cost and capacity gates, native decode and truncation,
cache integrity, partial failures, exchange-date boundaries, source markers,
option scope accounting and training support. They do not establish current
provider availability, install private configuration, retrain operational models
or start a trader.
