"""Portable exact sizing-payload proof checks; no external or trading operations."""
from __future__ import annotations
import argparse
import ast
from copy import copy, deepcopy
from dataclasses import asdict, dataclass, fields, replace, FrozenInstanceError
import hashlib
import json
from pathlib import Path
import pickle
import re
import socket
import sys
from types import ModuleType
import unittest

PARSER=argparse.ArgumentParser()
PARSER.add_argument('--installed-source',type=Path)
OPTIONS,REST=PARSER.parse_known_args()
ROOT=Path(__file__).resolve().parents[1]
BASE='intraday-sizing-payload-proof'
META=json.loads((ROOT/f'docs/development/patches/{BASE}.json').read_text(encoding='utf8'))
RECORD=META['sources']['ml/intraday/technical_sizing.py']
NETWORK=[]
def deny(*args,**kwargs):NETWORK.append('blocked');raise AssertionError('network prohibited')
socket.create_connection=deny
socket.socket.connect=deny
socket.socket.connect_ex=deny
def checked(record):
    value=record['source']
    assert hashlib.sha256(value.encode()).hexdigest()==record['sha256']
    ast.parse(value)
    return value
def namespace(phase):
    module=ModuleType('_sizing_payload_'+phase)
    sys.modules[module.__name__]=module
    ns=module.__dict__
    ns.update(dataclass=dataclass,hashlib=hashlib,json=json)
    for item in META['unchanged_dependencies']['ml/intraday/prediction.py']['extracts'].values():
        exec(compile(checked(item),'exact-prediction','exec'),ns)
    source=checked(RECORD['region_'+phase])
    if phase=='after' and OPTIONS.installed_source:
        raw=OPTIONS.installed_source.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==RECORD['after_sha256']
        text=raw.decode().replace('\r\n','\n')
        lines=text.splitlines(keepends=True)
        source=''.join(lines[RECORD['region_after']['line']-1:RECORD['region_after']['end_line']])
        assert source==checked(RECORD['region_after'])
    exec(compile('from __future__ import annotations\n'+source,'exact-'+phase,'exec'),ns)
    return ns
BEFORE,AFTER=namespace('before'),namespace('after')
def outcome(instance):
    try:return 'return',instance.payload()
    except Exception as error:return 'error',type(error).__name__,str(error)
def identity_field(name):return 'snapshot_id' if name=='IndicatorSnapshot' else 'result_id'
def new(ns,name,text,identifier=None):
    if identifier is None:
        identifier=ns['digest'](json.loads(text))
    return ns[name](identifier,text)
def produced(ns,name,value):return ns['_encoded_result'](ns[name],value)
def produced_outcome(ns,name,value):
    try:return outcome(produced(ns,name,value))
    except Exception as error:return 'error',type(error).__name__,str(error)
VALUE={'status':'sized','desired_quantity':3,'nested':{'values':[1,2,{'a':'x'}]},'null':None,'float':-0.0}

class PackageChecks(unittest.TestCase):
    def test_zero_context_patch_reconstructs_exact_owned_region(self):
        text=(ROOT/f'docs/development/patches/{BASE}.patch').read_text(encoding='utf8')
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),META['patch_sha256'])
        lines=text.splitlines(keepends=True)
        self.assertEqual([x.rstrip('\n') for x in lines[:2]],['--- a/ml/intraday/technical_sizing.py','+++ b/ml/intraday/technical_sizing.py'])
        original=checked(RECORD['region_before']).splitlines(keepends=True)
        base=RECORD['region_before']['line'];cursor=0;result=[];index=2
        while index<len(lines):
            match=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[index]);self.assertIsNotNone(match)
            start,count=int(match[1]),int(match[2] or 1);offset=start-base+(count==0)
            self.assertGreaterEqual(offset,cursor);self.assertLessEqual(offset,len(original))
            result.extend(original[cursor:offset]);cursor=offset;index+=1
            while index<len(lines) and not lines[index].startswith('@@'):
                line=lines[index];index+=1
                if line.startswith('-'):self.assertEqual(original[cursor],line[1:]);cursor+=1
                elif line.startswith('+'):result.append(line[1:])
                else:self.fail('zero-context patch expected')
        result.extend(original[cursor:])
        self.assertEqual(''.join(result),checked(RECORD['region_after']))

    def test_dataclass_public_shape_unchanged(self):
        for name in ('IndicatorSnapshot','SizingResult'):
            old,new_instance=produced(BEFORE,name,VALUE),produced(AFTER,name,VALUE)
            old.payload();new_instance.payload()
            self.assertEqual(asdict(old),asdict(new_instance))
            self.assertEqual([x.name for x in fields(old)],[x.name for x in fields(new_instance)])
            self.assertEqual(repr(old),repr(new_instance))
            self.assertEqual(new_instance,replace(new_instance))
            self.assertEqual(hash(new_instance),hash(replace(new_instance)))
            with self.assertRaises(FrozenInstanceError):new_instance.payload_json='{}'

    def test_network_unused(self):self.assertEqual(NETWORK,[])

