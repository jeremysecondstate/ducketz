"""Durable non-trading preparation from saved causal candles and provider receipts.

This entry point has no account, order, policy-promotion or manual-stop API.
The ordinary market collector and closed-minute feed may supply histories; a
prepared matrix is never an executable forecast or qualification decision.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import uuid

from filelock import FileLock
import numpy as np
import pandas as pd

from .prediction import ContractError, instant

VERSION = 'intraday-causal-preparation-v2'
SOURCE_FILES = ('data.py', 'observed_data.py', 'native_interval.py', 'native_recovery.py',
                'live_model_recipe.py', 'models.py', 'feature_support.py', 'market_context.py',
                'market_context_cache.py', 'market_collection_index.py', 'preparation.py',
                'preparation_watch.py', 'training_timing.py', 'market_state.py', 'assessment_reservation.py')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _sources():
    root = Path(__file__).parent
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _matrix_hash(*arrays):
    digest = hashlib.sha256()
    for array in arrays:
        value = np.asarray(array)
        digest.update(_json({'shape': list(value.shape), 'dtype': str(value.dtype)}).encode())
        digest.update(np.ascontiguousarray(value).tobytes())
    return digest.hexdigest()


def _write_once(path, payload):
    raw = (_json(payload)+'\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ContractError('Immutable preparation identity changed')
        return
    # Publish only a complete durable file. The caller holds the cycle lock.
    temporary = path.with_suffix(path.suffix + '.pending')
    with temporary.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _verified(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('sha256') != _digest({k:v for k,v in value.items() if k != 'sha256'}):
        raise ContractError('Preparation evidence checksum mismatch')
    return value


def _complete_fit(saved):
    artifacts = saved.get('artifacts')
    return (saved.get('complete') is True and isinstance(artifacts, list) and bool(artifacts)
            and all(a.get('status') == 'success' and isinstance(a.get('artifact_path'), str)
                    and bool(a['artifact_path']) and isinstance(a.get('artifact_sha256'), str)
                    and len(a['artifact_sha256']) == 64 for a in artifacts))


def _verify_artifacts(saved):
    if not _complete_fit(saved):
        raise ContractError('Prepared fit has incomplete or failed model artifacts')
    for artifact in saved['artifacts']:
        if hashlib.sha256(Path(artifact['artifact_path']).read_bytes()).hexdigest() != artifact['artifact_sha256']:
            raise ContractError('Prepared model artifact changed')


def prepare_cycle(*, datastore, pc, symbols, histories, reference_at, cutoff,
                  config=None, model_runtime=None, fit=False, output_root=None, history_errors=None,
                  reference_by_symbol=None):
    """Prepare one exact reference; retry/restart reuse exact immutable inputs.

    All symbol failures are recorded independently. Newly mature labels change
    the training key. Identical training bytes reuse fitted artifact evidence,
    never its old inference probability. A changed source contract invalidates
    reuse. No historical acquisition/receipt clock is modified here.
    """
    from .runtime import ResearchConfig
    from .live_model_recipe import prepare_manual_models
    config = config or ResearchConfig()
    if model_runtime is None:
        config, model_runtime = prepare_manual_models(config, datastore=datastore, pc=pc, symbols=symbols)
    if not model_runtime or 'dataset_builder' not in model_runtime:
        raise ContractError('Non-trading preparation requires an explicit local frozen recipe')
    reference, cutoff = instant(reference_at), instant(cutoff)
    if cutoff < reference:
        raise ContractError('Preparation cutoff precedes reference')
    selected_references={symbol:instant(value) for symbol,value in (reference_by_symbol or {}).items()}
    if set(selected_references)-set(symbols) or any(value>reference for value in selected_references.values()):
        raise ContractError('Preparation references must belong to their expected local scope and cursor')
    root = Path(output_root) if output_root else Path(datastore)/'intraday'/pc.lower()/'preparation'
    root.mkdir(parents=True, exist_ok=True)
    sources = _sources()
    result = {'schema_version': VERSION, 'pc': pc, 'symbols': list(symbols),
              'reference_at':reference.isoformat(), 'cutoff':cutoff.isoformat(),
              'source_sha256':sources, 'trading':False, 'qualification':'not_claimed', 'results':{},
              'reference_by_symbol':{s:v.isoformat() for s,v in selected_references.items()}}
    with FileLock(str(root/'preparation.lock'), timeout=0):
        for symbol in symbols:
            missing_selected=reference_by_symbol is not None and symbol not in selected_references
            selected_reference=selected_references.get(symbol,reference)
            item = {'symbol':symbol,'reference_at':None if missing_selected else selected_reference.isoformat(),
                    'expected_reference_at':reference.isoformat(),
                    'missing_expected_reference':missing_selected or selected_reference!=reference,
                    'no_eligible_saved_reference':missing_selected}
            result['results'][symbol] = item
            try:
                if symbol in (history_errors or {}):
                    item.update(status='unavailable', **history_errors[symbol])
                    continue
                if missing_selected:
                    item.update(status='unavailable',stage='saved_reference',
                                reason='No eligible completed saved native reference')
                    continue
                history = histories[symbol]
                frame = history.frame if hasattr(history, 'frame') else history
                selected_rows=frame.loc[pd.to_datetime(frame.opened_at,utc=True).eq(pd.Timestamp(selected_reference))]
                if len(selected_rows):
                    item['reference_availability_kind']=str(selected_rows.iloc[-1].availability_kind)
                builder=model_runtime['dataset_builder']
                builder=getattr(builder,'for_preparation',builder)
                dataset = builder(frame, reference_at=selected_reference, cutoff=cutoff,
                    lookback_days=config.lookback_days, synthetic_policy='exclude')
                recipe = dataset.manifest['manual_model_recipe']
                training = {'recipe_sha256':recipe['sha256'], 'symbol':symbol, 'source_sha256':sources,
                    'training_timing_contract':dataset.manifest.get('training_timing_contract'),
                    'feature_names':list(dataset.feature_names),
                    'matrix_sha256':_matrix_hash(dataset.X, dataset.y),
                    'references':[str(x) for x in dataset.reference_at],
                    'label_available_at':[str(x) for x in dataset.label_available_at],
                    'target_close_at':[str(x) for x in dataset.target_close_at],
                    'context_training_sha256':dataset.manifest.get('market_context',{}).get('training_sha256')}
                training_key = _digest(training)
                item.update(status='prepared', training_key=training_key, recipe_sha256=recipe['sha256'],
                    training_timing_contract=training['training_timing_contract'],
                    training_rows=len(dataset.y), feature_count=len(dataset.feature_names),
                    class_counts={str(int(v)):int(np.sum(dataset.y==v)) for v in np.unique(dataset.y)},
                    latest_mature_label=str(max(dataset.label_available_at)),
                    training_support=dataset.manifest.get('manual_recipe_training_support'),
                    context_training_rows=dataset.manifest.get('market_context',{}).get('training_rows_with_source',0),
                    inference_matrix_sha256=_matrix_hash(dataset.inference_X),
                    inference_features=np.asarray(dataset.inference_X).tolist(),
                    inference_snapshot_sha256=dataset.inference['snapshot_sha256'],
                    preparation_only=dataset.manifest.get('preparation_only',False),
                    missing_inputs=[{'source_id':e.get('source_id'), 'status':e.get('status'),
                        'records_used':e.get('records_used',0), 'exclusions':e.get('exclusions',{})}
                        for e in dataset.manifest.get('market_context',{}).get('inference_sources',[])
                        if not e.get('records_used') or e.get('status') != 'COMPLETE'])
                if selected_reference!=reference:
                    item['missing_inputs'].append({'source_id':'databento:EQUS.MINI:ohlcv-1m',
                        'status':'missing_expected_reference','expected_reference_at':reference.isoformat(),
                        'prepared_reference_at':selected_reference.isoformat(),
                        'reason':'Prepared latest genuine saved observation; no price or executable target was shifted'})
                training_path = root/'training'/(training_key+'.json')
                _write_once(training_path, {**training, 'sha256':training_key})
                fit_path = root/'fits'/(training_key+'.complete.json')
                if fit:
                    # Preserve older failed receipts in place. Only a complete
                    # successful artifact set is an idempotent fit cache entry.
                    existing = fit_path if fit_path.exists() else root/'fits'/(training_key+'.json')
                    saved = _verified(existing) if existing.exists() else None
                    if saved is not None and saved['training_key'] != training_key:
                        raise ContractError('Saved fit belongs to another training population')
                    if saved is not None and _complete_fit(saved):
                        _verify_artifacts(saved)
                        item['fit'] = {'status':'reused_verified_training_artifacts', 'evidence_path':str(existing)}
                    else:
                        fitted = model_runtime['trainer'](dataset.X, dataset.y, dataset.inference_X,
                            config=config.models, dataset_metadata=dataset.manifest,
                            target_version=dataset.manifest['target_version'], training_cutoff=cutoff,
                            label_available_at=dataset.label_available_at, reference_at=dataset.reference_at,
                            target_close_at=dataset.target_close_at, feature_names=dataset.feature_names,
                            **({'preparation_only':True} if dataset.manifest.get('preparation_only') else {}),
                            artifact_dir=root/'models', run_id=symbol+'-'+training_key+'-'+uuid.uuid4().hex)
                        artifacts = [{k:o.get(k) for k in ('name','status','artifact_path','artifact_sha256','error')}
                            for o in fitted['outputs']]
                        saved = {'training_key':training_key, 'artifacts':artifacts,
                                 'complete':fitted.get('complete'), 'qualification':'not_claimed'}
                        saved['sha256'] = _digest(saved)
                        attempt_path = root/'fit-attempts'/training_key/(saved['sha256']+'.json')
                        _write_once(attempt_path, saved)
                        if _complete_fit(saved):
                            _verify_artifacts(saved)
                            _write_once(fit_path, saved)
                            item['fit'] = {'status':'fitted', 'evidence_path':str(fit_path)}
                        else:
                            item.update(status='fit_incomplete')
                            item['fit'] = {'status':'incomplete', 'evidence_path':str(attempt_path),
                                           'retryable':True}
                else:
                    item['fit'] = {'status':'not_requested'}
            except Exception as exc:
                item.update(status='unavailable', error_type=type(exc).__name__, reason=str(exc))
                # Population/contract rejection needs changed inputs. Other
                # failures may be temporary I/O or estimator execution errors;
                # the watcher bounds retries and retains every failed attempt.
                item['retryable']=type(exc).__name__ not in {
                    'ContractError','InsufficientTrainingData','UnresolvedPolicy',
                    'ManualRecipeSupportError','ValueError','KeyError'}
                failed=getattr(exc,'result',None)
                if isinstance(failed,dict) and failed.get('outputs'):
                    item['model_failures']=[o.get('error') for o in failed['outputs'] if o.get('status')!='success']
                    item['retryable']=any(e and e.get('type') not in {'ContractError','ValueError','InsufficientTrainingData'}
                                          for e in item['model_failures'])
        if _sources() != sources:
            raise ContractError('Preparation implementation changed during the cycle')
        result['sha256'] = _digest(result)
        path = root/'cycles'/(result['sha256']+'.json')
        _write_once(path, result)
    return {**result, 'evidence_path':str(path)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--datastore', type=Path, required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--reference-at')
    mode.add_argument('--run', action='store_true', help='Continuously prepare new saved quarter observations; no acquisition or trading')
    parser.add_argument('--cutoff', help='Actual preparation time; defaults to now')
    parser.add_argument('--fit', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=15)
    args=parser.parse_args(argv)
    if args.run and args.cutoff:
        parser.error('--run uses actual current time; --cutoff is only for an explicit saved reference')
    from .runtime import ResearchConfig, local_symbols
    from .live_model_recipe import prepare_manual_models
    from .native_interval import load_native_interval_history
    pc,symbols=local_symbols(args.repository/'scratch/cross-pc/local-profile.json',
                            args.repository/'datafetching/watchlist.local.txt',args.datastore)
    config, runtime=prepare_manual_models(ResearchConfig(),datastore=args.datastore,pc=pc,symbols=symbols)
    recovery=getattr(runtime.get('dataset_builder'),'historical_recovery_contract',None)
    histories, errors = {}, {}
    for symbol in symbols:
        try:
            histories[symbol]=load_native_interval_history(args.datastore,symbol,
                source_dataset=config.source_dataset,pc=pc,recovery_contract=recovery)
        except Exception as exc:
            errors[symbol]={'error_type':type(exc).__name__,'reason':str(exc),'stage':'load_history'}
    if args.run:
        from .preparation_watch import run_preparation
        try:
            return run_preparation(datastore=args.datastore,pc=pc,symbols=symbols,histories=histories,
                config=config,model_runtime=runtime,fit=args.fit,history_errors=errors,
                poll_seconds=args.poll_seconds,emit=lambda result:print(json.dumps(result),flush=True))
        except KeyboardInterrupt:
            return {'status':'stopped','trading':False}
    print(json.dumps(prepare_cycle(datastore=args.datastore,pc=pc,symbols=symbols,histories=histories,
        reference_at=args.reference_at,cutoff=args.cutoff or datetime.now(timezone.utc),
        config=config,model_runtime=runtime,fit=args.fit,history_errors=errors),indent=2))


if __name__ == '__main__':
    main()
