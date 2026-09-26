"""Bounded Pricing-source/gate review while native fitting runs.

Does not invoke the prior completion-only audit, materialize/join data, fit models,
call providers, or write production. Current unpublished route fractions stay pending.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
from ml.horizons import horizon_specifications_for_profile
from ml.runtime_pipeline import (
    _specification_for_pricing_gate, OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
    OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE, OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS,
)

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
RUN=ROOT/'ml/overnight-runs/20260926T040723.834511Z'
PRIOR=Path('C:/dev/ducketz/artifacts/analysis/overnight-20260925/preflight/pricing-gate-review.json')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    issues=[]
    def check(condition,message):
        if not condition:issues.append(message)
    prior=read(PRIOR)
    pointerpath=ROOT/'ml/option-pricing-latest/run.json'
    pointer=read(pointerpath)
    check(pointer==prior['pricing_pointer'],'Current Pricing pointer changed')
    declared=prior['compact_source_comparison']
    if len(declared)!=51 or sum(item['bytes'] for item in declared)>12_000_000:
        raise RuntimeError('PRIOR_COMPACT_AUDIT_BOUND_DIFFERS')
    files=[]
    for item in declared:
        path=Path(item['path']).resolve()
        if not path.is_relative_to((ROOT/'ml/option-pricing-runs').resolve()) or path.stat().st_size>2_000_000:
            raise RuntimeError('COMPACT_SOURCE_PATH_OR_SIZE_BOUND_EXCEEDED')
        actual=sha(path)
        same=actual==item['sha256'] and path.stat().st_size==item['bytes']
        check(same,'Changed compact source: '+str(path))
        files.append({'path':str(path),'bytes':path.stat().st_size,'sha256':actual,'matches_prior':same})
    chain=[]
    authority=pointer['current']
    for _ in range(18):
        if authority is None:break
        path=(ROOT/authority['run_path']).resolve()
        if not path.is_relative_to((ROOT/'ml/option-pricing-runs').resolve()):
            raise RuntimeError('PRICING_AUTHORITY_ESCAPES_ROOT')
        publication=read(path/'publication.json')
        check(sha(path/'publication.json')==authority['receipt_checksum_sha256'],'Pricing publication binding failed')
        check(sha(path/'manifest.json')==authority['manifest_checksum_sha256'],'Pricing manifest binding failed')
        check(publication['run_path']==authority['run_path'] and publication['manifest_checksum_sha256']==authority['manifest_checksum_sha256'],'Pricing publication identity differs')
        chain.append({'run_path':authority['run_path'],'published_at':authority['published_at']})
        authority=publication.get('previous_publication')
    check(authority is None and chain==prior['authority_chain'],'Pricing chain changed')
    logpath=RUN/'loop_b_directional_generation.log'
    data=logpath.read_bytes()
    lines=data.decode('utf-8',errors='replace').splitlines()
    quarantine=[]
    for number,line in enumerate(lines,1):
        match=re.fullmatch(r'\[Loop B/([^]]+)\] Option Pricing family quarantined; fitting (\S+) until the coverage/freshness gate passes \((.*)\)',line)
        if match:
            quarantine.append({'horizon':match[1],'effective_feature_set':match[2],
                               'failed_routes':match[3].split(', '),'log_line_number':number})
    identities=lambda rows:[{key:row[key] for key in ['horizon','effective_feature_set','failed_routes']} for row in rows]
    check(identities(quarantine)==identities(prior['native_quarantine']),'Current quarantine identities/fallback changed')
    routes=[route for item in quarantine for route in item['failed_routes']]
    check(len(quarantine)==9 and len(routes)==len(set(routes))==99,'Expected nine groups and 99 unique routes')
    specs=horizon_specifications_for_profile('loop-a-all-bsgp-active-v3',horizons=[row['horizon'] for row in quarantine])
    contracts={}
    for item in quarantine:
        requested=specs[item['horizon']]
        effective=_specification_for_pricing_gate(requested,gate={'enabled':True,'downstream_training_eligible':False})
        check(effective.feature_set==item['effective_feature_set'],'Native fallback differs from observed log')
        contracts[item['horizon']]={'requested_feature_set':requested.feature_set,
            'effective_feature_set':effective.feature_set,'exact_non_pricing_feature_preservation':'PASS'}
    policy={'version':OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
            'minimum_complete_row_fraction':OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
            'minimum_fresh_joined_row_fraction':OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
            'minimum_distinct_surface_targets':OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS}
    check(policy==prior['gate_policy'],'Native Pricing thresholds changed')
    base=read(ROOT/'.ducketz-loop-a-complete.json')
    check(base['generation']=='20260926T040724.729021Z-pid46624' and base['status']=='COMPLETE','Current source cycle differs')
    starts=[line.removeprefix('LOOP B CYCLE ').strip() for line in lines if line.startswith('LOOP B CYCLE ')]
    check(len(starts)==1,'Expected one current Loop B cycle')
    created=datetime.fromisoformat(starts[0])
    currentrun=ROOT/'ml/runs'/created.strftime('%Y%m%dT%H%M%S.%fZ')
    manifestpath,publicationpath=currentrun/'manifest.json',currentrun/'publication.json'
    gate=None
    binding=None
    group_summary={}
    compact_bound=0
    if manifestpath.is_file() and publicationpath.is_file():
        manifest,publication=read(manifestpath),read(publicationpath)
        check(publication['manifest_checksum_sha256']==sha(manifestpath),'Current Loop B publication binding differs')
        gate=manifest['configuration']['pricing_evidence']
        check(sorted(gate['failed_routes'])==sorted(routes),'Current saved gate routes differ')
        check(gate['downstream_training_eligible'] is False and gate['enabled'] is True,'Current saved gate admission differs')
        check(gate['policy_version']==policy['version'] and gate['thresholds']=={key:value for key,value in policy.items() if key!='version'},'Current saved gate thresholds differ')
        check(gate['model_admission_by_horizon']==prior['current_saved_gate']['model_admission_by_horizon'],'Current saved feature-model admission differs')
        check(datetime.fromisoformat(manifest['configuration']['causal_input_cutoff'].replace('Z','+00:00'))==datetime.fromisoformat(base['finished_at'].replace('Z','+00:00')),'Current saved causal cutoff differs')
        check(manifest['configuration']['route_errors']=={},'Current saved route errors present')
        source_entries={str((ROOT/item['path']).resolve()):item for item in manifest['input_files']}
        for item in files:
            entry=source_entries.get(str(Path(item['path']).resolve()))
            matches=bool(entry and entry.get('checksum_sha256')==item['sha256'] and entry.get('size')==item['bytes'])
            check(matches,'Current Loop B manifest does not bind compact Pricing source: '+item['path'])
            compact_bound+=int(matches)
        for route,report in gate['route_gates'].items():
            for group,values in report['groups'].items():
                expected_pass=(values['complete_row_fraction']>=policy['minimum_complete_row_fraction']
                    and values['fresh_joined_row_fraction']>=policy['minimum_fresh_joined_row_fraction']
                    and values['distinct_surface_targets']>=policy['minimum_distinct_surface_targets'])
                check(values['pass']==expected_pass,'Saved Pricing subfamily gate arithmetic differs: '+route+'/'+group)
                check(values['sample_rows']>0 and math.isclose(values['complete_row_fraction'],values['complete_rows']/values['sample_rows'],rel_tol=0,abs_tol=1e-12)
                    and math.isclose(values['fresh_joined_row_fraction'],values['fresh_joined_rows']/values['sample_rows'],rel_tol=0,abs_tol=1e-12),
                    'Saved Pricing subfamily fractions differ from numerators: '+route+'/'+group)
                summary=group_summary.setdefault(group,{'routes':0,'passing_routes':0,'maximum_complete_fraction':0,'maximum_fresh_joined_fraction':0,'maximum_distinct_targets':0})
                summary['routes']+=1;summary['passing_routes']+=int(values['pass'])
                for key,field in [('maximum_complete_fraction','complete_row_fraction'),('maximum_fresh_joined_fraction','fresh_joined_row_fraction'),('maximum_distinct_targets','distinct_surface_targets')]:
                    summary[key]=max(summary[key],values[field])
            check(report['pass']==all(v['pass'] for v in report['groups'].values()),'Saved Pricing route gate differs: '+route)
        binding={'manifest':str(manifestpath),'manifest_sha256':sha(manifestpath),'publication':str(publicationpath),'publication_sha256':sha(publicationpath),
                 'route_errors':manifest['configuration']['route_errors'],'causal_input_cutoff':manifest['configuration']['causal_input_cutoff']}
    stage=read(RUN/'stage-report.json')
    characteristics=prior['source_characteristics_inherited_only_after_exact_compact_hash_match']
    cutoff=datetime.fromisoformat(base['finished_at'].replace('Z','+00:00'))
    latest=datetime.fromisoformat(characteristics['latest_availability'])
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),
        'status':'REVIEW_REQUIRED' if issues else 'KNOWN_OPTIONAL_PRICING_QUARANTINE_UNCHANGED_AUTHORITY',
        'issues':issues,'scope':'51 compact Pricing surfaces/manifests/publications totaling at most 12 MB, pointer chain, native log snapshot, pure fallback-contract check, and saved current manifest only if published. No fitting, joins, broad archive scan, provider calls or production writes.',
        'native_status_at_audit':{key:stage[key] for key in ['status','current_stage','heartbeat_at']},
        'current_loop_b_run':str(currentrun),'current_loop_b_completion_verified':False,
        'current_loop_b_log':str(logpath),'current_log_snapshot_sha256':hashlib.sha256(data).hexdigest(),
        'pricing_pointer_path':str(pointerpath),'pricing_pointer':pointer,'pointer_matches_prior':pointer==prior['pricing_pointer'],
        'authority_chain':chain,'compact_source_files_matched':sum(item['matches_prior'] for item in files),
        'compact_source_bytes_hashed':sum(item['bytes'] for item in files),'compact_source_comparison':files,
        'source_characteristics_inherited_only_after_exact_compact_hash_match':characteristics,
        'current_causal_cutoff':base['finished_at'],'latest_pricing_availability_age_at_current_cutoff_seconds':(cutoff-latest).total_seconds(),
        'native_quarantine':quarantine,'failed_route_count':len(routes),'effective_feature_contracts':contracts,'gate_policy':policy,
        'materialization_log':[line for line in lines if 'END   loop-b.materialize-samples ' in line],
        'current_saved_gate':gate,'current_saved_gate_binding':binding,'current_group_summary':group_summary,
        'compact_sources_bound_to_current_manifest':compact_bound,
        'saved_route_gate_and_fraction_arithmetic_verified':gate is not None and not issues,
        'current_route_fraction_review':'VERIFIED_SAVED_MANIFEST' if gate is not None else 'PENDING_NATIVE_PUBLICATION_NOT_INFERRED_FROM_PRIOR',
        'prior_route_fraction_reference_only':prior['current_group_summary'],
        'prior_path':str(PRIOR),'prior_sha256':sha(PRIOR),
        'conclusion':'Unchanged sparse August 20 Pricing authority explains the same native optional exclusion. Existing 80% completeness, 80% fresh-join and 20-target gates are preserved; non-Pricing feature fallback remains exact. Fresh OPRA acquisition does not create a new independent Pricing publication. No new Pricing source change or justified fit retry found.'}
    (OUT/'pricing-gate-review.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['status','issues','compact_source_files_matched','compact_source_bytes_hashed','failed_route_count','current_causal_cutoff','latest_pricing_availability_age_at_current_cutoff_seconds','current_route_fraction_review','current_group_summary']},indent=2))


if __name__=='__main__':main()
