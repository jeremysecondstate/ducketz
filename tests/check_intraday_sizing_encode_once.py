"""Portable offline exact-extract regression; no broker, datastore or model use."""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass,replace
import hashlib
import json
from pathlib import Path
import re
import socket
import unittest

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'docs/development/patches/intraday-sizing-encode-once.json'
TECH='ml/intraday/technical_sizing.py'
START='ml/intraday/starter_sizing.py'
META=json.loads(PACKAGE.read_text(encoding='utf8'))

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

class ContractError(ValueError):
    pass

def source(path,name,phase='after'):
    item=META['sources'][path]['extracts_'+phase][name]
    if hashlib.sha256(item['source'].encode()).hexdigest()!=item['sha256']:
        raise AssertionError('Exact extract hash changed: '+name)
    return item['source']

def namespace():
    ns={'encoded':encoded,'digest':digest,'hashlib':hashlib,'dataclass':dataclass,
        'json':json,'ContractError':ContractError}
    for name in('_encoded_result','IndicatorSnapshot','SizingResult'):
        exec(compile('from __future__ import annotations\n'+source(TECH,name),name,'exec'),ns)
    return ns

def dump(text):
    return ast.dump(ast.parse(text),include_attributes=False)

class RestoreConstructors(ast.NodeTransformer):
    def visit_Call(self,node):
        self.generic_visit(node)
        if isinstance(node.func,ast.Name)and node.func.id=='_encoded_result':
            typ,value=node.args
            return ast.copy_location(ast.Call(func=typ,args=[
                ast.Call(func=ast.Name(id='digest',ctx=ast.Load()),args=[copy.deepcopy(value)],keywords=[]),
                ast.Call(func=ast.Name(id='encoded',ctx=ast.Load()),args=[copy.deepcopy(value)],keywords=[])],keywords=[]),node)
        return node

def apply_unified(original,patch_text,path,reverse=False):
    lines=patch_text.splitlines(keepends=True);body=[];active=False
    for line in lines:
        if line.startswith('--- a/'):
            active=line.rstrip('\n')=='--- a/'+path
        if active:body.append(line)
    old=original.splitlines(keepends=True);result=[];pos=0;i=2
    while i<len(body):
        match=re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n',body[i])
        if not match:raise AssertionError('Invalid exact patch hunk')
        start=int(match.group(3 if reverse else 1))-1
        result.extend(old[pos:start]);pos=start;i+=1
        while i<len(body)and not body[i].startswith('@@ '):
            kind,text=body[i][0],body[i][1:]
            if reverse:kind={'+':'-','-':'+',' ':' '}[kind]
            if kind in(' ','-'):
                if pos>=len(old)or old[pos]!=text:raise AssertionError('Exact patch preimage differs')
                pos+=1
            if kind in(' ','+'):result.append(text)
            i+=1
    result.extend(old[pos:]);return ''.join(result)

