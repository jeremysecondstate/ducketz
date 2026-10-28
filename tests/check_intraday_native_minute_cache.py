"""Offline exact-hunk and native-minute duration cache checks.

The exact source projection runs the changed validator with actual pandas
clocks. Native evidence import is an explicit stub; complete application and
retained-input integration are separately tested by each installation owner.
"""
from __future__ import annotations
import ast, copy, hashlib, json, re, socket, sys, types
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from pandas._libs.tslibs.timedeltas import Timedelta as NativeTimedelta

BEFORE = '"""Versioned source-observation populations; absence is never a market-wide fact.\n\nNo provider client, file writes, fitting, or runtime state is created here.\nHistorical request ranges describe saved source coverage and explicitly retain\nassumed publication timing. They do not certify complete exchange/SIP activity.\n"""\nfrom __future__ import annotations\n\nimport json\nimport hashlib\nimport math\nfrom pathlib import Path\nfrom typing import Mapping, Sequence\n\nimport numpy as np\nimport pandas as pd\n\nfrom .prediction import ContractError, STEP, instant\n\nPOPULATION_CONTRACT = "intraday-verified-source-observations-v1"\nRECOVERY_POPULATION_CONTRACT = "intraday-multi-provider-recovery-v1"\nNO_TRADE_EVIDENCE_VERSION = "intraday-independent-complete-scope-no-trade-v1"\n# Reuse pandas\' fixed rounding rules without parsing a frequency for every\n# native observation in each source proof.\n_NATIVE_MINUTE = pd.offsets.Minute(1)\n_SOURCE_QUARTER = pd.offsets.Minute(15)\n\n\ndef check_population_contract(value):\n    if value not in {None, POPULATION_CONTRACT, RECOVERY_POPULATION_CONTRACT}:\n        raise ContractError("Unsupported intraday population contract")\n    return value\n\n\ndef _utc(value):\n    return pd.Timestamp(instant(value))\n\n\ndef independently_verified_no_trade(proof, *, symbol, source_id, source_dataset,\n                                    opened_at, closed_at, available_at) -> bool:\n    """Validate an explicit independent complete-market no-trade assertion.\n\n    The current MINI adapters never produce this proof. A provider omission\n    string, advertised dataset range, repeated emptiness, or continuity alone\n    cannot satisfy it. Future adapters must retain the independently sourced\n    complete-scope evidence identified here, not manufacture these fields.\n    """\n    if not isinstance(proof, Mapping):\n        return False\n    try:\n        return (proof.get("schema_version") == NO_TRADE_EVIDENCE_VERSION\n                and proof.get("scope") == "complete_market_trade_activity"\n                and proof.get("coverage_complete") is True\n                and proof.get("confirmed_no_trades") is True\n                and proof.get("symbol") == symbol\n                and proof.get("source_dataset") == source_dataset\n                and isinstance(proof.get("evidence_id"), str) and bool(proof["evidence_id"].strip())\n                and isinstance(proof.get("verifier_source_id"), str)\n                and bool(proof["verifier_source_id"].strip())\n                and proof["verifier_source_id"] != source_id\n                and _utc(proof["interval_open"]) <= _utc(opened_at)\n                and _utc(proof["interval_close"]) >= _utc(closed_at)\n                and _utc(proof["interval_close"]) <= _utc(proof["verified_at"]) <= _utc(available_at))\n    except (KeyError, TypeError, ValueError, ContractError):\n        return False\n\n\ndef response_no_trade_verified(response, *, opened_at=None, closed_at=None):\n    return independently_verified_no_trade(response.no_trade_scope_evidence,\n        symbol=response.symbol, source_id=response.source_id, source_dataset=response.source_dataset,\n        opened_at=opened_at or response.interval_open, closed_at=closed_at or response.interval_close,\n        available_at=response.request_ended_at)\n\n\ndef coverage_ranges(records: Sequence[Mapping]) -> tuple[dict, ...]:\n    """Merge overlapping successful-source ranges, retaining their basis/IDs."""\n    ordered = []\n    for item in records:\n        op, cl = _utc(item["opened_at"]), _utc(item["closed_at"])\n        if cl <= op or not item.get("basis"):\n            raise ContractError("Source coverage requires a positive explicit range and basis")\n        ordered.append((op, cl, dict(item)))\n    ordered.sort(key=lambda item: item[0])\n    merged = []\n    for op, cl, evidence in ordered:\n        if merged and op <= merged[-1][1]:\n            previous = merged[-1]\n            previous[1] = max(cl, previous[1])\n            previous[2].append(evidence)\n        else:\n            merged.append([op, cl, [evidence]])\n    return tuple({"opened_at": op.isoformat(), "closed_at": cl.isoformat(),\n                  "basis": "union_of_recorded_source_request_ranges", "range_count": len(evidence),\n                  "ranges_sha256": hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(",", ":")).encode()).hexdigest()}\n                 for op, cl, evidence in merged)\n\n\ndef clip_source_ranges(records, *, start=None, end=None):\n    """Do not claim complete quarters after native rows were window-filtered."""\n    result = []\n    for row in records:\n        op, cl = _utc(row["opened_at"]), _utc(row["closed_at"])\n        op = max(op, _utc(start)) if start is not None else op\n        cl = min(cl, _utc(end)) if end is not None else cl\n        if op < cl:\n            result.append({**row,"opened_at":op.isoformat(),"closed_at":cl.isoformat()})\n    return result\n\n\ndef verified_source_observation(candle: Mapping) -> bool:\n    """Structural identity and coverage proof for one nonempty source quarter."""\n    try:\n        if candle.get("population_contract") != POPULATION_CONTRACT:\n            return False\n        if any(not isinstance(candle.get(key), (bool, np.bool_)) or bool(candle[key])\n               for key in ("synthetic", "no_trade")):\n            return False\n        prices = [float(candle[key]) for key in ("open", "high", "low", "close", "volume")]\n        op_price, high, low, close, volume = prices\n        if (not all(math.isfinite(v) for v in prices) or min(prices[:4]) <= 0 or volume < 0\n                or high < max(op_price, close, low) or low > min(op_price, close)):\n            return False\n        if candle.get("interval_seconds") != 900 or candle.get("synthetic_constituents") != 0:\n            return False\n        observed, empty = candle["observed_constituents"], candle["source_empty_constituents"]\n        if (isinstance(observed, (bool, np.bool_)) or isinstance(empty, (bool, np.bool_))\n                or not float(observed).is_integer() or not float(empty).is_integer()\n                or not 1 <= observed <= 15 or empty != 15-observed):\n            return False\n        op, av = _utc(candle["opened_at"]), _utc(candle["available_at"])\n        if op != op.floor(_SOURCE_QUARTER) or av < op+STEP:\n            return False\n        evidence = candle["evidence_json"]\n        evidence = evidence if isinstance(evidence, Mapping) else json.loads(evidence)\n        if evidence.get("aggregation") != POPULATION_CONTRACT or evidence.get("source_id") != candle["source_id"] or evidence.get("source_dataset") != candle["source_dataset"]:\n            return False\n        coverage = evidence["source_coverage"]\n        if not coverage.get("basis") or _utc(coverage["opened_at"]) > op or _utc(coverage["closed_at"]) < op+STEP:\n            return False\n        from .data import _require_native_observations\n        _require_native_observations([candle, *evidence["observations"]])\n        native_step = pd.Timedelta(minutes=1)\n        times = [_utc(row["opened_at"]) for row in evidence["observations"]]\n        if len(times) != observed or len(set(times)) != len(times) or times != sorted(times):\n            return False\n        # UTC integer clocks retain exact nanoseconds without repeated scalar\n        # Timestamp arithmetic. Preserve pandas\' original path for dates that\n        # it represents outside its nanosecond range.\n        native_closes = None\n        try:\n            first_ns, end_ns = op.value, (op+STEP).value\n            native_ns = [opened.value for opened in times]\n        except OverflowError:\n            if any(t < op or t+native_step > op+STEP or t != t.floor(_NATIVE_MINUTE) for t in times):\n                return False\n        else:\n            if any(t < first_ns or t+60_000_000_000 > end_ns or t % 60_000_000_000 for t in native_ns):\n                return False\n            native_closes = [t+60_000_000_000 for t in native_ns]\n        if _utc(candle["native_first_opened_at"]) != times[0] or _utc(candle["native_last_opened_at"]) != times[-1]:\n            return False\n        for index, (row, opened) in enumerate(zip(evidence["observations"], times)):\n            if row.get("source_id") != candle["source_id"] or row.get("synthetic") is not False:\n                return False\n            available = _utc(row["available_at"])\n            if available > av:\n                return False\n            if native_closes is None:\n                too_early = available < opened+native_step\n            else:\n                try:\n                    too_early = available.value < native_closes[index]\n                except OverflowError:\n                    too_early = available < opened+native_step\n            if too_early:\n                return False\n            vals = [float(row[key]) for key in ("open","high","low","close","volume")]\n            a,h,l,c,v = vals\n            if not all(math.isfinite(x) for x in vals) or min(vals[:4])<=0 or v<0 or h<max(a,c,l) or l>min(a,c):\n                return False\n        native = evidence["observations"]\n        aggregates = {"open": native[0]["open"], "high": max(row["high"] for row in native),\n                      "low": min(row["low"] for row in native), "close": native[-1]["close"],\n                      "volume": sum(row["volume"] for row in native)}\n        if any(not math.isclose(float(candle[key]), float(value), rel_tol=1e-12, abs_tol=1e-12)\n               for key, value in aggregates.items()):\n            return False\n        return True\n    except (KeyError, TypeError, ValueError, OverflowError, ContractError):\n        return False\n\n'


