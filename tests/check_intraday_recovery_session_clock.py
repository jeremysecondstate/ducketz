"""Exact extracted session validation parity; no acquisition or runtime action."""
from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import pickle
import re
import socket
import sys
from types import ModuleType, SimpleNamespace
import unittest

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
META=json.loads((ROOT/'docs/development/patches/intraday-recovery-session-clock.json').read_text(encoding='utf8'))
RECORD=META['sources']['ml/intraday/recovered_population.py']
PATCH=ROOT/'docs/development/patches/intraday-recovery-session-clock.patch'
NETWORK=[]


def source(record):
    value=record['source']
    if hashlib.sha256(value.encode()).hexdigest()!=record['sha256']:
        raise AssertionError('Exact source extract changed')
    ast.parse(value)
    return value


def namespace(phase):
    module=ModuleType('_session_clock_'+phase)
    sys.modules[module.__name__]=module
    ns=module.__dict__
    ns.update(pd=pd,Mapping=Mapping,dataclass=dataclass,datetime=datetime,timedelta=timedelta,timezone=timezone)
    def controlled_import(name,globals=None,locals=None,fromlist=(),level=0):
        if name=='data' and level==1:
            return SimpleNamespace(SessionBounds=ns['SessionBounds'])
        return builtins.__import__(name,globals,locals,fromlist,level)
    ns['__builtins__']=dict(vars(builtins),__import__=controlled_import)
    for name in ('ml/intraday/prediction.py','ml/intraday/data.py'):
        for record in META['unchanged_dependencies'][name]['extracts'].values():
            exec(compile('from __future__ import annotations\n'+source(record),'exact-dependency','exec'),ns)
    for record in RECORD.get('constants_'+phase,{}).values():
        exec(compile(source(record),'exact-constant','exec'),ns)
    for record in RECORD['extracts_'+phase].values():
        exec(compile('from __future__ import annotations\n'+source(record),'exact-'+phase,'exec'),ns)
    return ns


BEFORE,AFTER=namespace('before'),namespace('after')


def result(fn,session,clock):
    try:return ('return',fn(session,clock))
    except Exception as exc:return ('exception',type(exc).__name__,str(exc))


def fixture(opened='2026-10-12T17:00:00+00:00'):
    stamp=pd.Timestamp(opened)
    return {'session_id':'offline-session','session_open':stamp.isoformat(),
            'session_close':(stamp+pd.Timedelta(hours=1)).isoformat()},opened


class SessionParity(unittest.TestCase):
    def parity(self,session,clock,expected=None):
        left,right=deepcopy(session),deepcopy(session)
        original_left,original_right=pickle.dumps(left),pickle.dumps(right)
        old,new=result(BEFORE['_session'],left,clock),result(AFTER['_session'],right,clock)
        self.assertEqual(old,new)
        self.assertEqual(pickle.dumps(left),original_left)
        self.assertEqual(pickle.dumps(right),original_right)
        if expected is not None:self.assertEqual(new[0],expected)

    def test_patch_projects_exact_session_and_constant(self):
        patch=PATCH.read_text(encoding='utf8')
        self.assertEqual(hashlib.sha256(patch.encode()).hexdigest(),META['patch_sha256'])
        self.assertEqual(patch.splitlines()[:2],['--- a/ml/intraday/recovered_population.py','+++ b/ml/intraday/recovered_population.py'])
        old=RECORD['extracts_before']['_session'];original=source(old).splitlines(keepends=True)
        lines=patch.splitlines(keepends=True);index=2;cursor=0;projected=[];constant=[];session_hunks=0
        while index<len(lines):
            match=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[index]);self.assertIsNotNone(match)
            start,count=int(match.group(1)),int(match.group(2)or 1);index+=1;body=[]
            while index<len(lines)and not lines[index].startswith('@@'):
                body.append(lines[index]);index+=1
            if start<old['line']:
                self.assertEqual(count,0)
                self.assertTrue(all(line.startswith('+')for line in body));constant.extend(line[1:]for line in body)
                continue
            offset=start-old['line'];self.assertGreaterEqual(offset,cursor);self.assertLessEqual(start+count-1,old['end_line'])
            projected.extend(original[cursor:offset]);cursor=offset;session_hunks+=1
            for line in body:
                if line.startswith((' ','-')):self.assertEqual(original[cursor],line[1:]);cursor+=1
                if line.startswith((' ','+')):projected.append(line[1:])
        projected.extend(original[cursor:])
        self.assertGreater(session_hunks,0)
        self.assertEqual(''.join(projected),source(RECORD['extracts_after']['_session']))
        self.assertEqual(''.join(constant),source(RECORD['constants_after']['_SESSION_QUARTER']))

    def test_only_session_and_fixed_offset_change(self):
        self.assertEqual(RECORD['extracts_before']['_utc']['source'],RECORD['extracts_after']['_utc']['source'])
        self.assertEqual(set(RECORD['constants_after']),{'_SESSION_QUARTER'})
        self.assertEqual(set(META['unchanged_dependencies']),{'ml/intraday/prediction.py','ml/intraday/data.py'})
        for record in META['unchanged_dependencies'].values():
            for extract in record['extracts'].values():source(extract)

    def test_custom_contains_short_circuit_and_return_order(self):
        class Custom:
            def __init__(self,answer):self.answer=answer;self.calls=[]
            def contains(self,clock):self.calls.append(('contains',clock));return self.answer
            @property
            def session_id(self):self.calls.append('id');return 'custom'
            @property
            def opened_at(self):self.calls.append('open');return '2026-10-12T17:00:00Z'
            @property
            def closed_at(self):self.calls.append('close');return '2026-10-12T18:00:00Z'
        for answer,clock in [(False,'invalid'),(True,'invalid'),(True,'2026-10-12T17:00:00Z'),(False,None),(True,'2026-10-12T17:00:00.000000001Z')]:
            old,new=Custom(answer),Custom(answer)
            self.assertEqual(result(BEFORE['_session'],old,clock),result(AFTER['_session'],new,clock))
            self.assertEqual(old.calls,new.calls)

    def test_custom_contains_exception_preserved(self):
        class Custom:
            def contains(self,clock):raise RuntimeError('offline exact custom failure')
        self.assertEqual(result(BEFORE['_session'],Custom(),'invalid'),result(AFTER['_session'],Custom(),'invalid'))

    def test_network_prohibited_unused(self):
        self.parity(*fixture(),'return');self.assertEqual(NETWORK,[])


