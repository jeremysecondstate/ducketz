# Independent Scout submission contract

The October 13 standing human direction replaces the earlier proposed distributed Atlas reservation protocol. Scout submits and manages its own 15-minute orders locally. No Atlas grant, Gameplan compatibility proof, cross-trader verification, shared lease or combined native ledger is part of Scout startup or order admission.

See [Independent trading systems](independent-traders.md) and [Scout adoption](intraday-scout-adoption.md). Atlas runs the combined Gameplan trader plus Atlas's 15-minute trader; Scout runs Scout's 15-minute trader. Only the two nightly Gameplans are synthesized, and the same combined 22-symbol plan returns to both PCs.

Scout's local execution binding and original order records live under `intraday/execution/`. Each order uses the local forecast, desired quantity and actual available broker cash/shares. Record the real order identity, partial fills, terminal status and cancellation outcome. An unknown submission remains unknown and must not be resent as a new order. These are this trader's own broker interactions, with no inspection or reconciliation of another trader.

Nightly preparation and routine repair are permanently authorized. Trading begins only from the human's manual launcher and honors the local stop control and existing market/cutoff policy. Retain the historical shared-grant proposal and its receipts in Git history; its request/grant state machine is not an operating prerequisite.
