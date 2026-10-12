# Scout: install the existing 15m build from exact Git source

Use the already built common system at
`53aea68a29ebb136faa4378e89261109100652e3` (PR #78). Do not rebuild forecasting,
model adapters, sizing, scoring or UI from design prose. Keep Scout shadow-only.
This handoff adds an installation manifest and instructions; it changes no trader
implementation, operating binding, schedule or live authority.

## One coherent source

Repository: [jeremysecondstate/ducketz](https://github.com/jeremysecondstate/ducketz).
The exact tree is `08d3eca0c92ee29bf59e7fc0c40df6852a56efe7`.
Remote heads and direct parents were reverified on October 12, 2026:

| PR | Exact commit | Existing Completion-Record |
| --- | --- | --- |
| [#78](https://github.com/jeremysecondstate/ducketz/pull/78) | `53aea68a29ebb136faa4378e89261109100652e3` | `20261012T185820Z-3e62123b644c4b839dc6f127e1c1a3a5` |
| [#76](https://github.com/jeremysecondstate/ducketz/pull/76) | `878fa8b91bc7811aa3f3e01c08f5c9e8f1c02189` | `20261012T174021Z-ec7f5124acb34eef807fbca5cb7245c4` |
| [#75](https://github.com/jeremysecondstate/ducketz/pull/75) | `0a51b8c2bf81ad8cbdf1863ec5ba10c44567db9a` | `20261012T171451Z-161b15ff6e1e49cfa7c07cae0b709bb2` |
| [#74](https://github.com/jeremysecondstate/ducketz/pull/74) | `233c46b8254191b0cce0580385f158f77e155f98` | `20261012T160403Z-45b006781e4a4c3da7c94b3c4ecc7b51` |
| [#70](https://github.com/jeremysecondstate/ducketz/pull/70) | `95a57dccdcae9f178446c9f570a72aa9415dd21d` | `20261012T115221Z-05f6f682546f46bcb3457651239e0da2` |

The parent chain is **#78 -> #76 -> #75 -> #74 -> #70**. #74 composes the
earlier intraday implementation through #73, including Scout #69, on the
shared-account #70 base. Those earlier intraday PRs are content provenance,
not additional cherry-picks required on top of #78. Reuse these identities;
do not republish an identical implementation under another completion.

[The exact installation manifest](intraday-scout-exact-installation.json)
lists every required path, raw SHA-256, Git blob ID, byte length and file mode.
It starts with all **360** fingerprints in the sealed #78 record, including
the union of #74/#75/#76/#78 ownership, and adds existing source needed by
static imports, the portable Gameplan launcher/helper and a linked reference.
Every application byte comes from the same #78 tree. No Atlas operating files
are included. This is a required dependency closure, not a whole-checkout copy.

It includes `ml/intraday`, shared `ml/account_gameplan` authority/execution,
native horizon broker/ledger, Gameplan runtime/session/reporting, cancellation
fences, `app/ui` integration, tests, documentation, manual launchers,
`pyproject.toml`, `requirements.txt` and `requirements-ml-runtime.lock`.
The project requires Python 3.13. Compare Scout's existing environment to the
dependency specifications; reuse it when compatible. The separate PyTorch
sequence overlay is not required for the ten-model intraday system. Never
transfer a virtual environment or fitted model as source-installation proof.

## Scout evidence and overlap decisions

[Scout's latest peer-reported installation receipt](https://github.com/jeremysecondstate/atlas-scout/issues/18#issuecomment-6123382722)
was updated at 12:22:12 UTC on October 12. It identifies
`67c4da0f8e0c119fc4fd8892970c279a173145e0` (#69), Completion-Record
`20261012T113948Z-8f3449b0966a466fb48507193607e811`, installed research source
and 187 passing local checks. It describes manual shadow research and no live
15m orders. No later Scout intraday installation/load receipt was found in the
reviewed GitHub coordination. This is historical peer evidence, not a claim
about Scout's current disk bytes or running process.

All ten #69 changed paths are present in #78. Preserve Scout's profile/watchlist
research fallback without account activation, native-verified local XNAS replay,
separate source cohorts, dataset filters, newest-cycle selection and legacy
telemetry compatibility. #78 extends these paths with later reviewed behavior;
byte differences alone are not evidence that Scout's changes were lost.

The manifest's `pr69_sha256` and `reference_operation_vs_pr69` are comparison
aids only. Capture actual Scout bytes and owners first. Matching #78 paths need
no write. For each different path, compare actual Scout bytes, its reviewed
base and the #78 blob. Preserve later Scout improvements; resolve overlaps
explicitly with the existing owner. A changed merged result needs its own exact
hash map and fresh focused checks, not an assertion of #78 byte equality.
Never bulk-stage or replace unrelated work.

**Exclude PR #77** (`22377391edc2d6b6fc840211306ee74d7285417e`). It is a sibling
on #76, owned by Atlas TRADER REP, Completion-Record
`20261012T182724Z-7e09b55744a24be8a54158ebcf2ffa38`. Its pending-BUY valuation
change touches `fixed_horizon_engine.py`, `state.py` and two tests. It is
semantically related to quote readiness but is not in #78. The
[post-#78 disposition](https://github.com/jeremysecondstate/atlas-scout/issues/1#issuecomment-6130844890)
and [later runtime observation](https://github.com/jeremysecondstate/atlas-scout/issues/1#issuecomment-6131787308)
report it uninstalled. Preserve its owner and identity for separate compatibility
review; do not recreate it, silently apply it or claim it loaded.

## Installation procedure

1. Follow current AGENTS.md. Verify Scout's installed `active.json` manifest and
   every pinned file; use that release's contract and Scout's own local profile.
   The October 9 standing grant permits reviewed peer adoption without another
   approval. An unmerged draft PR is not itself an installation blocker.
2. Record actual Scout HEAD/index, dirty paths, active writers, eleven-symbol
   membership, local bindings and private before hashes. Use the supported
   isolated source cache and managed worktree at exact #78. Fetch using existing
   native Git authentication and locks. Never pull, reset, stash or switch the
   running checkout. Verify the advertised commit/tree and original blobs.
3. Compare the manifest to the isolated tree and Scout installation. Record an
   explicit local add/modify plan with expected-before and target-after hashes.
   Carry unchanged dependencies as verification requirements, not forced writes.
   No deletion is required by this manifest. Resolve overlap before installation.
4. Review mixed configuration by field. Common strategy defaults remain shared;
   Scout keeps its actual symbols, account/profile identity, DATASTORE/watchlist
   paths, credentials, active Gameplans and enabled/schedule settings. The
   readiness launcher has a DATASTORE default; prefer explicit Scout-local
   diagnostic arguments or a reviewed local override. Preserve manual controls.
5. Run the focused offline checks below against exact candidate bytes. Install
   only reviewed operations using original blob bytes and before-hash guards,
   then read back all required installed paths and rerun those focused checks.
   Preserve an active writer's files: where installation requires a worker fence,
   retain the complete verified candidate and name that technical condition.
   Do not stop/restart workers to manufacture a successful installation receipt.
6. Retain the resulting exact source/dependency map, installed checks and local
   preservation evidence privately. For newly merged shared changes, use Scout's
   installed queue/courier with a new completion, original provenance and actual
   passing checks. Otherwise reference the existing source completions; do not
   duplicate the source publication.
7. Return one substantive receipt on issue #18: source/manifest identity, exact
   installed paths or unresolved overlap, actual Scout check results, preserved
   local bindings, loaded process version if independently known, and continued
   shadow-only status. Installation and runtime loading are separate facts.

Credentials, private account/order data, databases/ledgers, raw history, fitted
models, operating originals, native task/chat identities, memory and coordination
receipts stay local. No live launch, schedule change, provider/broker call or
training run is needed to verify this transfer. Do not invoke either trader
launcher. No new task or monitor is required.

## Focused offline verification

The manifest contains `focused_adoption_check_argv_after_python`: four fixture
suites for shared capacity, exact fill/unknown-submission recovery, cancellation
and loaded-code compatibility, plus seven tests for Scout research membership,
archive/provider separation and nonactivation. They use temporary stores and
injected dependencies; they do not fit models or operate traders. Run them with
Scout's verified installed network-disabled `test_runner.py`, binding imports
to the candidate's manifest hashes. Use `PYTHONSAFEPATH=1` and disable pytest
plugin autoload, as in the existing source receipts.

The following **read-only application audit/check driver** can be saved in
Scout's ignored evidence directory and run with its existing Python 3.13. Supply
the checked repository root, this reviewed manifest, a fresh ignored evidence
directory, the verified installed coordination release root and Scout's actual
application checkout, in that order. The last argument lets the verifier reject
accidental imports from the running checkout during isolated candidate checks;
it equals the first argument only for the installed-source check.
It writes only check evidence, refuses mismatched application bytes and launches
only the pinned offline verifier. Use it first for the candidate, then for the
installed source. It does not install anything. If Scout intentionally merges
different shared bytes, make a reviewed local manifest for those bytes first.

```python
import hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

root, manifest_path, evidence, release, original = map(Path, sys.argv[1:])
root, evidence, release = root.resolve(), evidence.resolve(), release.resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
expected = {item["path"]: item["sha256"] for item in manifest["files"]}
def audit():
    return [name for name, digest in expected.items()
            if not (root / name).is_file()
            or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
mismatches = audit()
if mismatches:
    raise SystemExit("Source mismatch; preserve and review: " + ", ".join(mismatches))
evidence.mkdir(parents=True, exist_ok=False)
spec = {"root": str(root), "original_root": str(original.resolve()),
        "fingerprints": expected,
        "argv": [sys.executable, *manifest["focused_adoption_check_argv_after_python"]],
        "result_path": str(evidence / "imports.json")}
spec_path = evidence / "runner.json"
spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
env = os.environ.copy()
env.pop("PYTHONPATH", None)
env.pop("PYTHONOPTIMIZE", None)
env.update(PYTHONSAFEPATH="1", PYTHONDONTWRITEBYTECODE="1",
           PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
started = datetime.now(timezone.utc).isoformat()
result = subprocess.run([sys.executable, "-B",
    str(release / "tools/cross_pc/test_runner.py"), str(spec_path)],
    cwd=root, env=env, capture_output=True)
output = result.stdout + b"\n" + result.stderr
(evidence / "checks.log").write_bytes(output)
if result.returncode != 0:
    raise SystemExit("Offline checks failed; retained checks.log")
if json.loads((evidence / "imports.json").read_text())["violations"]:
    raise SystemExit("Imported source failed verification")
if audit():
    raise SystemExit("Source changed during checks")
receipt = {"started_at_utc": started,
    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    "exit_code": result.returncode,
    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    "output_sha256": hashlib.sha256(output).hexdigest(),
    "verifier_sha256": hashlib.sha256(
        (release / "tools/cross_pc/test_runner.py").read_bytes()).hexdigest()}
(evidence / "receipt.json").write_text(json.dumps(receipt, indent=2))
print(output.decode("utf-8", "replace"))
print("output_sha256=" + hashlib.sha256(output).hexdigest())
```

The manifest preserves historical Atlas isolated evidence: #74 1002 passed,
#75 138, #76 414 and #78 544, with original completion times and output hashes.
Those counts describe those exact earlier checks. They are not proof Scout
passed, a performance benchmark, or a reason to repeat full model fitting.
The publication receipt for this handoff records its fresh focused check result.

## Runtime and the separate Scout live gap

Historical final-disposition files predate human reloads. Later Atlas watch
evidence records #78 loaded by both human-started workers, loaded Gameplan
compatibility at 19:25:30 UTC, all 17 repair hashes verified at 19:31:59 UTC,
and the first recovered full cycle. Later observations through 22:05:12 UTC
continue that evidence. Keep detailed watch/account/order records private.
Historical repairs retain their existing Atlas owner; no new defect is established
by this handoff. These observations establish neither consistently low latency
nor predictive performance, and establish nothing about Scout's loaded runtime.

Scout live submission needs a small **distributed execution/admission extension**,
not another model system. Current #78 deliberately restricts manual execution,
native reservations and cancellation to Atlas. Account admission accepts the
coordinator's `atlas-15m` stream and depends on host-local ledgers, locks and
leases. Keep all those guards during this shadow adoption.

The remaining implementation is:

- An authenticated Atlas admission/reconciliation endpoint that serializes with
  existing Gameplan and Atlas 15m account mutations, using one authority and one
  actual cash/share pool. Scout must never acquire the coordinator lease or write
  Atlas's SQLite databases.
- A durable Scout client journal and local submit adapter with stable forecast,
  request, attempt, quantity/price and reservation identities. Atlas persists a
  fenced single-use submission transition; Scout performs the final manual,
  identity, quote and cutoff checks before its own broker POST. Lost/ambiguous
  responses remain committed, never guessed or resent.
- Exact accepted order IDs, statuses, partial fills and cancellation/terminal
  receipts returned to Atlas with idempotent recovery after crashes, disconnects
  and lease changes. A cancellation acknowledgment or expired lease cannot free
  capacity; pending fills remain charged until newer account evidence covers them.
- Explicit Scout stream ownership in native reservation/retry/fill/cancellation
  paths and every Gameplan exclusion, including direct native cancellation,
  timeout and exit methods that currently exclude only `atlas-15m`. Update loaded
  compatibility fingerprints with that future change. No stream budget, reserved
  allowance, copied/shared SQLite or identity renaming supplies this protocol.

Future offline acceptance cases should cover concurrent Gameplan/Atlas/Scout
admission without double spending, duplicate/changed requests, crash windows
around permit/POST/receipt, unknown sends, late exact acceptance after takeover,
partial-fill/cancellation races, duplicated/out-of-order evidence, stream lot
attribution, both layers of Gameplan fences, and manual/identity/quote/cutoff
rejection. Preserve the shared pool; contention-priority policy remains a separate
explicit decision. Scout stays shadow-only until that extension is implemented,
locally verified and later human live activation occurs.

Reuse the existing monitoring request `scout-intraday-monitoring-20261012`;
[the prior Atlas packet/receipt](https://github.com/jeremysecondstate/atlas-scout/issues/18#issuecomment-6124501809)
still needs Scout's independent receiving receipt in the evidence reviewed here.
Do not duplicate the request or mistake historical replay telemetry for live
execution or distributed account coordination.

## Concise Scout adoption prompt

Adopt the existing common 15m source at exact #78
`53aea68a29ebb136faa4378e89261109100652e3` using the adjacent hashed installation
manifest and your verified installed coordination release. Capture actual Scout
bytes/owners, preserve later Scout improvements and your own eleven symbols,
then verify and install only reviewed operations under standing authorization.
Run the focused network-disabled checks on the exact candidate and installed
bytes. Do not replay #67-#73, rebuild models/UI, include sibling #77, copy private
state, launch trading, restart workers or change schedules. Return one issue #18
receipt separating source availability, local installation, loaded runtime and
the remaining Atlas-hosted admission/Scout-local submission work. Keep Scout
shadow-only and reuse the existing monitoring request/completion identities.
