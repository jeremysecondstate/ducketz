"""Offline documentation/link/archive checks; no application imports or network calls."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
from urllib.parse import unquote

REVIEWED = ['docs/loops-system-analysis/CROSS_HORIZON_BEARISH_FALLBACK.md', 'docs/loops-system-analysis/CURRENT_SYSTEM.md', 'docs/loops-system-analysis/DATASTORE_HYGIENE.md', 'docs/loops-system-analysis/HOURLY_AUTOMATION.md', 'docs/loops-system-analysis/HOURLY_TRADER_LOOP_C_HANDOFF_PROMPT.md', 'docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md', 'docs/loops-system-analysis/JOINT_ACCOUNT_CAPITAL.md', 'docs/loops-system-analysis/LOOPS_SYSTEM_MIND_MAP.md', 'docs/loops-system-analysis/LOOP_C_RISK_CALCULUS.md', 'docs/loops-system-analysis/LOOP_C_ROLLOUT_PLAN.md', 'docs/loops-system-analysis/LOOP_INVENTORY.md', 'docs/loops-system-analysis/LOOP_MAP.md', 'docs/loops-system-analysis/LOOP_RELATIONSHIPS.md', 'docs/loops-system-analysis/MONITORING.md', 'docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md', 'docs/loops-system-analysis/OPRA_HISTORY_MAINTENANCE_AUTOMATION.md', 'docs/loops-system-analysis/OPTIONS_STRATEGY_ML_UPGRADE.md', 'docs/loops-system-analysis/OPTIONS_STRATEGY_PAPER_AUTOMATION.md', 'docs/loops-system-analysis/POOLED_SEQUENCE_LOOP_C.md', 'docs/loops-system-analysis/PREDICTION_CONTRIBUTION_MATRIX.md', 'docs/loops-system-analysis/README.md', 'docs/loops-system-analysis/RELEVANCE_REVIEW.md', 'docs/loops-system-analysis/RESEARCH_SYMBOL_ONBOARDING.md', 'docs/loops-system-analysis/STOCK_TRADER_AUTOMATION.md', 'docs/loops-system-analysis/STOCK_TRADER_RUNTIME.md', 'docs/loops-system-analysis/SYMBOL_ONBOARDING.md', 'docs/loops-system-analysis/SYSTEM_FUNCTIONALITY.md', 'docs/loops-system-analysis/WEEKLY_REVIEW_AUTOMATION.md', 'docs/loops-system-analysis/loops/active-pricing-loop-3.md', 'docs/loops-system-analysis/loops/cme-l2-runtime.md', 'docs/loops-system-analysis/loops/daily-alfred-runtime.md', 'docs/loops-system-analysis/loops/directional-loop-b.md', 'docs/loops-system-analysis/loops/loop-a.md', 'docs/loops-system-analysis/loops/options-capture-loop-4.md', 'docs/loops-system-analysis/loops/strategy-runtime.md']
ORIGINAL = ['CROSS_HORIZON_BEARISH_FALLBACK.md', 'DATASTORE_HYGIENE.md', 'HOURLY_AUTOMATION.md', 'HOURLY_TRADER_LOOP_C_HANDOFF_PROMPT.md', 'INDEPENDENT_STOCK_HORIZONS.md', 'INTRADAY_15M_SYSTEM_DESIGN.md', 'JOINT_ACCOUNT_CAPITAL.md', 'LOOPS_SYSTEM_MIND_MAP.md', 'LOOP_C_RISK_CALCULUS.md', 'LOOP_C_ROLLOUT_PLAN.md', 'LOOP_INVENTORY.md', 'LOOP_MAP.md', 'LOOP_RELATIONSHIPS.md', 'MONITORING.md', 'NIGHTLY_GAMEPLAN.md', 'OPRA_HISTORY_MAINTENANCE_AUTOMATION.md', 'OPTIONS_STRATEGY_ML_UPGRADE.md', 'OPTIONS_STRATEGY_PAPER_AUTOMATION.md', 'POOLED_SEQUENCE_LOOP_C.md', 'PREDICTION_CONTRIBUTION_MATRIX.md', 'README.md', 'RESEARCH_SYMBOL_ONBOARDING.md', 'SHARED_ACCOUNT_GAMEPLAN.md', 'STOCK_TRADER_AUTOMATION.md', 'STOCK_TRADER_RUNTIME.md', 'SYMBOL_ONBOARDING.md', 'SYMBOL_ONBOARDING_PROPOSAL.md', 'SYSTEM_FUNCTIONALITY.md', 'WEEKLY_REVIEW_AUTOMATION.md', 'audits/2026-09-04/AUDIT.md', 'audits/2026-09-04/FOUR_HOUR_CALIBRATION.md', 'audits/2026-09-04/SUPERVISION_UPDATE.md', 'loops/active-pricing-loop-3.md', 'loops/cme-inline-history.md', 'loops/cme-l2-runtime.md', 'loops/daily-alfred-runtime.md', 'loops/directional-loop-b.md', 'loops/loop-a.md', 'loops/options-capture-loop-4.md', 'loops/strategy-runtime.md']

def outside_fences(text):
    lines=[]
    fence=None
    for line in text.splitlines():
        match=re.match(r"^\s*(`{3,}|~{3,})",line)
        if match:
            token=match.group(1)
            if fence is None: fence=token
            elif token[0]==fence[0] and len(token)>=len(fence): fence=None
            continue
        if fence is None: lines.append(line)
    return "\n".join(lines), fence


def anchors(text):
    result=set();seen={}
    content,_=outside_fences(text)
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$",content,re.M):
        title=re.sub(r"\[([^]]+)\]\([^)]+\)",r"\1",title)
        title=title.replace("`", "").lower()
        slug="".join(c for c in title if c.isalnum() or c in " _-")
        slug=slug.replace(" ","-")
        count=seen.get(slug,0);seen[slug]=count+1
        result.add(slug if count==0 else slug+"-"+str(count))
    result.update(re.findall(r'<(?:a|span)\s+id=[\'"]([^\'"]+)',text))
    return result


def main():
    root=Path.cwd().resolve();errors=[];count=0
    for name in REVIEWED:
        path=root/name
        raw=path.read_bytes();text=raw.decode("utf-8")
        if b"\r" in raw or text.startswith("\ufeff") or not text.endswith("\n"):
            errors.append(name+": expected UTF-8 without BOM, LF and final newline")
        if any(line.rstrip()!=line or "\t" in line for line in text.splitlines()):
            errors.append(name+": trailing whitespace or tab")
        content,fence=outside_fences(text)
        if fence: errors.append(name+": unclosed fence")
        if re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]",text): errors.append(name+": private absolute Windows path")
        if re.search(r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])",text,re.I):
            errors.append(name+": raw UUID needs privacy review")
        for target in re.findall(r"\[[^]\n]*\]\(([^)]+)\)",content):
            target=target.strip("<>")
            if re.match(r"^(https?://|mailto:)",target): continue
            local,_,fragment=target.partition("#")
            resolved=(path.parent/unquote(local)).resolve() if local else path
            if not resolved.is_relative_to(root) or not resolved.exists():
                errors.append(name+": missing/escaping link "+target);continue
            if fragment and resolved.suffix.lower()==".md":
                if unquote(fragment) not in anchors(resolved.read_text(encoding="utf-8-sig")):
                    errors.append(name+": missing anchor "+target)
            count+=1
    review=(root/"docs/loops-system-analysis/RELEVANCE_REVIEW.md").read_text(encoding="utf-8")
    table=re.findall(r"^\| \[([^]]+\.md)\]\([^)]+\) \| (?:KEEP CURRENT|UPDATE|MIXED|HISTORICAL REFERENCE|ARCHIVE|UNRESOLVED)",review,re.M)
    if sorted(table)!=ORIGINAL: errors.append("Disposition table does not cover each original exactly once")
    recovery=json.loads((root/"docs/loops-system-analysis/ARCHIVE_RECOVERY.json").read_text())
    for entry in recovery["entries"]:
        blob=subprocess.check_output(["git","cat-file","blob",entry["commit"]+":"+entry["path"]],cwd=root)
        assert hashlib.sha256(blob).hexdigest()==entry["git_sha256"]
        assert subprocess.check_output(["git","rev-parse",entry["commit"]+":"+entry["path"]],cwd=root,text=True).strip()==entry["blob"]
        endings=set(entry["crlf_lines"])
        rebuilt=b"".join(line[:-1]+b"\r\n" if i in endings and line.endswith(b"\n") else line for i,line in enumerate(blob.replace(b"\r\n",b"\n").splitlines(keepends=True),1))
        assert hashlib.sha256(rebuilt).hexdigest()==entry["original_sha256"]
        assert (root/entry["replacement"]).is_file()
    if errors: raise SystemExit("\n".join(errors))
    print(f"PASS: {len(REVIEWED)} refreshed Markdown files; {count} local links/anchors; all {len(ORIGINAL)} original dispositions; exact archive reconstruction; portable/private-identifier checks")

if __name__=="__main__":
    main()
