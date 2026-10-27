"""Portable exact-function proof parity with real pandas/NumPy; no provider calls."""
from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import pickle
import re
import socket
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META = json.loads((ROOT/'docs/development/patches/intraday-native-proof-clocks.json').read_text(encoding='utf-8'))
RECORD = META['sources']['ml/intraday/coverage.py']
PATCH = ROOT/'docs/development/patches/intraday-native-proof-clocks.patch'
NETWORK_ATTEMPTS = []


def source(record):
    value=record['source']
    if hashlib.sha256(value.encode()).hexdigest()!=record['sha256']:
        raise AssertionError('Exact source extract changed')
    ast.parse(value)
    return value


def namespace(phase):
    ns={'pd':pd,'np':np,'math':math,'json':json,'Mapping':Mapping,
        'datetime':datetime,'timedelta':timedelta,'timezone':timezone}
    native={}
    def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name=='data' and level==1:
            return SimpleNamespace(**native)
        if name=='ml.market_gap_evidence':
            return SimpleNamespace(MARKERS=ns['MARKERS'])
        return builtins.__import__(name,globals,locals,fromlist,level)
    ns['__builtins__']=dict(vars(builtins),__import__=controlled_import)
    for name in ['ml/intraday/prediction.py','ml/market_gap_evidence.py','ml/intraday/data.py']:
        for record in META['unchanged_dependencies'][name]['extracts'].values():
            exec(compile('from __future__ import annotations\n'+source(record),'exact-dependency','exec'),ns)
    native['_require_native_observations']=ns['_require_native_observations']
    for record in RECORD['constants'].values():
        exec(compile(source(record),'exact-constant','exec'),ns)
    for record in RECORD['extracts_'+phase].values():
        exec(compile('from __future__ import annotations\n'+source(record),'exact-'+phase,'exec'),ns)
    return ns


BEFORE, AFTER=namespace('before'),namespace('after')


def fixture(indices=tuple(range(15)), opened='2026-01-02T14:00:00+00:00', serialized=False):
    stamp=datetime.fromisoformat(opened.replace('Z','+00:00')) if isinstance(opened,str) else opened
    observations=[{'opened_at':(stamp+timedelta(minutes=i)).isoformat(),
        'available_at':(stamp+timedelta(minutes=i+1)).isoformat(),'source_id':'offline-native',
        'synthetic':False,'open':100.+i,'high':101.+i,'low':99.+i,'close':100.5+i,'volume':float(i+1)} for i in indices]
    proof={'aggregation':BEFORE['POPULATION_CONTRACT'],'source_id':'offline-native','source_dataset':'OFFLINE',
        'source_coverage':{'opened_at':stamp.isoformat(),'closed_at':(stamp+timedelta(minutes=15)).isoformat(),'basis':'explicit offline fixture'},'observations':observations}
    return {'symbol':'OFFLINE','opened_at':stamp.isoformat(),'available_at':(stamp+timedelta(minutes=15)).isoformat(),
        'open':observations[0]['open'],'high':max(r['high'] for r in observations),
        'low':min(r['low'] for r in observations),'close':observations[-1]['close'],'volume':sum(r['volume'] for r in observations),
        'source_id':'offline-native','source_dataset':'OFFLINE','synthetic':False,'no_trade':False,
        'interval_seconds':900,'synthetic_constituents':0,'observed_constituents':len(indices),'source_empty_constituents':15-len(indices),
        'native_first_opened_at':observations[0]['opened_at'],'native_last_opened_at':observations[-1]['opened_at'],
        'population_contract':BEFORE['POPULATION_CONTRACT'],'evidence_json':json.dumps(proof) if serialized else proof}


def outcome(fn,value):
    try:
        return ('return',fn(value))
    except Exception as exc:
        return ('exception',type(exc).__name__,str(exc))


