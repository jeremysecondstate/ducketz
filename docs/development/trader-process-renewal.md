# Manually started trader sessions and automatic process renewal

Jeremy's October 26, 2026 direction authorizes planned worker renewal inside an
already manually started Atlas Gameplan or independent 15-minute session. It
supersedes blanket restart prohibitions only for this continuation. Initial
starts, starts after manual stops, reboot starts and extensions of session
lifetime remain unauthorized. Scheduled preparation does not start trading.

## Lifetime and iteration boundaries

The two existing command launchers enter a small standard-library Windows
kernel lifetime anchor. It owns the existing trader lock throughout the session,
including gaps between generations, and puts itself and all descendants in a
non-inheritable kill-on-close Job Object before creating workers. Duplicate
launches refuse ownership, including a still-running legacy worker. They never
adopt or signal a process found by PID. A stale lock is retained as evidence.

Every admitted turn runs a new interpreter and the selected generation's
`tools/trader_worker.py` controller adapter. Only a matching durable handoff
receipt plus exit 75 permits the next generation. The old process must have
exited first. Exit zero with a terminal result completes that lane; unknown
failures end the session. There is no restart-on-error loop.

| Lane | Complete iteration and preserved cursor |
| --- | --- |
| Gameplan | Full control pass across due symbols/horizons. Immediate broker-state recapture after a partially submitted batch stays in the same worker until the remaining universe is handled. Preserve original day/open/close, attempted slots, quote recovery, preflight, counters and absolute next poll. |
| 15m execution | Entire due forecast queue across symbols plus due recovery pass; preserve session start and absolute recovery deadline. This is a recurring control pass, not a new scientific cycle. |
| 15m forecasting | Complete admitted multi-symbol scientific cycle or supported bounded recovery pass. Preserve session/configuration identity, original quarter reference and fairness cursor; reuse durable completed forecasts, selected models and frozen inputs. |
| 15m scoring | Full scoring pass; preserve audited identities and original observations. |
| 15m candles | Full multi-symbol collector pass, using durable collection cursors. Final collection and saved-outcome scoring run once at natural close. |
| 15m optional market inputs | One quarter acquisition interval, including its one-second collection/retry subpasses; preserve pending references. Streams close before renewal. This lane has no execution authority. |

The 15m lanes remain concurrent, so model fitting does not hold up execution.
Only its execution lane can submit orders. All lanes share that trader's single
logical session and original 17:00 Pacific trading close; final candle catch-up
retains the existing post-close collection behavior and cannot place orders.
Gameplan retains its supported market calendar and wait-for-open behavior.
Successors start importing during the existing pause and honor its absolute due
time; interpreter startup does not reset scientific or market clocks. If startup
overruns that due time, existing expiry checks still apply; no expired signal is
replayed to compensate.

## Stops, orders and evidence

Ctrl+C, console close, manual controls and loss of the exact kernel owner remove
authority for the whole session. A durable stop latch survives all handoff
phases. Official Gameplan FALSE controls latch before changing the control file;
turning them TRUE later cannot resume this session. Initial manual activation
runs once. Successors never clear stop records or repeat account setup.
Initial control activation compares the captured revision and writes through one
Windows handle that excludes competing writes and replacements. A successful raw
stop edit before that handle is acquired is latched; an edit after it is released
remains visible. A competing raw writer receives a sharing error during this brief
guard. Official stop APIs latch first even if their file write is denied. The exact
prior control bytes remain private session evidence; any activation error fails
closed before execution.

Planned renewal has its own disposition and is not a manual stop, session
closeout or final scoring event. Existing SQLite ledgers, reservations, exact
order identities, submission evidence, partial fills and UNKNOWN commitments
remain in the original datastore. Existing reconciliation handles resting
orders; renewal neither waits for them to fill nor cancels/replaces/resubmits
them. Unknown worker failures are terminal and retain ambiguous evidence for
the next explicit manual session's supported recovery.

No cross-trader ledger, health, peer acceptance or publication prerequisite is
added. Each trader retains its own execution state. The October 19 direction
thresholds, live-capacity sizing, horizon ownership and scientific model recipes
are unchanged.

## Complete local installation generations

The coordination release installer installs coordination helpers, not application
source. Continue the existing exact-preimage reviewed local source installation,
then seal the complete reviewed code and Python runtime with
`tools/trader_installation.py seal --root LOCAL_GENERATION_ROOT --spec PRIVATE_SPEC`.
The default local generation root is `scratch/trader-installation`.

