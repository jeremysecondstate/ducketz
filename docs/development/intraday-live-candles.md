# Live 15-minute candle collection

Each manually started 15-minute trader collects its own symbols independently
of model fitting and target scoring. At each quarter-hour boundary, after the
configured publication offset, it requests the exact preceding completed
15-minute interval from the existing one-minute provider binding. Missed
intervals remain in its durable collection queue while newer intervals receive
priority. No forming minute is included.

The existing aggregator requires all fifteen consecutive minute slots. It uses
the first open, maximum high, minimum low, last close and summed volume. A
missing provider minute remains pending unless the existing repeated covered
response and same-session prior-price rules establish a no-trade minute.
Such a minute is explicitly synthetic, flat at the known prior close, with
zero volume. It is never presented as a traded minute.

Provider responses and coverage evidence are retained in
`intraday/acquisition.sqlite3` under the local DATASTORE. Validated aggregate
candles are retained in `intraday/shadow.sqlite3` and merged into each symbol's
in-memory history by opening time. Original history and earlier observations
remain intact. These live records do not rewrite the separate historical
Parquet exports.

The session cutoff is 17:00 in `America/Los_Angeles`, fixed to the date on which
the human started the session. The timezone follows PST and PDT. At that cutoff,
new forecast work, scoring fetches and order submissions end. The collector
continues only the final 16:45–17:00 interval for local symbols that have not yet
been saved. Provider publication delays and retry waits may extend this final
collection past 17:00. Existing minimal predecessor recovery remains available
when required to establish a confirmed no-trade constituent's prior price.

Once every local symbol's validated final candle is durable, collection and the
manual session finish with `market_close` as the stop reason. The session's final
forecast outcomes are scored from those saved candles without another fetch.
On holidays or short sessions, a final interval outside the established
ingestion envelope is recorded as inapplicable; no candle is invented.
No 17:00–17:15
interval or next-day session is started. A human stop takes priority over final
collection. Already received responses are preserved, and resting broker order
identities are retained; this flow does not automatically cancel orders.

The consensus display reports reference-candle observed and confirmed no-trade
minute counts separately from the number of successful models. Older saved
forecasts use their linked immutable input snapshot when that metadata exists;
missing or inconsistent provenance is shown as unknown.
