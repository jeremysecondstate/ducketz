"""Write only this directory's audit summaries from read-only observations."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

git = subprocess.run(['git', 'status', '--porcelain=v1', '-z'], cwd=REPO,
                     check=True, capture_output=True).stdout.decode().split('\0')
entries = []
for entry in git:
    if not entry:
        continue
    relative = entry[3:]
    path = REPO / relative
    entries.append({'status': entry[:2], 'path': relative,
                    'sha256': sha(path) if path.is_file() else None})
audit = {'reviewed_at': datetime.now(timezone.utc).isoformat(),
         'scope': 'READ_ONLY_GIT_STATUS_AND_FILE_HASHES', 'entries': entries,
         'tracked_changes': sum(e['status'] != '??' for e in entries),
         'untracked_entries': sum(e['status'] == '??' for e in entries),
         'instruction': 'Preserve all concurrent Hyperliquid work and unrelated artifacts.'}
(OUT/'concurrent-changes.json').write_text(json.dumps(audit, indent=2)+'\n', encoding='utf-8')

local = read(OUT/'local-preflight.json')
controls = read(OUT/'controls-and-schedules.json')
tasks = read(OUT/'windows-schedules.json')
summary = f"""# September 29 UTC overnight preflight

Read-only evidence was captured at {local['reviewed_at']} for the September 28 completed source and September 29 action session. The audit made no provider/broker calls, production/control writes, supervision operations, trader starts or order actions. Its only writes are audit artifacts in this directory.

The production watchlist contains eleven symbols: {', '.join(local['symbols'])}. Expected current output is 264 forecasts, 264 intents and 33 production OPRA cursors. The CROX/PATH/TWST/IONQ batch is ACTIVE, exact membership agrees with production, and saved Gameplan, trade-plan and overnight receipt hashes verify. No unfinished registered batch exists; no onboarding work was repeated.

The September 28 manual Gameplan trader is terminal FINISHED, with heartbeat at 2026-09-29T00:00:00.000474Z, and no matching trader process. Its last cycle is NO_ORDERS_SUBMITTED, with CURRENT broker capture and verified manifest/decisions hashes. The session reports 52 submitted orders and two failed cycles historically; the latter are not diagnosed by this preflight and do not change the saved final status. Overnight work has no order authority.

Native read-only ownership validation reports REVIEW_REQUIRED / safe_for_planning=false because three WORKING SELL reservations remain: CROX 1h 34 shares, PATH 1h 7 shares and TWST 4h 21 shares, each with zero filled shares and last evidence at 2026-09-28T23:59:54.929833Z. There are no persistent blocks. The latest saved reconciliation was ready at 2026-09-28T23:59:54.930525Z; readiness then does not eliminate these pending reservations or prove present broker order state. The 500 reservations comprise 475 FILLED, 20 CANCELLED, two REJECTED and three WORKING.

Before planning, the root supervisor must use the documented native post-close reconciliation path under its own valid supervision claim, with terminal/absent trader checks, native session then cycle locks, ledger backup, exact matching-account terminal order history and a newer coherent portfolio snapshot. Preserve filled allocations and entry claims. Absence from open orders alone cannot prove cancellation; unknown or still-working evidence remains unresolved. This preflight changed no reservation or ownership data.

Both stock operator controls remain TRUE, and hashes of controls, launchers, environment and 51 entry/recovery claim files were captured. The native Windows Ducketz Independent Stock Session task is Disabled; its retained weekday trigger remains 03:55. Codex schedules show Overnight Gameplan ACTIVE at 21:05, Operations Watch ACTIVE every 90 minutes, daytime supervision ACTIVE at 03:55 weekdays, weekly review ACTIVE and options paper tracking PAUSED. All settings were preserved.

Saved latest CME BBO captures still match continuous ES.v.0/NQ.v.0/CL.v.0/GC.v.0 to literal ESZ6/NQZ6/CLX6/GCZ6 instrument IDs at September 25 20:59 UTC. GCZ6 definition/raw/normalized checksums verify and the saved expiration remains December 29. These observations indicate no literal-scope repair; tonight's provider freshness remains a native-stage check.

Git status has {audit['tracked_changes']} modified tracked files, all concurrent Hyperliquid source/configuration/docs/tests work, plus {audit['untracked_entries']} untracked entries including ongoing Hyperliquid work and audit directories. None was changed by this preflight. File paths and current hashes are preserved in concurrent-changes.json.

Evidence: local-preflight.json, controls-and-schedules.json, windows-schedules.json and concurrent-changes.json, with reusable read-only capture scripts. The ownership reservation reconciliation is the sole local planning blocker found here; provider coverage and fresh planning cash/holdings remain native-stage checks.
"""
(OUT/'local-preflight.md').write_text(summary, encoding='utf-8')
print(json.dumps({'status': local['status'], 'summary': str(OUT/'local-preflight.md'),
                  'tracked_changes': audit['tracked_changes'], 'untracked_entries': audit['untracked_entries']}))
