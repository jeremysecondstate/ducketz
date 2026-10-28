# Atlas data worker renewal

Atlas's independent 15-minute service uses a fresh installed worker after each
completed iteration. The trading service renews its execution, forecasting and
scoring workers. The data service renews its preparation, candle collection and
optional market-input workers independently.

The lifetime anchor can keep the same PID. Each completed worker exits before
its successor starts, and the successor selects the latest complete installed
source and Python dependencies. Working checkout edits become available after
reviewed installation is sealed with `tools/trader_installation.py`.

When an installation changes the controller itself, the controller lets its
admitted passes finish and transfers their exact state to a fresh installed
controller. The original anchor retains the lifetime lock and kernel ownership.
This transition preserves the session and does not repeat initial activation.

## Data iterations

- Each preparation lane completes its own symbol pass. It publishes completed
  immutable inputs immediately; another lane's unfinished work does not hold
  up its next pass.
- Candle collection completes a bounded pass and carries its collection cursor
  and fair selection of pending days into the next worker.
- Market inputs finish a quarter interval, including the existing collection
  and retry subpasses, and retain pending references across worker replacement.

The host owns the existing data-only runtime lock across worker transitions.
Workers retain their original session, frozen configuration, symbols, reference
clocks and daily close. Durable collection and published inputs remain available
through replacement. The existing configured daily data wake governs admission
of subsequent days; renewal does not invent a new schedule.

A successor requires the previous process to exit with its planned-renewal code
and provide a matching durable completion receipt. Unknown exits and malformed
receipts are failures, not automatic start requests. A manual stop ends the
whole data lifetime, including its children. Worker renewal cannot undo it.
The data service has no trading activation or order authority and does not read
or write the trader's manual-session controls.

## Evidence

Data renewal records live under the local datastore's
`state/intraday-data-renewal/`. Each iteration has its own request, loaded-source
record and completion receipt. Verify matching identities, changed worker PIDs
and selected generation across successive completed iterations. A heartbeat or
an unchanged anchor PID alone does not establish whether workers renewed.

Publication, complete local installation, loaded generation and successful
natural data processing are separate facts. Offline tests use isolated stores
and simulated work; production services are not invoked as test fixtures.
