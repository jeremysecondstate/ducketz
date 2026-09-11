"""Record the verified completed run after supervision is explicitly released."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

root = Path('C:/DATASTORE')
run = root/'ml/overnight-runs/20260911T061454.689692Z'
trade = root/'ml/gameplan-trade-plan-runs/20260911T061456.678966Z'
actuals = root/'ml/gameplan-actuals-review-runs/20260911T061544.303359Z'
audit = json.loads((run/'operator-final-verification.json').read_text(encoding='utf-8'))
assert not audit['errors'] and audit['status'].startswith('VERIFIED')
receipt = json.loads((run/'receipt.json').read_text())
assert receipt['status'] == 'COMPLETE' and receipt['orders_placed'] == 0
release = json.loads((run/'operator-supervision-release.json').read_text())
assert release['status'] == 'RELEASED' and release['owner_token'] == '5e169e2e-25a3-4c6b-86f9-80a5bc172ac1'
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
stamp = datetime.now(timezone.utc).isoformat()
summary = f'''## Bounded COST carry-forward implemented; overnight complete — {stamp}

The user explicitly authorized bounded forward fill for rare possible no-trade gaps. This supersedes the earlier dual-successful-Databento-API prerequisite for this planning-only policy; do not re-block the completed COST plan on denied XNAS Live or require another approval. Production now uses cash-aware-gameplan-trade-planning-v4, price-band/path v2, and bounded-planning-reference-completion-v1. Legacy calls remain strict v1 by default.

Only the current action date's exact prior XNYS session17:00Pacific reference can be completed when actual close age is >5 and <=15minutes and a verified native source partition fully covers through the completed boundary. UTC date-only native archive bounds are supported. New1minuteOHLC rows equal the last actual close, assumedvolume0, is_synthetic=true, ASSUMED_NO_TRADES. Missing candles do not prove no trades. Original observed_at and separate effective_at, native source identity, originating bar and verified partition are preserved. No cross-session/cross-source borrow, overwritten native minute, future reference, invalid data, interrupted-source fill, or >15minute fill. Observed historical samples, training, actuals and livequote/order gates remain unchanged; independent quote freshness uses the effective boundary for synthetic anchors.

Implemented ml/gameplan_price_completion.py; integrated price_bands/trade_planning/trade_review and updated NIGHTLY_GAMEPLAN/INDEPENDENT_STOCK_HORIZONS docs. 184 focused tests passed across completion, bands, planning, rendering, cash ledger and actuals. Offline exact pinned COST reproduction passed98availableplanningpoints/168rows using saved snapshot. No upstream model/forecast retraining. Original forecasts receipt remains950809362b98ad46853cd5c715829d2f49d39e4eb045986fe7b729c0f5ae6bb9.

Native recovery verification passed, then resume{run.name} completed planning+actuals at06:15:49UTC. Six upstream stages from20260911T040707.492977Z retained; originalSep11 11:00UTC/04:00Pacificdeadline preserved. Trade plan{trade.name} COMPLETE: fresh read-only snapshot06:15:03.854620UTC, cash105541.16, 168rows,98availablepoints. COST alone uses13syntheticminutes23:47..23:59UTC at902.20, originating actual23:46bar/completion23:47(16:47Pacific), effective00:00UTC(17:00Pacific). Both planning-reference-completion.json and synthetic-reference-bars.parquet are checksummed manifest outputs; readable Gameplan explicitly discloses the assumption. Direction ledger verifies3conditional sales,0buys, endingcash115760.65–115802.00/base115781.41; these are scenario results, not broker fills. Trade receipt SHA256 {sha(trade/'receipt.json')}.

Actuals{actuals.name} COMPLETE forSep10 against original preopenGameplan andtradeplan:121evaluated,5matureawaitingrealdata,42future;96of98hourlypricescompared,2awaitingdata. Synthetic planning rows never become actual outcomes. Current+dated pointers and successor links verified. Actuals receipt SHA256 {sha(actuals/'receipt.json')}.

Full offline operator audit {audit['status']}, errors[], all original ancestry/source/model/OPRA/cash/share/price/actuals checks retained plus exact synthetic JSON/Parquet/native-lineage/history preservation. First audit flagged serialization-only JSONkey/Parquetcolumn ordering; verifier corrected to require identical column sets and exact values independent of order, then rerun. No production artifact changed for that correction. Evidence at{run.as_posix()}/operator-final-verification.json and C:/dev/ducketz/artifacts/analysis/cost-closing-gap-20260911.

Updated only existing loops-hourly-operations automation prompt via native automation_update; verified only prompt/updated_at changed, preserving schedule/status/model/effort/notifications/project/cwds. New policy requires no extra click, duplicate schedule or provider re-query. Do not rerun completed sourceSep10 preparation. No trader launched, controls/real ownership changed, order submitted, license purchased or providerdata modified. Orders0. Delegated monitor stopped with no in-flight/future renewal/helper; claim5e169e2e-25a3-4c6b-86f9-80a5bc172ac1 RELEASED at{release['updated_at']}. Final run time{stamp}.

'''
memory = Path('C:/Users/7980X/.codex/automations/loops-hourly-operations/memory.md')
header = '# Loops hourly operations memory\n\n'
old = memory.read_text(encoding='utf-8')
assert old.startswith(header)
memory.write_text(header+summary+old[len(header):], encoding='utf-8')
(run/'operator-notes.md').write_text(summary, encoding='utf-8')
original = root/'ml/overnight-runs/20260911T040707.492977Z/operator-notes.md'
with original.open('a',encoding='utf-8') as stream:
    stream.write('\n\n'+summary)
print('Recorded verified completion at '+stamp)