def add(name,session,clock,expected=None):
    def test(self):self.parity(session,clock,expected)
    setattr(SessionParity,'test_'+name,test)


dates=['0001-01-02T00:00:00+00:00','1600-01-02T00:00:00+00:00','1677-09-21T00:00:00+00:00',
       '1969-12-31T23:45:00+00:00','1970-01-01T00:00:00+00:00','2026-11-01T01:00:00-08:00',
       '2026-11-01T01:00:00-07:00','2026-01-02T19:30:00+05:30','2262-04-11T23:45:00+00:00',
       '2500-01-02T00:00:00+00:00','9999-01-02T00:00:00+00:00']
for index,clock in enumerate(dates):
    session,opened=fixture(clock)
    for kind,value in [('string',opened),('timestamp',pd.Timestamp(opened)),('datetime',datetime.fromisoformat(opened))]:
        add('domain_%02d_%s'%(index,kind),session,value,'return')
for index,delta in enumerate([-900000000001,-900000000000,-1,0,1,59999999999,60000000000,899999999999,900000000000,2700000000000,2700000000001,3600000000000]):
    session,clock=fixture();value=pd.Timestamp(clock)+pd.Timedelta(nanoseconds=delta)
    for kind,opened in [('timestamp',value),('string',value.isoformat())]:add('boundary_%02d_%s'%(index,kind),session,opened)
bad=[None,pd.NaT,'NaT',float('nan'),'not-a-time','2026-10-12T17:00:00',datetime(2026,10,12,17),True,0,{},[]]
for field in ['clock','session_open','session_close','session_id']:
    for index,value in enumerate(bad):
        session,clock=fixture()
        if field=='clock':clock=value
        else:session[field]=value
        add('malformed_%s_%02d'%(field,index),session,clock)
for missing in ['session_id','session_open','session_close']:
    session,clock=fixture();del session[missing];add('missing_'+missing,session,clock)
for index,session in enumerate([None,{},[],SimpleNamespace(),BEFORE['SessionBounds']('typed','2026-10-12T17:00:00Z','2026-10-12T18:00:00Z')]):
    add('session_type_%02d'%index,session,'2026-10-12T17:00:00Z')


def run():
    saved={name:getattr(socket,name)for name in ['create_connection','getaddrinfo']}
    saved_connect,saved_ex=socket.socket.connect,socket.socket.connect_ex
    def denied(*a,**k):NETWORK.append('blocked');raise AssertionError('Network prohibited in offline session checks')
    try:
        socket.create_connection=socket.getaddrinfo=denied;socket.socket.connect=socket.socket.connect_ex=denied
        result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(SessionParity))
    finally:
        socket.socket.connect,socket.socket.connect_ex=saved_connect,saved_ex
        for name,value in saved.items():setattr(socket,name,value)
    print(json.dumps({'checks':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'network_attempts':len(NETWORK),'pandas':pd.__version__}))
    return 0 if result.wasSuccessful()and not NETWORK else 1


if __name__=='__main__':raise SystemExit(run())