class ProofParity(unittest.TestCase):
    def parity(self,value,expected=None):
        left,right=deepcopy(value),deepcopy(value)
        before_left,before_right=pickle.dumps(left),pickle.dumps(right)
        old=outcome(BEFORE['verified_source_observation'],left)
        new=outcome(AFTER['verified_source_observation'],right)
        self.assertEqual(old,new)
        self.assertEqual(before_left,pickle.dumps(left),'Original mutated input')
        self.assertEqual(before_right,pickle.dumps(right),'Candidate mutated input')
        if expected is not None:self.assertEqual(new,('return',expected))

    def test_exact_patch_projects_complete_proof(self):
        patch=PATCH.read_text(encoding='utf-8')
        self.assertEqual(hashlib.sha256(patch.encode()).hexdigest(),META['patch_sha256'])
        self.assertEqual(patch.splitlines()[:2],['--- a/ml/intraday/coverage.py','+++ b/ml/intraday/coverage.py'])
        old=RECORD['extracts_before']['verified_source_observation']
        original=source(old).splitlines(keepends=True)
        result=[]; cursor=0; lines=patch.splitlines(keepends=True); index=2
        while index<len(lines):
            match=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[index])
            self.assertIsNotNone(match,'Unexpected unified diff metadata')
            start=int(match.group(1))-old['line'];self.assertGreaterEqual(start,cursor)
            result.extend(original[cursor:start]);cursor=start;index+=1
            while index<len(lines) and not lines[index].startswith('@@'):
                line=lines[index];index+=1
                if line.startswith((' ','-')):
                    self.assertEqual(original[cursor],line[1:]);cursor+=1
                if line.startswith((' ','+')):result.append(line[1:])
        result.extend(original[cursor:])
        self.assertEqual(''.join(result),source(RECORD['extracts_after']['verified_source_observation']))

    def test_only_proof_changes_and_dependencies_are_parseable(self):
        self.assertEqual(RECORD['extracts_before']['_utc']['source'],RECORD['extracts_after']['_utc']['source'])
        for group in ['extracts_before','extracts_after','constants']:
            for record in RECORD[group].values():source(record)
        for dependency in META['unchanged_dependencies'].values():
            for record in dependency['extracts'].values():source(record)
        self.assertEqual(set(META['unchanged_dependencies']),{'ml/intraday/prediction.py','ml/intraday/data.py','ml/market_gap_evidence.py'})

    def test_network_prohibited_and_unused(self):
        self.parity(fixture(),True)
        self.assertEqual(NETWORK_ATTEMPTS,[])


def add_case(name,build,expected=None):
    def test(self):self.parity(build(),expected)
    setattr(ProofParity,'test_'+name,test)


dates=['2026-01-02T14:00:00+00:00','1969-12-31T23:45:00+00:00','1970-01-01T00:00:00+00:00',
       '2026-11-01T01:00:00-08:00','2026-11-01T01:00:00-07:00','2026-01-02T19:30:00+05:30',
       '0001-01-02T14:00:00+00:00','1600-01-02T14:00:00+00:00','1677-09-20T14:00:00+00:00',
       '1677-09-21T00:15:00+00:00','2262-04-11T23:15:00+00:00','2262-04-11T23:45:00+00:00',
       '2300-01-02T14:00:00+00:00','9999-01-02T14:00:00+00:00']
for i,date in enumerate(dates):
    for j,indices in enumerate([tuple(range(15)),(0,7,14),(8,)]):
        for serialized in [False,True]:
            add_case('date_%02d_%d_%d'%(i,j,serialized),lambda d=date,x=indices,s=serialized:fixture(x,d,s))

def change(location,key,value):
    item=fixture()
    target=item if location=='candle' else item['evidence_json']['source_coverage'] if location=='coverage' else item['evidence_json']['observations'][4]
    target[key]=deepcopy(value)
    return item

bad_clocks=[None,pd.NaT,float('nan'),'not-a-time','2026-01-02T14:04:00',
            pd.Timestamp('2026-01-02T14:04:00Z')+pd.Timedelta(nanoseconds=1),
            pd.Timestamp('2026-01-02T14:04:00Z')-pd.Timedelta(nanoseconds=1),
            datetime(1,1,2,14,tzinfo=timezone.utc),datetime(9999,1,2,14,tzinfo=timezone.utc)]
for location,keys in [('candle',['opened_at','available_at','native_first_opened_at','native_last_opened_at']),('coverage',['opened_at','closed_at']),('native',['opened_at','available_at'])]:
    for key in keys:
        for i,value in enumerate(bad_clocks):
            add_case('clock_%s_%s_%02d'%(location,key,i),lambda l=location,k=key,v=value:change(l,k,v))

