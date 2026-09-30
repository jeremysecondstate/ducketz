"""Watch native Loop A logs locally through completion; never runs providers."""
from pathlib import Path
import json
import time
import sys

sys.dont_write_bytecode = True
import review_current

previous = set()
while True:
    review_current.main()
    latest = json.loads((review_current.OUT/'latest.json').read_text())
    now = {(row['line'],row['text']) for row in latest['warnings']}
    new = sorted(now-previous)
    if new:
        print(json.dumps({'new_warning_lines':[{'line':line,'text':text} for line,text in new]}), flush=True)
    previous = now
    if latest['native_status'] != 'RUNNING' or latest['native_stage'] != 'loop_a_close_fetch':
        print(json.dumps({'watch_complete':True,'native_status':latest['native_status'],
                          'native_stage':latest['native_stage']}), flush=True)
        break
    if (review_current.OUT/'STOP').exists():
        print(json.dumps({'watch_stopped_by_supervisor':True}), flush=True)
        break
    time.sleep(45)
