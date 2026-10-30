# Preparing the 15-minute opening target

Jeremy's October 29, 2026 direction retires scheduled Gameplan production and
explicitly authorizes preparation before 04:00 Pacific for the 15-minute
trader's 04:00 target. Preparation does not create a trading session.

## Data and timing

The target definition is unchanged. A reference candle from 03:30 to 03:45
predicts the 04:00–04:15 target. Its actual completed observations must be
available after the configured publication offset before fitting or inference.
The trading calendar, original feature order, selected specifications,
probabilities, calibration, causal training cutoffs and target expiry remain
authoritative. Missing observations stay missing.

The existing data-only owner starts early enough to collect and prepare that
reference. Atlas's existing Windows data registration wakes at 03:40, including
its retained daily rollover wake. Its identity and all unrelated settings remain
local. The trader registration retains its existing 03:55 trigger and waits
until 04:00; execution cannot submit or cancel orders before 04:00. Each PC
reviews its own bindings; receiving this source does not edit a peer schedule.

Daytime collection and nightly catch-up use the same canonical history.
`tools.intraday_history_catchup` calls the existing `refresh_ohlcv` continuation,
which first reuses retained intraday minute responses, then overlap-fetches and
deduplicates. It uses the existing model supervisor ownership lock and DATASTORE
writer lock. Its configuration, profile and watchlist are supplied explicitly;
it does not fit models, fetch unrelated inputs or activate trading. Reports
distinguish complete, incomplete and failed acquisition.

## Opening prediction ownership

The existing model owner runs `ml.intraday.opening_preparation --wait` at its
03:30 wake with the local datastore, profile and watchlist. Earlier wakes retain
normal causal evaluation and selected-specification production. The opening
helper consumes System 1A's immutable ready inputs after 03:45 plus the
publication offset and uses the existing fitting and inference implementation.
It uses the same local scientific configuration as the trader. It has no broker,
execution queue, session-activation or manual-stop capability.

The helper preserves the exact original forecast, issue and training clocks,
probability, direction and target in a separate local preparation store. At
04:00 an already authorized session may adopt it without refitting. The adoption
links the original and session forecast, checks the configured universe and
scientific configuration, preserves provenance, and durably claims the original
so another session cannot replay it. A stopped session cannot consume it.
The normal expiration and capacity rules still apply.

An admitted fit may finish after 04:00; it retains its real completion time and
04:15 expiry. The report distinguishes a forecast prepared before opening from
a late or missing forecast. No deadline invents readiness or cancels independent
data/model continuation. Actual provider availability and fitting time can leave
individual symbols unavailable at opening.

## Stats and retained controls

Stats review only original 15-minute saved forecasts, mature observed outcomes,
and the independent session's actual orders and fills. Preparation records are
not trades. Adopted opening forecasts retain their original science and outcome
attribution. Do not produce a next-day Gameplan or score manufactured outcomes.

BUY remains original unrounded probability at least 0.5001; SELL is at most
0.4999; HOLD is strictly between. Preserve pending, partial and UNKNOWN order
evidence, original identities and manual stops. Opening preparation never starts
or restarts a trader. Process renewal remains limited to an already authorized
session and stops with that session.

Retired Windows registrations are deleted locally, and the historical launcher
and registration helper reject activation. Historical source, task definitions,
receipts, raw data and fitted artifacts retain their existing preservation and
privacy rules. Two redundant paused legacy Codex owners were deleted; active
15-minute owners retain their native IDs even where an ID contains an old name.

## Validation and installation

Offline checks cover real temporary stores, mocked fits, causal/calendar edges,
exact forecast adoption, retries, stops, expiry and pre-opening execution
rejection. Native task readback verifies saved definitions without running them.
Portable source packages reconstruct owned changes for review; their projected
checks do not substitute for full installed-dependency integration tests.

Seal the reviewed complete source generation with `tools/trader_installation.py`
before reporting runtime installation. Source publication, sealed installation,
loaded processes and a naturally observed opening session remain separate facts.
Do not call a provider, train models or operate a live trader to test this change.