def main():
    root=Path(__file__).resolve().parents[1]
    stem=root/'docs/development/patches/intraday-native-minute-cache'
    meta=json.loads(stem.with_suffix('.json').read_bytes())
    patch=stem.with_suffix('.patch').read_bytes(); checks=[]; network=[]
    def check(value,label):
        assert value,label
        checks.append(label)
    def deny(*a,**kw):
        network.append(True)
        raise AssertionError('Native minute cache checks prohibit network')
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
    digest=lambda b:hashlib.sha256(b).hexdigest()
    check(digest(Path(__file__).read_bytes())==meta['checker_sha256'],'checker exact bytes')
    check(digest(patch)==meta['patch_sha256'],'patch exact bytes')
    check(digest(BEFORE.encode())==meta['before_projection_sha256'],'before exact projection bytes')
    ptext=patch.decode(); check(ptext.startswith('--- a/'+meta['path']+'\n+++ b/'+meta['path']+'\n'),'single intended patch path')
    hunks=re.split(r'^@@.*?@@[^\n]*\n',ptext,flags=re.M)[1:]
    check(len(hunks)==3,'exact three patch hunks')
    projected=BEFORE
    for number,hunk in enumerate(hunks):
        old=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','-')))
        new=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','+')))
        check(projected.count(old)==1,'unique exact hunk preimage '+str(number))
        projected=projected.replace(old,new)
    check(digest(projected.encode())==meta['after_projection_sha256'],'after exact projection bytes')
    tree=ast.parse(projected); rest=copy.deepcopy(tree)
    removed_import=removed_cache=restored_assignment=0
    for node in list(rest.body):
        if isinstance(node,ast.ImportFrom)and node.module=='pandas._libs.tslibs.timedeltas':
            check([(x.name,x.asname)for x in node.names]==[('Timedelta','_NATIVE_STEP_CONSTRUCTOR')],'canonical native constructor import')
            rest.body.remove(node);removed_import+=1
        if isinstance(node,ast.Assign)and any(isinstance(x,ast.Name)and x.id=='_NATIVE_STEP_CACHED'for x in node.targets):
            check(ast.dump(node.value)==ast.dump(ast.parse('_NATIVE_STEP_CONSTRUCTOR(minutes=1)',mode='eval').body),'fixed canonical minute initialization')
            rest.body.remove(node);removed_cache+=1
        if isinstance(node,ast.FunctionDef)and node.name=='verified_source_observation':
            for assign in ast.walk(node):
                if isinstance(assign,ast.Assign)and any(isinstance(x,ast.Name)and x.id=='native_step'for x in assign.targets):
                    expected=ast.parse('_NATIVE_STEP_CACHED if pd.Timedelta is _NATIVE_STEP_CONSTRUCTOR else pd.Timedelta(minutes=1)',mode='eval').body
                    check(ast.dump(assign.value)==ast.dump(expected),'identity guard plus dynamic constructor fallback')
                    assign.value=ast.parse('pd.Timedelta(minutes=1)',mode='eval').body;restored_assignment+=1
    check((removed_import,removed_cache,restored_assignment)==(1,1,1),'only intended AST additions replaced')
    check(ast.dump(rest)==ast.dump(ast.parse(BEFORE)),'unaffected projection AST unchanged')
    candidate=root/meta['path'];mode='portable exact hunk projection; full application source absent'
    if candidate.exists():
        raw=candidate.read_bytes();text=raw.decode().replace('\r\n','\n');sha=digest(raw)
        if sha==meta['after_sha256']:
            check(text.startswith(projected),'actual exact complete source contains reviewed projection');mode='complete exact installed source plus portable projection'
        elif sha==meta['before_sha256']:
            applied=text
            for hunk in hunks:
                old=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','-')))
                new=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','+')))
                check(applied.count(old)==1,'full exact source hunk applies');applied=applied.replace(old,new)
            check(digest(applied.encode())==meta['after_lf_sha256'],'full normalized source reconstructs after')
            exact=applied.replace('\n','\r\n').encode()if b'\r\n'in raw else applied.encode()
            check(digest(exact)==meta['after_sha256'],'full raw source reconstructs after');mode='complete exact preimage plus portable projection'
        else:mode='portable exact hunk projection; checkout source composition differs'
    package='portable_native_minute_cache'
    class ContractError(ValueError):pass
    def instant(value):
        try:result=value if isinstance(value,datetime)else datetime.fromisoformat(value.replace('Z','+00:00'))
        except (ValueError,TypeError,AttributeError)as exc:raise ContractError('Timezone-aware timestamp required')from exc
        if result.tzinfo is None or result.utcoffset()is None:raise ContractError('Timezone-aware timestamp required')
        return result.astimezone(timezone.utc)
    prediction=types.ModuleType(package+'.prediction');prediction.ContractError=ContractError;prediction.STEP=timedelta(minutes=15);prediction.instant=instant
    data=types.ModuleType(package+'.data');pkg=types.ModuleType(package);pkg.__path__=[]
    previous={key:sys.modules.get(key)for key in(package,package+'.prediction',package+'.data')}
    sys.modules.update({package:pkg,package+'.prediction':prediction,package+'.data':data})
    original_ctor=pd.Timedelta;calls=[];native_calls=[];hook=[None]
    def require_native(rows):
        native_calls.append(len(rows))
        if hook[0]:hook[0]()
    data._require_native_observations=require_native
    def make(source):
        env={'__name__':package+'.projection','__package__':package}
        exec(compile(source,'native-minute-exact-projection','exec'),env)
        return env
    def outcome(env,row):
        try:return ('value',env['verified_source_observation'](row))
        except Exception as exc:return('error',type(exc).__name__,str(exc))
    try:
        old=make(BEFORE);new=make(projected)
        check(type(new['_NATIVE_STEP_CACHED'])is NativeTimedelta,'cache native immutable duration type')
        check(new['_NATIVE_STEP_CACHED'].value==60_000_000_000,'cache exact native minute nanoseconds')
        for attr in('value','_value'):
            try:setattr(new['_NATIVE_STEP_CACHED'],attr,0);immutable=False
            except (AttributeError,TypeError):immutable=True
            check(immutable,'native duration immutable '+attr)
        at='2024-01-15T12:00:00+00:00';end='2024-01-15T12:15:00+00:00'
        observation=dict(opened_at=at,available_at='2024-01-15T12:01:00+00:00',source_id='fixture-native',synthetic=False,open=1.,high=2.,low=.5,close=1.5,volume=10.)
        evidence=dict(aggregation=old['POPULATION_CONTRACT'],source_id='fixture-native',source_dataset='fixture-dataset',source_coverage=dict(basis='retained-fixture',opened_at=at,closed_at=end),observations=[observation])
        row=dict(population_contract=old['POPULATION_CONTRACT'],source_id='fixture-native',source_dataset='fixture-dataset',synthetic=False,no_trade=False,open=1.,high=2.,low=.5,close=1.5,volume=10.,interval_seconds=900,synthetic_constituents=0,observed_constituents=1,source_empty_constituents=14,opened_at=at,available_at=end,native_first_opened_at=at,native_last_opened_at=at,evidence_json=json.dumps(evidence))
        check(outcome(old,row)==outcome(new,row)==('value',True),'valid source proof parity')
        mapping=copy.deepcopy(row);mapping['evidence_json']=copy.deepcopy(evidence)
        check(outcome(old,mapping)==outcome(new,mapping)==('value',True),'mapping evidence parity')
        invalid=[('open',0),('high',0),('low',1e99),('volume',-1),('interval_seconds',60),('synthetic',True),('no_trade',True),('observed_constituents',0),('source_empty_constituents',99),('available_at','1990-01-01T00:00:00+00:00'),('opened_at','2024-01-15T12:00:01+00:00'),('native_first_opened_at','1990-01-01T00:00:00+00:00'),('evidence_json','{}')]
        for key,value in invalid:
            z=copy.deepcopy(row);z[key]=value;check(outcome(old,z)==outcome(new,z)==('value',False),'invalid proof parity '+key)
        def shift(x,year):
            if isinstance(x,dict):return{k:shift(v,year)for k,v in x.items()}
            if isinstance(x,list):return[shift(v,year)for v in x]
            if isinstance(x,str):
                try:
                    t=datetime.fromisoformat(x)
                    if t.tzinfo:return t.replace(year=year).isoformat()
                except (TypeError,ValueError):pass
            return x
        for year in(1500,1677,1678,2261,2262,2500):
            z=shift(mapping,year);check(outcome(old,z)==outcome(new,z)==('value',True),'nanosecond overflow fallback parity '+str(year))
        for mode_ctor in('ordinary','value_error','runtime_error'):
            def replacement(*a,**kw):
                calls.append((a,kw))
                if mode_ctor=='value_error':raise ValueError('fixture constructor refusal')
                if mode_ctor=='runtime_error':raise RuntimeError('fixture constructor failure')
                return original_ctor(*a,**kw)
            pd.Timedelta=replacement;calls.clear();special=make(projected)
            check(not calls,'no replaced constructor call at module initialization '+mode_ctor)
            native_calls.clear();left=outcome(old,row);leftcalls=len(calls);leftnative=list(native_calls);calls.clear();native_calls.clear();right=outcome(special,row)
            check(left==right,'preinitialized dynamic constructor parity '+mode_ctor)
            check(leftcalls==len(calls)==1,'constructor fallback call count '+mode_ctor)
            check(leftnative==native_calls==[2],'native evidence validation preserved '+mode_ctor)
            calls.clear();bad=dict(row,open=0);check(outcome(special,bad)==('value',False)and not calls,'early invalid proof skips dynamic constructor '+mode_ctor)
            pd.Timedelta=original_ctor
        def late_replace():pd.Timedelta=lambda **kw:(_ for _ in()).throw(ValueError('fixture late replacement'))
        hook[0]=late_replace;pd.Timedelta=original_ctor;left=outcome(old,row);pd.Timedelta=original_ctor;right=outcome(new,row);hook[0]=None;pd.Timedelta=original_ctor
        check(left==right==('value',False),'native-validation callback constructor replacement parity')
        check(not network,'zero network attempts')
    finally:
        pd.Timedelta=original_ctor
        for key,value in previous.items():
            if value is None:sys.modules.pop(key,None)
            else:sys.modules[key]=value
    print(json.dumps(dict(checks_passed=len(checks),source_validation=mode,after_projection_sha256=meta['after_projection_sha256'],network_attempts=len(network),limitation='Exact changed validator uses actual pandas; native evidence import is a stub. Complete application and retained-input integration remain separate.')))
    return 0


if __name__=='__main__':raise SystemExit(main())
