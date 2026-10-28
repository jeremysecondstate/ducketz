"""Portable forecast-cache outcome checks; installed consumer integrations stay separate."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--basetemp',type=Path)
    args=parser.parse_args()
    candidate=Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode=True
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    original_roots=set(); finders=[]
    for finder in sys.meta_path:
        name=getattr(finder,'__module__','')
        if name.startswith('__editable___ducketz_') and name.endswith('_finder'):
            mapping=getattr(sys.modules.get(name),'MAPPING',{})
            original_roots.update(Path(p).resolve().parent for p in mapping.values())
        else: finders.append(finder)
    sys.meta_path[:]=finders
    original_roots.discard(candidate)
    paths=[]
    for entry in sys.path:
        path=Path(entry or '.').resolve()
        collision=(path.name=='cross_pc' and (path/'test_runner.py').is_file() and (path/'requests.py').is_file())
        if path not in original_roots|{candidate} and not collision:paths.append(entry)
    sys.path[:]=[str(candidate),*paths]
    attempts=[]
    def denied(*args,**kwargs):
        attempts.append('network');raise RuntimeError('Forecast outcome checks are offline')
    socket.create_connection=denied
    socket.socket.connect=socket.socket.connect_ex=socket.socket.sendto=denied
    def boundary(event,args):
        if event in {'socket.connect','socket.sendto','socket.getaddrinfo','socket.gethostbyname',
                     'socket.gethostbyaddr','subprocess.Popen','os.system','os.spawn','os.posix_spawn'}:
            attempts.append(event);raise RuntimeError('Forecast outcome checks prohibit external operations')
    sys.addaudithook(boundary)
    scratch=(candidate/'scratch').resolve()
    basetemp=(args.basetemp or scratch/'checks/forecast-cache-outcomes').resolve()
    if basetemp==scratch or not basetemp.is_relative_to(scratch):
        raise RuntimeError('Fixture path must be candidate-owned scratch')
    basetemp.parent.mkdir(parents=True,exist_ok=True)
    import pytest
    result=pytest.main(['-q','--noconftest','-p','no:cacheprovider','--rootdir',str(candidate),
        '--basetemp',str(basetemp),str(candidate/'tests/test_intraday_partial_header_reads.py'),
        str(candidate/'tests/test_intraday_durable_forecast_diagnostics.py')])
    closure={}
    for module in list(sys.modules.values()):
        filename=getattr(module,'__file__',None)
        if not filename:continue
        path=Path(filename).resolve()
        if path.is_relative_to(candidate) and path.suffix=='.py' and '.venv' not in path.parts:
            closure[path.relative_to(candidate).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
        if '.venv' not in path.parts and any(path.is_relative_to(root) for root in original_roots):
            raise RuntimeError('Application import escaped the candidate')
    for name in ('ml/intraday/forecast_header_cache.py','tests/test_intraday_partial_header_reads.py',
                 'tests/test_intraday_durable_forecast_diagnostics.py',
                 'tests/check_isolated_intraday_durable_forecast_diagnostics.py'):
        closure[name]=hashlib.sha256((candidate/name).read_bytes()).hexdigest()
    manifest=candidate/'docs/development/patches/intraday-durable-forecast-diagnostics.json'
    if manifest.exists():
        package=json.loads(manifest.read_bytes())
        patch=candidate/package['patch']
        if hashlib.sha256(patch.read_bytes()).hexdigest()!=package['patch_sha256']:
            raise RuntimeError('Consumer patch bytes changed')
        closure[package['patch']]=package['patch_sha256']
        closure[manifest.relative_to(candidate).as_posix()]=hashlib.sha256(manifest.read_bytes()).hexdigest()
    print('FORECAST_CACHE_OUTCOME_CHECK='+json.dumps(dict(exit_code=result,
        network_attempts=len(attempts),import_closure=closure,
        integration_scope='Portable cache only; Atlas full installed consumer checks recorded separately'),sort_keys=True))
    if attempts:raise RuntimeError('Forbidden external operation attempted')
    return result

if __name__=='__main__':raise SystemExit(main())
