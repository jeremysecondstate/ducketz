"""Restore the pre-onboarding, verified September 14 seven-symbol publication."""
import json
from pathlib import Path
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import _write
from datafetching.symbol_universe import read_symbols, REPOSITORY_WATCHLIST
from ml.artifacts import file_checksum, verify_manifest
from ml.nightly_gameplan import read_gameplan_run, read_current_gameplan
from ml.stock_trader.model import load_current_enrichment_model
from app.ui.rolling_forecast_data import load_gameplan_dashboard

root=Path('C:/DATASTORE')
folder=Path(__file__).resolve().parent
batch=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
old=read_gameplan_run(root,root/'ml/nightly-gameplan-runs/20260912T051210.050260Z')
assert old.receipt['action_date']=='2026-09-14'
assert list(old.manifest['configuration']['symbols'])==batch['previous_symbols']
model_run=root/'ml/stock-trader-model-runs/20260912T051308.132302Z'
verify_manifest(model_run)
receipt=json.loads((model_run/'receipt.json').read_text(encoding='utf-8'))
payload=json.loads((model_run/'model.json').read_text(encoding='utf-8'))
assert payload['source_publication']['source_gameplan_run']==old.receipt['run_path']
assert file_checksum(model_run/'model.json')==receipt['model_sha256']
assert file_checksum(model_run/'manifest.json')==receipt['manifest_sha256']
model_pointer={key:receipt[key] for key in ('run_path','trained_at','model_fingerprint','manifest_sha256','model_sha256')}
model_pointer.update(schema_version='stock-trader-enrichment-model-pointer-v1',receipt_sha256=file_checksum(model_run/'receipt.json'))
gameplan_pointer_path=root/'ml/nightly-gameplan-latest/run.json'
model_pointer_path=root/'ml/stock-trader-model-latest/run.json'
trade_path=root/'ml/gameplan-trade-plan-latest/run.json'
trade=json.loads(trade_path.read_text(encoding='utf-8'))['current']
assert trade['run_path']=='ml/gameplan-trade-plan-runs/20260912T051328.518994Z'
assert trade['source_receipt_sha256']==file_checksum(old.run_directory/'receipt.json')
verify_manifest(root/trade['run_path'])
with exclusive_runtime_lock(root/'locks/stock-trader-hourly.lock',process_name='Restore verified onboarding baseline'):
    with exclusive_runtime_lock(root/'.ducketz-nightly-gameplan.lock',process_name='Restore verified Gameplan'):
        assert list(read_symbols(REPOSITORY_WATCHLIST))==batch['previous_symbols']
        current=json.loads(gameplan_pointer_path.read_text(encoding='utf-8'))
        current_model=json.loads(model_pointer_path.read_text(encoding='utf-8'))
        already_restored = current == old.pointer and current_model == model_pointer
        if not already_restored:
            assert current['current']['run_path']=='ml/nightly-gameplan-runs/20260914T104432.295244Z'
            assert current_model['run_path']=='ml/stock-trader-model-runs/20260914T104613.232728Z'
            _write(folder/'candidate-current-pointers.json',{'gameplan':current,'model':current_model})
            _write(model_pointer_path,model_pointer)
            _write(gameplan_pointer_path,old.pointer)
        verified=read_current_gameplan(root)
        model=load_current_enrichment_model(root)
        assert verified.run_directory==old.run_directory
        assert model.model_fingerprint==receipt['model_fingerprint']
view=load_gameplan_dashboard(gameplan_pointer_path)
assert len(view.symbols)==7 and view.source_row_count==168
assert {s.symbol for s in view.pending_symbols} == set(batch['selected_symbols'])
result={'status':'VERIFIED_BASELINE_RESTORED','plan_id':batch['plan_id'],'production_symbols':batch['previous_symbols'],
        'gameplan_run':str(old.run_directory),'model_run':str(model_run),'trade_plan_run':trade['run_path'],
        'dashboard_rows':view.source_row_count,'pending_symbols':[s.symbol for s in view.pending_symbols], 'orders_placed':0,
        'pointer_sha256':{str(p.relative_to(root)):file_checksum(p) for p in (gameplan_pointer_path,model_pointer_path,trade_path)}}
_write(folder/'production-restoration.json',result)
print(json.dumps(result,indent=2))
