"""Adapt bounded saved-report inspection for the authorized active publication."""
from pathlib import Path
import ast

OUT = Path(__file__).resolve().parent
source = OUT.parent.parent/'overnight-20260925/verification/preliminary_directional_review.py'
value = source.read_text(encoding='utf-8')
value = value.replace('20260925T040822.112032Z', '20260926T040723.834511Z')
value = value.replace('2026-09-25', '2026-09-28').replace('September 25', 'September 28')
value = value.replace('    sys.path.insert(0, str(REPO))',
    '    from audit_environment import verify_environment\n    implementation = verify_environment()\n    sys.path.insert(0, str(REPO))')
value = value.replace('        "gameplan_run": str(run), "action_date": ACTION,',
    '        "implementation": implementation, "gameplan_run": str(run), "action_date": ACTION,')
old = '"TWST fitted support: hourly 10,895 total, with only 1 row on route 1h@16:00; four-hour 1,982 total, minimum 24 on 4h@16:00; daily 10 total, 2 per route, with 10 assessment rows total; weekly 8 fitted and 2 assessment rows. These are observed saved counts, not a new qualification rule."'
new = '"Per-symbol/route fitted and assessment counts are retained exactly in the JSON, including thin TWST histories. These are observed saved counts, not a new qualification rule."'
assert old in value
value = value.replace(old, new)
old = '". The other promoted horizons pass the saved tolerances without beating both baselines."'
new = '". Other groups keep their actual saved qualification status and metric margins."'
assert old in value
value = value.replace(old, new)
ast.parse(value)
(OUT/'preliminary_directional_review.py').write_text(value, encoding='utf-8')
print('PREPARED_BOUNDED_REPORT_ONLY_REVIEW')
