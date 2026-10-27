"""Portable candidate-local SQLite setup checks; no network or child processes."""
from __future__ import annotations
import argparse,ast,hashlib,importlib.util,json,os
from pathlib import Path
import socket,sys


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--basetemp',type=Path);args=parser.parse_args()
 root=Path(__file__).resolve().parents[1]
 # Remove production editable import hooks before collecting any fixture.
 sys.meta_path[:]=[finder for finder in sys.meta_path if not getattr(finder,'__module__','').startswith('__editable___ducketz_')]
 def deny(*args,**kwargs):raise RuntimeError('Storage setup checks prohibit network/provider actions')
 socket.create_connection=socket.getaddrinfo=deny
 socket.socket.connect=socket.socket.connect_ex=socket.socket.sendto=deny
 def boundary(event,args):
  if event in {'socket.connect','socket.sendto','socket.getaddrinfo','socket.gethostbyname','socket.gethostbyaddr','subprocess.Popen','os.system','os.spawn','os.posix_spawn'}:raise RuntimeError('Storage setup checks prohibit network/subprocess actions')
 sys.addaudithook(boundary)
 files=['ml/intraday/renewal_storage.py','tests/test_intraday_renewal_storage.py','tests/check_isolated_intraday_renewal_storage.py','docs/development/intraday-renewal-storage-setup.patch','docs/development/intraday-renewal-storage-setup.json']
 hashes=lambda:{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
 before=hashes();metadata=json.loads((root/files[-1]).read_bytes())
 assert before[files[0]]==metadata['helper_sha256'] and before[files[-2]]==metadata['patch_sha256']
 allowed={'__future__','json','pathlib','sqlite3','traceback'}
 tree=ast.parse((root/files[0]).read_bytes())
 for node in ast.walk(tree):
  if isinstance(node,ast.Import):assert all(item.name in allowed for item in node.names)
  elif isinstance(node,ast.ImportFrom):assert node.module in allowed
 spec=importlib.util.spec_from_file_location('storage_setup_verified_candidate',root/files[0]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 assert Path(module.__file__).resolve()==(root/files[0]).resolve()
 os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
 import pytest
 arguments=['-q','-p','no:cacheprovider',str(root/files[1])]
 basetemp=(root/(args.basetemp or Path('scratch/checks/renewal-storage-fixtures'))).resolve()
 scratch=(root/'scratch').resolve()
 if basetemp==scratch or not basetemp.is_relative_to(scratch):
  raise RuntimeError('Storage setup fixtures require candidate-owned scratch')
 basetemp.parent.mkdir(parents=True,exist_ok=True)
 arguments+=['--basetemp',str(basetemp)]
 result=pytest.main(arguments)
 assert before==hashes(),'Candidate bytes changed during offline checks'
 for imported in list(sys.modules.values()):
  filename=getattr(imported,'__file__',None)
  if filename and Path(filename).resolve()==(root/files[0]).resolve():assert Path(filename).resolve().is_relative_to(root)
 print('INTRADAY_RENEWAL_STORAGE_IMPORT_CLOSURE='+json.dumps(before,sort_keys=True))
 print('Consumer integration remains separate: portable main may not contain installed renewal adapters.')
 return result


if __name__=='__main__':raise SystemExit(main())
