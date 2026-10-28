"""Run isolated offline recovery sizing regressions against this exact source."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile

def main():
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('batched_sizing_isolation',root/'tests/intraday_split_package_support.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    originals=helper.isolate(root)
    attempts=[]
    def denied(*args,**kwargs):
        attempts.append(True)
        raise AssertionError('Sizing regressions prohibit network/provider/broker calls')
    socket.create_connection=socket.getaddrinfo=denied
    socket.socket.connect=socket.socket.connect_ex=socket.socket.sendto=denied
    def boundary(event,args):
        if event in {'subprocess.Popen','os.system','os.spawn','os.posix_spawn'}:
            raise AssertionError('Sizing regressions prohibit child process operations')
    sys.addaudithook(boundary)
    import pytest
    targets=['test_intraday_batched_sizing_headers.py','test_intraday_recovery_batch_preflight.py',
        'test_intraday_durable_sizing_headers.py']
    args=['-q','-p','no:cacheprovider','--confcutdir='+str(root),'--rootdir='+str(root),
        '--basetemp',tempfile.mkdtemp(prefix='ducketz-batched-sizing-'),
        *[str(root/'tests'/name) for name in targets]]
    code=pytest.main(args)
    helper.check_imports(root,originals)
    closure={}
    for name,module in list(sys.modules.items()):
        filename=getattr(module,'__file__',None)
        if not filename:continue
        path=Path(filename).resolve()
        if name.split('.')[0] in {'ml','tools','app','datafetching','tests'} and not path.is_relative_to(root):
            raise AssertionError('Regression imported another checkout')
        if path.is_relative_to(root) and path.suffix=='.py':
            closure[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ['pyproject.toml','tests/conftest.py','tests/intraday_split_package_support.py',
                 'tests/check_intraday_batched_sizing_package.py']:
        path=root/name
        if path.is_file():closure[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    print('BATCHED_SIZING_IMPORT_CLOSURE='+json.dumps(closure,sort_keys=True))
    print('BATCHED_SIZING_RESULT='+json.dumps(dict(exit_code=code,network_attempts=len(attempts),candidate=str(root),members=len(closure))))
    if attempts:raise AssertionError('Offline regression attempted network access')
    return code

if __name__=='__main__':raise SystemExit(main())
