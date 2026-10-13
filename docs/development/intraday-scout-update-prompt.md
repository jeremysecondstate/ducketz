# Scout: prepare the independent local 15-minute trader

Apply Jeremy's permanent October 13 direction in [Independent trading systems](independent-traders.md). Scout makes and executes its own eleven-symbol 15-minute forecasts and logs its own activity. It does not combine, reconcile or verify its trader with Atlas's 15-minute trader or the Gameplan trader.

Every night Scout also creates its own eleven-symbol Gameplan research package. Atlas and Scout synthesize those two Gameplans once into the same 22-symbol cash/stock Gameplan returned to both machines. That is the only synthesis. Atlas executes the combined Gameplan; Scout runs only its local 15-minute trader.

Install applicable completed shared source under the existing standing peer-adoption authority, preserving Scout's symbols, native bindings, private data, running work and original receipts. Routine implementation and nightly preparation require no repeated human or peer approval. Keep trading off until the human's manual start.

Use `intraday/execution/config.json`, `intraday/execution/holdings.sqlite3` and `intraday/execution/authority.sqlite3` as the local 15-minute execution state. Do not depend on Atlas's account Gameplan activation, a shared ledger, a distributed reservation/grant service, another worker's liveness, or a compatibility attestation. Preserve old mixed-ledger history; never invent fills or reuse another trader's native order identities.

Preserve the established causal 15-minute target, complete per-symbol model ensemble, quarter-hour publication timing, independent technical sizing, current broker capacity adjustment and own-order cancellation/recovery. The sizing recipe remains `0.015 * current equity / fresh side price * own-symbol TA score`, with half-up rounding. Actual cash and available shares can reduce executable quantity, including zero; forecast results remain saved and scoreable.

The human uses Scout's `Start-15m-Trader.cmd` when ready. The worker observes the configured market window and strict 16:45 Pacific new-submission cutoff. It records actual own broker order responses and fills, never resending an unresolved submission by guessing its outcome. A new forecast replaces only this trader's own previous intention under its existing cancellation policy.

Keep raw history, fitted models, native databases and account state private. Optional telemetry remains informational; neither its delivery nor a peer receipt gates trading. Publish completed shared source and report actual local installation, preparation and manual-session state without claiming the peer performed work not yet observed.
