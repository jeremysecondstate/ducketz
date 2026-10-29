# Historical uncertain orders and later manual sessions

A previous-day order with an unknown submission result could send every fresh
forecast for the same symbol back into cancellation recovery. Without an exact
broker order ID, that recovery cannot query or cancel the order. Repeating it in
every later manual session permanently blocked fresh intentions for that symbol.

The execution predicate now distinguishes an ID-less, unfilled `RESERVED` or
`UNKNOWN` attempt whose target ended before the current manual session's Pacific
date. Such a historical attempt does not force a fresh forecast into the old
order's cancellation path. Its original native and authority records remain
intact, and their complete unresolved cash or share commitment still participates
in the existing capacity calculation. Any new intention remains subject to its
own fresh quote, actual holdings, cash, commitment and submission checks.

An attempt with an exact broker order ID, any recorded fill, a current or future
Pacific target date, or a missing or invalid target time keeps the existing
reconciliation path. The predicate does not prove that an old order was unsent,
release a commitment, infer a broker ID, cancel an order, or replay an old
forecast. Original order evidence remains necessary to resolve uncertainty.

The portable source package is
`docs/development/patches/intraday-historical-unknown-liveness.patch`, with exact
member hashes and newline conventions in the adjacent JSON manifest. It contains
only the owned execution change and new synthetic integration tests. The source
consumer has existing local composition beyond the shared base. Apply these hunks
only to an exact reviewed preimage, or prepare a separately reviewed adaptation
when the surrounding source differs. Never overwrite a peer's complete consumer.

Run `python -B tests/check_intraday_historical_unknown_package.py` for patch
integrity, synthetic predicate behavior and exact preimage reconstruction when
the complete target source is present. The portable check imports no application
module and calls no broker or provider. Its predicate fixtures do not establish
the behavior of an absent application. Run
`tests/test_intraday_historical_unknown_liveness.py` with the adapted executor and
the existing uncertain-commitment, cancellation, interrupted-submission and
account-execution fixtures for full integration evidence. Keep actual check
results with the completion record.

Publication, reviewed local installation, a sealed runtime generation, loaded
code and live behavior are separate facts. This package does not start or restart
a trader, change a manual stop, alter schedules, extend a session, or modify
private ledgers. Both PCs retain their own scientific selections, symbols,
execution state and manual session authority.
