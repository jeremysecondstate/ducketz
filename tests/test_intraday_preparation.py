"""Non-trading preparation is durable, independent per symbol and idempotent."""
from pathlib import Path
import hashlib
import json

import pandas as pd
import numpy as np
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.preparation import prepare_cycle, _matrix_hash, _digest
from ml.intraday.runtime import ResearchConfig
from tests.test_intraday_market_recipe import candidate, frame, save


def test_successive_quarters_new_labels_restart_and_exact_fit_reuse(tmp_path):
    save(tmp_path, candidate())
    config, runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    calls=[]
    def trainer(X,y,inference_X,**kwargs):
        calls.append((len(y),kwargs['run_id']))
        path=Path(kwargs['artifact_dir'])/(kwargs['run_id']+'.bin')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'offline-test-model')
        return {'complete':True,'outputs':[{'name':'fixture','status':'success','artifact_path':str(path),
            'artifact_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}
    runtime={**runtime,'trainer':trainer}
    results=[]
    for opened in data.opened_at.iloc[-3:]:
        ref=pd.Timestamp(opened)
        results.append(prepare_cycle(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
            reference_at=ref,cutoff=ref+pd.Timedelta(minutes=16),config=config,model_runtime=runtime,fit=True))
    assert len(calls)==3
    rows=[r['results']['TEST']['training_rows'] for r in results]
    assert rows[0]<rows[1]<rows[2]
    labels=[r['results']['TEST']['latest_mature_label'] for r in results]
    assert labels[0]<labels[1]<labels[2]
    assert all(r['results']['TEST']['missing_inputs'] for r in results)
    for result in results:
        saved = result['results']['TEST']
        assert _matrix_hash(np.asarray(saved['inference_features'])) == saved['inference_matrix_sha256']
    # A fresh adapter/process uses immutable training evidence and checks model bytes.
    _,restarted=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    restarted={**restarted,'trainer':trainer}
    again=prepare_cycle(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        reference_at=ref,cutoff=ref+pd.Timedelta(minutes=16),config=config,model_runtime=restarted,fit=True)
    assert len(calls)==3
    assert again['results']['TEST']['fit']['status']=='reused_verified_training_artifacts'
    assert again['results']['TEST']['training_key']==results[-1]['results']['TEST']['training_key']
    assert json.loads(Path(again['evidence_path']).read_text())['trading'] is False
    assert not (tmp_path/'intraday/shadow.sqlite3').exists()


def test_missing_symbol_persists_failure_without_blocking_other_symbol(tmp_path):
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    ref=pd.Timestamp(data.opened_at.iloc[-1])
    result=prepare_cycle(datastore=tmp_path,pc='Scout',symbols=('MISSING','TEST'),histories={'TEST':data},
        reference_at=ref,cutoff=ref+pd.Timedelta(minutes=16),config=config,model_runtime=runtime)
    assert result['results']['MISSING']['status']=='unavailable'
    assert result['results']['TEST']['status']=='prepared'
    assert Path(result['evidence_path']).is_file()


@pytest.mark.parametrize('declared_complete', [False, True])
def test_incomplete_fit_is_recorded_retried_and_never_reused(tmp_path, declared_complete):
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    ref=pd.Timestamp(data.opened_at.iloc[-1])
    calls=[]
    def trainer(*args, **kwargs):
        calls.append(kwargs['run_id'])
        if len(calls)==1:
            return {'complete':declared_complete,'outputs':[{'name':'fixture','status':'failed',
                'artifact_path':None,'artifact_sha256':None,'error':{'phase':'persistence','message':'temporary error'}}]}
        path=Path(kwargs['artifact_dir'])/(kwargs['run_id']+'.bin')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'recovered-model')
        return {'complete':True,'outputs':[{'name':'fixture','status':'success','artifact_path':str(path),
            'artifact_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}
    options=dict(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},reference_at=ref,
        cutoff=ref+pd.Timedelta(minutes=16),config=config,model_runtime={**runtime,'trainer':trainer},fit=True)
    failed=prepare_cycle(**options)['results']['TEST']
    assert failed['status']=='fit_incomplete' and failed['fit']['status']=='incomplete'
    failure_bytes=Path(failed['fit']['evidence_path']).read_bytes()
    assert json.loads(failure_bytes)['artifacts'][0]['error']['message']=='temporary error'
    # Preserve and tolerate old-version incomplete fit cache files too.
    legacy=tmp_path/'intraday/scout/preparation/fits'/(failed['training_key']+'.json')
    legacy.parent.mkdir(parents=True,exist_ok=True)
    legacy.write_bytes(failure_bytes)
    recovered=prepare_cycle(**options)['results']['TEST']
    assert recovered['status']=='prepared' and recovered['fit']['status']=='fitted'
    assert len(calls)==2 and calls[0]!=calls[1]
    assert Path(failed['fit']['evidence_path']).read_bytes()==legacy.read_bytes()==failure_bytes
    reused=prepare_cycle(**options)['results']['TEST']
    assert reused['fit']['status']=='reused_verified_training_artifacts' and len(calls)==2


def test_cli_one_history_failure_still_prepares_other_symbol(tmp_path, monkeypatch, capsys):
    from ml.intraday import preparation, runtime as runtime_module, native_interval
    save(tmp_path,candidate())
    config,adapters=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    ref=pd.Timestamp(data.opened_at.iloc[-1])
    monkeypatch.setattr(runtime_module,'local_symbols',lambda *args:('Scout',('BROKEN','TEST')))
    monkeypatch.setattr(recipes,'prepare_manual_models',lambda *args,**kwargs:(config,adapters))
    def loader(root,symbol,**kwargs):
        if symbol=='BROKEN':
            raise ValueError('Immutable receipt hash changed')
        return data
    monkeypatch.setattr(native_interval,'load_native_interval_history',loader)
    preparation.main(['--datastore',str(tmp_path),'--reference-at',str(ref),
                      '--cutoff',str(ref+pd.Timedelta(minutes=16))])
    result=json.loads(capsys.readouterr().out)
    assert result['results']['BROKEN']['status']=='unavailable'
    assert result['results']['BROKEN']['stage']=='load_history'
    assert result['results']['BROKEN']['reason']=='Immutable receipt hash changed'
    assert result['results']['TEST']['status']=='prepared'
    assert Path(result['evidence_path']).is_file()
