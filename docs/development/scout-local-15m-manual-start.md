# Scout local 15-minute manual trader

Actor: Scout. Task: finish manual-start preparation. Shared source, Scout-specific
execution selection; Atlas Gameplan ownership is unchanged. October 13, 2026.

The human's October 13 correction removes Atlas enrollment and service readiness
as prerequisites for Scout's manual trader. The command-line launcher defaults
to `broker-local` for Scout. The existing `atlas-service` integration remains an
explicit alternative; it is not an automatic fallback or required setup step.
This selection supersedes earlier Scout instructions to wait for an Atlas address,
private enrollment package or authenticated distributed prestart. The only
cross-PC synthesis is Atlas's Gameplan plus Scout's Gameplan. Each trader executes
independently and logs its own activity; another trader's state, verification or
reconciliation is not a startup, acceptance or execution prerequisite.

From the Scout repository in a PowerShell terminal, prepare without live calls:

```powershell
.\.venv\Scripts\python.exe -B -m ml.intraday.scout_prestart
```

The operator starts the actual trader manually:

```powershell
.\.venv\Scripts\python.exe -B -m ml.intraday.live_session --live --scout-admission broker-local
```

The first command neither authenticates to the broker nor starts a worker. Its
ready result establishes local installation, dependencies, account identity,
symbol membership and saved-book readiness. The second command uses the existing
local credentials and begins the real session. Current broker/provider access,
fresh quotes and accepted orders can only be observed after that manual start.
No cash balance, held position, Gameplan allocation or Atlas connection is a
startup requirement. Ctrl-C stops this worker gracefully; resting orders and
their exact identities remain recorded for recovery.

Each completed quarter-hour cycle uses the existing full configured ensemble,
original fixed target, independent technical sizing and half-up whole-share
rounding. A valid desired trade is saved before capacity adjustment. The fresh
broker account capture supplies available cash after working BUY commitments and
held shares after working SELL commitments. Quantities may shrink to zero without
erasing the forecast or intended trade. SELL availability spans actual same-symbol
holdings without an intraday horizon budget. Unknown cost basis, horizon ownership
and realized P&L are not invented.

Orders are submitted and managed locally. Durable local permits bind the saved
intention, account snapshot, quantity and manual session; they are not Atlas
reservations. The local journal permits one send per attempt and retains exact
broker IDs, partial fills, terminal status and cancellation evidence. A lost
submission reply remains uncertain and cannot authorize another POST. Replacement
waits for the previous Scout order's terminal evidence. Other streams' orders
are included in observed capacity but are not cancelled by this trader. The
strict 16:45 Pacific new-submission cutoff remains in place.

Independent PCs can submit between another PC's account read and broker POST.
This mode therefore does not provide atomic cross-PC capacity reservations.
Broker acceptance and rejection remain authoritative; the local single-send
journal prevents duplicate Scout attempts, not simultaneous peer submissions.
Local books do not claim to rewrite Atlas's horizon allocation ledger.

The session writes `intraday/manual-session.json`, a session-specific status and
event log, the existing forecast/acquisition stores, and Scout's private books
under `intraday/scout` in this PC's DATASTORE. The first-session watch observes
those saved identities and outcomes. Installation and offline fixtures are
separate from an actual human-started session and its measured trading results.
