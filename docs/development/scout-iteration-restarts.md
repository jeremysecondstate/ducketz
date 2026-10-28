# Scout iteration-boundary restarts

## Independent 1B model and trading replacement

Jeremy's October 28 clarification requires both worker roles to reload after
every full eleven-symbol iteration. The earlier independent controller replaced
only its model worker and left trading pinned for the entire logical run; that
does not satisfy the requirement.

The controller records each verified full model-attempt boundary in an ordered
queue. It issues a request fenced to the current trading generation, then
replaces the model independently. Trading completes its current eligible effects
or retains their durable retries, drains, closes its journal, writes a trading
checkpoint, and exits with code 75. After validating that checkpoint and proving
the entire old process tree exited, the controller validates and loads a complete
selected release into the next trading generation. This occurs even when the
release selection is unchanged. Boundaries arriving during a drain remain queued.

A full attempt may retain genuine symbol failures or data deferrals. Subset
retries, polling ticks, and order-recovery passes are not full-iteration boundaries.
Trading checkpoints preserve issued forecast identities, pending work, retry
floors, and the existing journal while permitting concurrent model appends and
normal retirement of expired or superseded signals. The same logical session,
original day/deadline, recipe, stop latch, reservations and known-order recovery
remain authoritative. Independent 1A data work is not restarted by this lifecycle.

The release manifest pins `trading_rotation_version=scout-trading-rotation-v1`.
The updated controller rejects releases without that capability. The enclosing
independent service protocol remains compatible with 1A. Installing this
controller requires one authorized initial activation; subsequent compatible
application fixes load through automatic iteration replacement. A terminal
startup, drain, checkpoint or controller failure remains explicit and cannot
authorize an unrestricted crash respawner.

Jeremy's October 26, 2026 direct instruction authorizes planned worker
replacement after every completed Scout eleven-symbol 15-minute iteration
within one authorized logical run. The existing cured launcher starts that
run, manually or through the existing Windows task. The operator retains
initial activation and stop authority. No new schedule, next-day rollover,
Atlas restart, Gameplan dependency, or crash respawner is introduced.

The controller holds both existing launcher and manual-session locks for the
whole run, including gaps between generations. Each child starts suspended
inside a Windows kill-on-close Job Object, assigned atomically by CreateProcess.
It resumes only after the controller records its generation and rechecks stop
authority. The worker checks the controller PID/birth, generation token, exact
job membership, manual-stop latch and original deadline at existing execution
gates. Controller death or console closure terminates the owned process tree.

An iteration is a returned full-scope `ShadowRunner.run_cycle` attempt. Every
configured symbol must have its immutable forecast or an existing durable
failure/deferral record. Partial populations keep `partial_failure`; the restart
mechanism never changes a missing result to a successful forecast. A polling
tick, a three-second order recovery pass or a single symbol is not a boundary.
An exact full-scope recovery attempt is also a boundary, even when every symbol
retains a durable input deferral. A subset retry or reuse of a completed cycle
is not a full iteration. The checkpoint preserves the scheduler next turn:
a fresh attempt gives one due recovery pass priority; a full recovery attempt
lets a newer due quarter run first. With no new quarter, existing due retries
remain eligible. The saved Boolean is checked against the durable measured
attempt count before broker construction; historical first-attempt checkpoints
retain their original recovery-first meaning.

The forecast thread stops after that attempt, due execution receives its normal
disposition or durable retry, and all owned data/context/scoring threads drain.
Only then is a checkpoint written and exit code 75 (`PLANNED_RESTART`) returned.
The controller proves the whole process tree exited before another generation.

Windows sharing violations during authority/status I/O have a one-second
bounded retry. No effect is enabled while evidence is unavailable. Stop and
deadline authority are rechecked after I/O and on failure. Once a generation
observes a real stop, mismatched fence or exhausted read failure, its revocation
is permanent; a later apparently healthy read cannot restore authority. The
worker retains the exact failure category and message in its result receipt.

Cycles, forecasts, queue deadlines and Scout order journals remain in their
existing stores. Generations retain the logical session, original authorization,
selected calendar day and 17:00 Pacific deadline. The successor validates the
checkpoint against those stores before constructing the broker, resumes the
same Scout journal, and obtains fresh broker capacity and quotes through the
existing executor. Working orders, fills, reservations and uncertain sends are
neither canceled nor reinterpreted as unsent by rotation. Recovery still uses
their durable IDs. The next eligible quarter and existing expiry/supersession
rules determine work; rotating cannot replay completed or expired forecasts.

`tools/scout_release.py` resolves an atomic local selector before every
generation, even when unchanged. It pins the complete manifest, verifies every
source hash, rejects extra importable source/bytecode, and checks the private
recipe hash. The fresh process revalidates the exact selected release before
importing application modules. A pointer change during validation, invalid
manifest/recipe, incompatible checkpoint, failed startup or failed drain stops
the logical run. There is no automatic fallback and no automatic failure retry.
Preserve the exact error, old release and pending stores. An installer may select
a reviewed compatible predecessor explicitly; resuming a failed run still needs
a new authorized initial launch. Default startup and drain limits are 300 and
120 seconds respectively; forced cleanup never certifies a planned boundary.

Install a new immutable release beside its predecessors, preserving the local
recipe and runtime overlays by reviewed field/hunk composition. Record final
source hashes and passing offline tests, then use `select_release` as the atomic
commit point under installation ownership. Never edit an installed source tree
or repoint the cured launcher to a single application release. The selector is
local private binding; source queue publication remains separate. A private
recipe copy pinned within its release prevents later installations from
mutating an active generation's recipe.

The small controller/bootstrap itself is pinned for the logical run. A
controller protocol change must use a new bootstrap and a new normal authorized
launch; an incompatible release fails closed. Ordinary startup-cached trader
source, configuration and compatible saved recipe changes load through the next
generation. Preserve Scout's symbols, frozen model contracts, hyperparameters,
calibration, fitted artifacts and data requirements. Saved unrounded probability
direction stays BUY at p >= 0.5001, SELL at p <= 0.4999, HOLD strictly between;
historical labels and policies remain unchanged.

Ctrl-C in the controller, closing it, or the controller's `--stop --datastore`
command ends the run. The stop command writes a run-specific durable latch; no
replacement clears it. A new separately authorized activation creates a new
identity. Unattended replacements have NUL input, per-generation logs and no new
console or interactive pause. The existing Windows task identity, initial
trigger, principal, logging, working directory and settings are preserved.

The Representative reports routine installed repairs as awaiting the next safe
automatic reload, then checks the generation's actual loaded manifest and the
affected behavior. Installation, loading and confirmed behavior are separate.
The old already-running process cannot acquire this controller from edited
files: the next normal authorized cured-launcher start activates it. During
implementation, use fixture brokers/stores and fake worker adapters; do not
start, stop, restart or adopt the existing live trader to verify the repair.
