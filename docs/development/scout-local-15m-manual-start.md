# Scout local 15-minute manual trader

Actor: Scout. Current standing operating contract, October 13, 2026.
Shared implementation; each trader owns its local execution and logs.

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
.\.venv\Scripts\python.exe -B -m ml.intraday.manual_launcher --live --scout-admission broker-local
```

The first command is an optional local diagnostic; it neither authenticates to
the broker nor starts a worker and is not another approval or operating gate.
It reports local installation, dependencies, symbol membership and saved books. The second command uses the existing
local credentials and begins the real session at the selected opening. An evening
launch waits for the next eligible session's 04:00 Pacific opening; see
[early manual startup](intraday-manual-wake.md). Current broker/provider access,
fresh quotes and accepted orders can only be observed after that manual start.
The live executor obtains its account fingerprint directly from its existing
broker at manual startup. No Gameplan account configuration, cash balance, held
position, Gameplan allocation or Atlas connection is a startup requirement. Ctrl-C stops this worker gracefully; resting orders and
their exact identities remain recorded for recovery.

Scout also begins a graceful stop automatically at **5:00 p.m.
America/Los_Angeles** on the selected operating date. The deadline follows
Pacific daylight-saving time and stays fixed if a session runs past midnight.
At the deadline, order authority ends and model/scoring workers stop taking new
work. Active computation finishes saving its real output. The data worker drains
the latched day's remaining windows, including the final 4:45–5:00 p.m. window
once published, before the process exits. Actual missing data remains pending
while existing acquisition retries continue. Status and the session
log record `automatic_17_00_pacific` separately from a manual stop or failure.
This does not cancel resting broker orders or sell positions. The command-file
and `manual_launcher` paths accept an evening start for the next eligible day.
The direct `live_session` entrypoint retains its historical immediate stop for
launches at or after 5 p.m. Once a session stops, the next day still requires a
new manual start. Atlas's sessions are unchanged.

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

## Every-night standing authorization

Jeremy's October 13, 2026 instruction is standing preauthorization for every
night's existing preparation, Gameplan synthesis and delivery. Continue those
operations automatically without requesting human or peer confirmation. The
nightly handoff is Atlas Gameplan + Scout Gameplan -> Scout synthesizes -> Scout
sends the result to Atlas. Sending that completed result finishes the handoff;
there is no third account snapshot, Atlas acceptance, cross-trader verification,
reconciliation or readiness ceremony before completion or the next night's run.

Gameplan, Atlas 15m and Scout 15m execute independently and keep their own logs.
Each trader obtains actual capacity from its own existing broker connection and
manages only its own orders. No trader waits for another trader's state, service,
locks, approval or reconciliation. Jeremy's manual first trader start remains a
manual action; it is not an invitation to ask for approval again. The existing
exact-byte source publication and private-data handling rules remain engineering
and privacy procedures, never additional nightly operating prerequisites.