def add(name,fn):setattr(PackageChecks,'test_'+name,fn)
for NAME in ('IndicatorSnapshot','SizingResult'):
    def fresh(self,name=NAME):
        instance=produced(AFTER,name,VALUE)
        first=instance.payload();first['nested']['values'][2]['a']='changed'
        second=instance.payload()
        self.assertEqual(second,VALUE);self.assertIsNot(first,second)
        self.assertIsNot(first['nested'],second['nested'])
    add(NAME+'_fresh_owned_tree',fresh)
    def counts(self,name=NAME):
        original=AFTER['digest'];calls=[]
        def count(value):calls.append(1);return original(value)
        AFTER['digest']=count
        try:
            instance=produced(AFTER,name,VALUE)
            self.assertEqual(instance.payload(),VALUE)
            self.assertEqual(instance.payload(),VALUE)
            self.assertEqual(len(calls),1)
            object.__setattr__(instance,'payload_json',(' '+instance.payload_json).lstrip())
            self.assertEqual(instance.payload(),VALUE)
            self.assertEqual(len(calls),2)
            object.__setattr__(instance,identity_field(name),(' '+getattr(instance,identity_field(name))).lstrip())
            self.assertEqual(instance.payload(),VALUE)
            self.assertEqual(len(calls),3)
        finally:AFTER['digest']=original
    add(NAME+'_exact_string_identity_proof',counts)
    def field_mutation(self,name=NAME):
        for field,value in [('payload_json','{}'),(identity_field(name),'0'*64)]:
            old,new_instance=produced(BEFORE,name,VALUE),produced(AFTER,name,VALUE)
            old.payload();new_instance.payload()
            object.__setattr__(old,field,value);object.__setattr__(new_instance,field,value)
            self.assertEqual(outcome(old),outcome(new_instance))
    add(NAME+'_tampered_public_fields',field_mutation)
    def proofs(self,name=NAME):
        for proof in [None,(),(object(),'{}','x'),('fake','{}','x'),{},[],False]:
            instance=produced(AFTER,name,VALUE);instance.payload()
            object.__setattr__(instance,'_validated_payload_proof',proof)
            self.assertEqual(instance.payload(),VALUE)
            object.__setattr__(instance,'payload_json','{}')
            self.assertEqual(outcome(instance)[0],'error')
    add(NAME+'_invalid_private_proof_falls_back',proofs)
    def copies(self,name=NAME):
        instance=produced(AFTER,name,VALUE);instance.payload()
        for duplicate in (copy(instance),deepcopy(instance),replace(instance),pickle.loads(pickle.dumps(instance))):
            self.assertEqual(duplicate.payload(),VALUE)
            self.assertEqual(duplicate.payload(),VALUE)
    add(NAME+'_copy_deepcopy_replace_pickle',copies)
    def subclasses(self,name=NAME):
        class Old(BEFORE[name]):pass
        class New(AFTER[name]):pass
        text=AFTER['encoded'](VALUE);identifier=AFTER['digest'](VALUE)
        old,new_instance=Old(identifier,text),New(identifier,text)
        self.assertEqual(outcome(old),outcome(new_instance))
        self.assertNotIn('_validated_payload_proof',new_instance.__dict__)
        def fail(self,field):
            if field==identity_field(name):raise RuntimeError('offline identifier access')
            return object.__getattribute__(self,field)
        Old.__getattribute__=fail;New.__getattribute__=fail
        object.__setattr__(old,'payload_json','bad');object.__setattr__(new_instance,'payload_json','bad')
        self.assertEqual(outcome(old),outcome(new_instance))
        object.__setattr__(old,'payload_json','{}');object.__setattr__(new_instance,'payload_json','{}')
        self.assertEqual(outcome(old),outcome(new_instance))
    add(NAME+'_subclass_access_order',subclasses)
    def string_subclasses(self,name=NAME):
        class Trick(str):
            def __eq__(self,other):return True
        for which in ('text','id'):
            text=AFTER['encoded'](VALUE);identifier=AFTER['digest'](VALUE)
            if which=='text':text=Trick(text)
            else:identifier=Trick(identifier)
            old,new_instance=new(BEFORE,name,text,identifier),new(AFTER,name,text,identifier)
            self.assertEqual(outcome(old),outcome(new_instance))
            self.assertNotIn('_validated_payload_proof',new_instance.__dict__)
    add(NAME+'_str_subclass_no_proof',string_subclasses)
    def numeric(self,name=NAME):
        for value in [{2:'two',10:'ten'},{'nested':{2:'two',10:'ten'}},{1:'number','1':'string'},{False:1,True:2}]:
            self.assertEqual(produced_outcome(BEFORE,name,value),produced_outcome(AFTER,name,value))
    add(NAME+'_numeric_mapping_key_normalization',numeric)
    for INDEX,TEXT in enumerate(['{}',' { "b" : 2, "a" : 1 } ','{"a":1,"a":2}','[1,2,{"a":0}]','null','true','1.0','-0.0','"unicode \\u00e9"','{"x":1e20}','{"x":NaN}','{"x":Infinity}','bad','{"a":','']):
        def json_parity(self,name=NAME,text=TEXT):
            try:identifier=BEFORE['digest'](json.loads(text))
            except Exception:identifier='offline-invalid'
            old,new_instance=new(BEFORE,name,text,identifier),new(AFTER,name,text,identifier)
            self.assertEqual(outcome(old),outcome(new_instance))
            self.assertEqual(outcome(old),outcome(new_instance))
        add(NAME+'_legacy_json_'+str(INDEX),json_parity)

def property_test(self):
    result=produced(AFTER,'SizingResult',VALUE)
    self.assertEqual(result.status,'sized');self.assertEqual(result.desired_quantity,3)
    self.assertIsNone(result.adjusted_quantity)
    result.payload()['desired_quantity']=999
    self.assertEqual(result.desired_quantity,3)
add('sizing_properties_fresh',property_test)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(PackageChecks)
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'network_attempts':NETWORK,'installed_source':str(OPTIONS.installed_source) if OPTIONS.installed_source else None}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