The private spec supplies `source_root`, an exact SHA-256 `source_inventory`,
`python_root`, `site_packages`, `reviewed: true` and review metadata. Include the
complete application dependency closure and packaged source resources. Exclude
credentials, local watchlists/profiles, databases, models, raw data and ledgers.
The installer copies interpreter/standard library and dependency packages to a
content-addressed runtime, removes editable import hooks and generated caches,
verifies stable input bytes before and after copying, verifies the sealed output,
then atomically replaces `active.json`. Old generations remain available.

Future reviewed source/dependency installations must finish this sealing step;
ordinary edited working files are not an installed generation. Use the same
installer in existing source-owner workflows, without another task or watcher.
Never mutate a published generation. Its manifest and complete source bytes are
checked at each worker start; a newly selected generation receives full runtime
verification. `-I -S` and explicit frozen paths prevent editable imports from
mixing the live checkout into a worker. Local credentials, symbol selection and
bindings are read explicitly from the original installation; scientific session
configuration stays frozen.

An incomplete staging directory is invisible; the previous complete generation
remains selected. An invalid selected generation ends the session visibly and
does not create a rapid failure loop. It requires a corrected installation and a
new manual launch. No peer or publication wait is involved.

Controller updates also continue this session. When its installed source or
interpreter changes, the current controller stops new scientific admissions,
lets admitted full passes checkpoint, and keeps execution passes running while
science drains. After the final execution pass it durably transfers all cursors,
completed lanes, counters and original deadlines to a freshly loaded controller.
The original process retains the kernel Job Object and legacy lock and becomes
only a waiting lifetime anchor. The successor validates its exact living parent,
original owner, token and single-use handoff; it never repeats initialization or
activation. Later controller updates use the same relay protocol. Stops remain
latched throughout; killing the original anchor kills the entire descendant
chain. Unsupported protocol versions fail closed with no restart loop.

The active lifetime record at `state/trader-renewal/<trader>/active.json` and
per-generation receipts provide controller/worker identity and heartbeat
evidence. The 15m `intraday/manual-session.json` remains the logical status and
manual-stop surface; heartbeat updates do not repeatedly overwrite that stop
surface. A process startup or new runtime verification can overrun a polling
due time; absolute session/scientific clocks stay unchanged, and expiration
checks remain authoritative.

## Activation and delivery evidence

Installation does not alter either already running live process. At a time of
Jeremy's choosing, stop each old session with its existing manual control and
launch its same command file once. Duplicate launching while the old worker is
alive refuses ownership. New automatic renewal applies only within that new
manual session. Source publication, local generation installation, loaded code
and naturally exercised live behavior are separate recorded facts.

Offline coverage uses isolated stores, simulated brokers and real Windows
subprocesses. It covers multiple generations, changed behavior, working/partial/
UNKNOWN orders, deadlines, stops at handoff phases, duplicate ownership, stale
locks, crashes, controller termination, installation concurrency and terminal
completion. Real brokers and running traders are never used for verification.

## Reusable existing-task instruction

October 26, 2026 manual-session renewal direction: follow
docs/development/trader-process-renewal.md. Jeremy manually starts each Atlas
trader. Within that already manually started session, safe planned renewal after
every completed iteration is authorized and loads the latest complete locally
installed generation. A manual stop ends the entire session and forbids further
renewal. This supersedes older blanket restart prohibitions only for this
automatic continuation; never initially start a trader, resume after a manual
stop, add reboot autostart, extend its session, or operate live traders to test
infrastructure. Preserve independent state, working/partial/UNKNOWN orders,
original science, timing, local bindings and stock policy. After reviewed local
source/dependency installation, seal the complete exact-byte generation with
tools/trader_installation.py before recording it installed for renewal. Use the
existing publication/installation workflow and tasks; create no new task. Keep
nightly work moving independently. Record publication, installation, loaded code
and natural live exercise separately. Existing legacy workers need one manual
relaunch by Jeremy to enter this architecture.

## Reviewed publication package

The portable helpers, new adapters and tests are published directly. Existing
installed Atlas consumers contain independently reviewed local composition that
differs from common main. Their exact owned changes are carried in
`patches/trader-process-renewal.patch` with raw before/after hashes in
`patches/trader-process-renewal.json`, preserving unrelated source. Review and
apply those hunks to each applicable installation; do not overwrite a peer's
full consumer or local bindings. `tests/check_trader_renewal_package.py` verifies
all direct bytes and patch hunks, distinguishes full reconstruction from partial
hunk projection, and runs portable offline controller/installer tests. Integration
tests explicitly skip on a public base without the adapted APIs; that skip is
not proof of working integration. Atlas's full installed-adapter suite and exact
complete-original reconstruction are recorded separately with the local receipt.