class EncodeOnce(unittest.TestCase):
    def setUp(self):
        self.ns=namespace();self.create=self.ns['_encoded_result'];self.cls=self.ns['SizingResult']

    def parity(self,value,cls=None):
        cls=cls or self.cls
        result=self.create(cls,value)
        self.assertEqual((result.result_id if hasattr(result,'result_id')else result.snapshot_id,result.payload_json),(digest(value),encoded(value)))
        self.assertEqual(result.payload(),value)
        return result

    def test_01_patch_and_all_extract_hashes(self):
        self.assertEqual(hashlib.sha256(PACKAGE.with_suffix('.patch').read_bytes()).hexdigest(),META['patch_sha256'])
        for path,record in META['sources'].items():
            for phase in('before','after'):
                for name in record['extracts_'+phase]:source(path,name,phase)

    def test_02_mapping_order(self):
        self.parity({'z':1,'a':2,'nested':{'b':False,'a':None}})

    def test_03_unicode(self):
        self.parity({'source':'Δ 東京 🦆','escaped':'\n\t\\"'})

    def test_04_nested_provenance(self):
        self.parity({'candles':[{'evidence_json':encoded({'original':'untouched','rows':[1,2,3]})}],'quantity':13})

    def test_05_finite_float_negative_zero(self):
        result=self.parity({'tiny':1e-15,'negative_zero':-0.0,'large':1e30})
        self.assertIn('-0.0',result.payload_json)

    def test_06_list_value(self):
        self.parity([1,'x',None,{'x':[1.2,True]}])

    def test_07_nonfinite_rejection_parity(self):
        for value in(float('nan'),float('inf'),-float('inf')):
            with self.subTest(value=value):
                try:encoded({'x':value})
                except ValueError as old:
                    with self.assertRaisesRegex(type(old),re.escape(str(old))):self.create(self.cls,{'x':value})
                else:self.fail('Baseline accepted nonfinite input')

    def test_08_unsupported_type_rejection(self):
        for value in({'x':object()},{'x':{1,2}}):
            try:encoded(value)
            except TypeError as old:
                with self.assertRaisesRegex(type(old),re.escape(str(old))):self.create(self.cls,value)
            else:self.fail('Baseline accepted unsupported input')

    def test_09_mutation_changes_new_id(self):
        value={'evidence':{'original':[1,2]}}
        first=self.parity(value);value['evidence']['original'].append(3);second=self.parity(value)
        self.assertNotEqual(first.result_id,second.result_id)
        self.assertEqual(first.payload(),{'evidence':{'original':[1,2]}})

    def test_10_sizing_payload_tamper_rejected(self):
        value=self.parity({'status':'sized','desired_quantity':3})
        with self.assertRaisesRegex(ContractError,'Sizing result evidence changed'):
            replace(value,payload_json=value.payload_json.replace(':3',':4')).payload()

    def test_11_indicator_payload_tamper_rejected(self):
        value=self.parity({'candles':[1,2]},self.ns['IndicatorSnapshot'])
        with self.assertRaisesRegex(ContractError,'Indicator snapshot evidence changed'):
            replace(value,payload_json='{}').payload()

    def test_12_returned_payload_mutation_is_separate(self):
        value=self.parity({'evidence':[1]});decoded=value.payload();decoded['evidence'].append(2)
        self.assertEqual(value.payload(),{'evidence':[1]})

    def test_13_encoder_called_once(self):
        calls=[]
        def counted(value):calls.append(value);return encoded(value)
        self.ns['encoded']=counted
        value={'evidence':['exact']};result=self.create(self.cls,value)
        self.assertEqual(calls,[value]);self.assertEqual(result.payload_json,encoded(value))

    def test_14_large_evidence_encodes_once(self):
        calls=[]
        def counted(value):calls.append(len(encoded(value)));return encoded(value)
        self.ns['encoded']=counted
        value={'evidence':'x'*1048576};result=self.create(self.cls,value)
        self.assertEqual(len(calls),1);self.assertEqual(result.payload_json,encoded(value));self.assertEqual(result.result_id,digest(value))

    def test_15_result_subclass_preserved(self):
        class Child(self.cls):pass
        result=self.create(Child,{'x':1})
        self.assertIs(type(result),Child);self.assertEqual(result.result_id,digest({'x':1}))

    def test_16_no_identity_cache(self):
        value={'x':1};first=self.create(self.cls,value);value['x']=2;second=self.create(self.cls,value)
        self.assertNotEqual(first.result_id,second.result_id)

    def test_17_unchanged_indicator_science_and_proof(self):
        old=ast.parse(source(TECH,'indicator_snapshot','before'))
        new=RestoreConstructors().visit(ast.parse(source(TECH,'indicator_snapshot')))
        self.assertEqual(ast.dump(old),ast.dump(new))
        self.assertIn('digest(forecast_snapshot)',source(TECH,'indicator_snapshot'))

    def test_18_unchanged_size_science(self):
        old=ast.parse(source(TECH,'evaluate_size','before'))
        new=RestoreConstructors().visit(ast.parse(source(TECH,'evaluate_size')))
        self.assertEqual(ast.dump(old),ast.dump(new))

    def test_19_unchanged_starter_policy_and_proof(self):
        old=ast.parse(source(START,'size_forecast','before'))
        new=RestoreConstructors().visit(ast.parse(source(START,'size_forecast')))
        self.assertEqual(ast.dump(old),ast.dump(new))
        self.assertIn('digest(forecast_snapshot)',source(START,'size_forecast'))

    def test_20_immutable_value_revalidation_unchanged(self):
        for name in('IndicatorSnapshot','SizingResult'):
            self.assertEqual(source(TECH,name,'before'),source(TECH,name))

    def test_21_exact_matching_local_patch_roundtrip(self):
        patch_text=PACKAGE.with_suffix('.patch').read_text(encoding='utf8')
        for path,record in META['sources'].items():
            local=ROOT/path
            if not local.is_file():continue
            raw=local.read_bytes();text=raw.decode().replace('\r\n','\n');hashvalue=hashlib.sha256(text.encode()).hexdigest()
            if hashvalue==record['before_normalized_sha256']:
                after=apply_unified(text,patch_text,path)
                self.assertEqual(hashlib.sha256(after.encode()).hexdigest(),record['after_normalized_sha256'])
                self.assertEqual(apply_unified(after,patch_text,path,reverse=True),text)
            elif hashvalue==record['after_normalized_sha256']:
                before=apply_unified(text,patch_text,path,reverse=True)
                self.assertEqual(hashlib.sha256(before.encode()).hexdigest(),record['before_normalized_sha256'])
                self.assertEqual(apply_unified(before,patch_text,path),text)
            # Different local composition is not a matching preimage and is never applied.

if __name__=='__main__':
    def denied(*args,**kwargs):
        raise AssertionError('Portable regression forbids network connections')
    socket.create_connection=denied
    socket.socket.connect=denied
    unittest.main(verbosity=2)