for location in ['candle','native']:
    for key in BEFORE['MARKERS']:
        for i,value in enumerate([True,False,0,1,None,'false',np.bool_(False),np.bool_(True)]):
            add_case('marker_%s_%s_%d'%(location,key,i),lambda l=location,k=key,v=value:change(l,k,v))
    for key in ['open','high','low','close','volume']:
        for i,value in enumerate([float('nan'),float('inf'),-1.,0.,'bad',None,1000.]):
            add_case('value_%s_%s_%d'%(location,key,i),lambda l=location,k=key,v=value:change(l,k,v))

for key in ['observed_constituents','source_empty_constituents','synthetic_constituents','interval_seconds']:
    for i,value in enumerate([True,np.bool_(False),None,'15',-1,0,0.,14,15,16,float('nan'),float('inf')]):
        add_case('count_%s_%02d'%(key,i),lambda k=key,v=value:change('candle',k,v))

def malformed(case):
    item=fixture();proof=item['evidence_json'];rows=proof['observations']
    if case=='duplicate':rows[2]=deepcopy(rows[1])
    elif case=='unordered':rows[2],rows[3]=rows[3],rows[2]
    elif case=='missing':rows.pop()
    elif case=='empty':proof['observations']=[]
    elif case=='source':rows[2]['source_id']='other'
    elif case=='dataset':proof['source_dataset']='other'
    elif case=='aggregation':proof['aggregation']='other'
    elif case=='contract':item['population_contract']='other'
    elif case=='uncovered_open':proof['source_coverage']['opened_at']='2026-01-02T14:01:00Z'
    elif case=='uncovered_close':proof['source_coverage']['closed_at']='2026-01-02T14:14:00Z'
    elif case=='no_basis':proof['source_coverage']['basis']=''
    elif case=='invalid_json':item['evidence_json']='{'
    elif case=='evidence_list':item['evidence_json']=[]
    elif case=='evidence_null':item['evidence_json']=None
    elif case=='rows_null':proof['observations']=None
    elif case=='row_list':rows[1]=[]
    elif case=='missing_key':del rows[1]['available_at']
    elif case=='early_available':rows[4]['available_at']=pd.Timestamp('2026-01-02T14:05:00Z')-pd.Timedelta(nanoseconds=1)
    elif case=='exact_available':rows[4]['available_at']=pd.Timestamp('2026-01-02T14:05:00Z')
    elif case=='late_available':rows[4]['available_at']=pd.Timestamp('2026-01-02T14:15:00Z')+pd.Timedelta(nanoseconds=1)
    elif case=='equal_available':rows[4]['available_at']=pd.Timestamp('2026-01-02T14:15:00Z')
    elif case=='nanosecond_open':rows[4]['opened_at']=pd.Timestamp('2026-01-02T14:04:00Z')+pd.Timedelta(nanoseconds=1)
    elif case=='before_interval':rows[0]['opened_at']='2026-01-02T13:59:00Z'
    elif case=='after_interval':rows[-1]['opened_at']='2026-01-02T14:15:00Z'
    return item

for case in ['duplicate','unordered','missing','empty','source','dataset','aggregation','contract','uncovered_open','uncovered_close','no_basis','invalid_json','evidence_list','evidence_null','rows_null','row_list','missing_key','early_available','exact_available','late_available','equal_available','nanosecond_open','before_interval','after_interval']:
    add_case('proof_'+case,lambda c=case:malformed(c))


def run():
    saved={name:getattr(socket,name) for name in ['create_connection','getaddrinfo']}
    saved_connect,saved_connect_ex=socket.socket.connect,socket.socket.connect_ex
    def denied(*args,**kwargs):
        NETWORK_ATTEMPTS.append('blocked')
        raise AssertionError('Network prohibited in offline source-proof checks')
    try:
        socket.create_connection=socket.getaddrinfo=denied
        socket.socket.connect=socket.socket.connect_ex=denied
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(ProofParity)
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        socket.socket.connect,socket.socket.connect_ex=saved_connect,saved_connect_ex
        for name,value in saved.items():setattr(socket,name,value)
    print(json.dumps({'checks':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'network_attempts':len(NETWORK_ATTEMPTS),'pandas':pd.__version__,'numpy':np.__version__}))
    return 0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1


if __name__=='__main__':raise SystemExit(run())
