"""Resolve the saved review flag without modifying source/provider audit evidence."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import pandas as pd
OUT = Path(__file__).resolve().parent
read = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
path = OUT/'provider-completion.json'
provider = read(path)
a = provider['optional_advisory_review']
prior_path = Path(a['prior_evidence'])
assert sha(prior_path) == a['prior_evidence_sha256']
prior = read(prior_path)['optional_advisory_review']
assert not provider['issues'] and not provider['pending'] and not a['issues']
assert a['coverage_notes'] == ['cme retains prior diagnostic category/policy, but exact message differs; no unchanged-source claim']
current = a['diagnostics']['cme']['current_rows']
previous = prior['diagnostics']['cme']['current_rows']
assert len(current) == len(previous) == 1
current, previous = current[0], previous[0]
stable_fields = ('symbol', 'source', 'category', 'request_key', 'severity', 'advisory_type',
                 'calculation', 'input_policy', 'provider_rows_preserved')
assert all(current[k] == previous[k] for k in stable_fields)
pattern = r'is stale by (.*?); maximum is'
normalize = lambda s: re.sub(pattern, 'is stale by <age>; maximum is', s)
assert normalize(current['advisory_message']) == normalize(previous['advisory_message'])
assert '2026-09-03T21:00:00+00:00: CME BBO NQ' in current['advisory_message']
age_delta = pd.Timedelta(re.search(pattern, current['advisory_message'])[1])-pd.Timedelta(re.search(pattern, previous['advisory_message'])[1])
receipt_delta = pd.Timestamp(current['fetched_at'])-pd.Timestamp(previous['fetched_at'])
assert abs((age_delta-receipt_delta).total_seconds()) < 1
assert a['fmp_full_validation_matches_prior']
assert a['fmp_full_source_pure_validation'] == prior['fmp_full_source_pure_validation']
assert a['fmp_current_source_pure_validation'] == {'status':'PASS','rows':1}
assert a['fmp_historical_clock_skew_rows'] == prior['fmp_historical_clock_skew_rows']
assert len(a['fmp_historical_clock_skew_rows']) == 5
assert a['diagnostics']['fmp']['exact_message_matches_prior']
assert a['diagnostics']['fmp']['evidence_age'] == 'RETAINED_DIAGNOSTIC'
captures = a['cme_scope_coverage']['captures']
assert len(captures) == 6
assert all(a['cme_scope_coverage']['expected_configuration_checks'].values())
assert not a['cme_scope_coverage']['native_unresolved_symbol_warnings']
summaries = []
for c in captures:
    assert not c['missing_requested_symbols'] and not c['unexpected_symbols']
    token = 'name="CME '+c['scope'].lower()+'_'+c['schema']+'"'
    logs = [line for line in provider['provider_request_completed_lines'] if token in line]
    assert len(logs) == 1 and 'status=ok' in logs[0]
    assert int(re.search(r' rows=(\d+)',logs[0])[1]) == c['current_stage_rows']
    saturated = {row['request_limit_saturated'] for row in c['request_metadata']}
    assert saturated == {c['schema'] == 'mbp-10'}
    if c['schema'] == 'mbp-10':
        assert c['current_stage_rows'] == 5000
    summaries.append({'scope':c['scope'],'schema':c['schema'],'rows':c['current_stage_rows'],
                      'configured_symbols':c['expected_symbols'],'all_configured_symbols_observed':True,
                      'request_limit_saturated':c['schema']=='mbp-10','native_completion_log':logs[0],
                      'request_metadata':c['request_metadata']})
result = {'reviewed_at':datetime.now(timezone.utc).isoformat(),
    'status':'KNOWN_OPTIONAL_LIMITATIONS_REVERIFIED', 'unresolved_review_flags':[],
    'original_provider_disposition':a['disposition'],
    'provider_evidence':{'path':str(path),'sha256':sha(path)},
    'prior_evidence':{'path':str(prior_path),'sha256':sha(prior_path)},
    'cme':{'comparison':'Only staleness age text changed in the advisory; receipt identity/time also advanced. Candidate boundary, stale instrument, tolerance, advisory type and preservation policy match.',
           'current':current,'prior':previous,'age_advance_seconds':age_delta.total_seconds(),
           'receipt_advance_seconds':receipt_delta.total_seconds(),
           'captures':summaries,
           'limitations':['Derived CME cross-asset context remains excluded; current successful raw captures do not validate the stale derived candidate.',
                          'Both MBP captures are explicitly capped at 5000 rows and do not establish complete request-range coverage.',
                          'Continuous and raw contract prices remain distinct; no substituted prices or new provider requests.']},
    'fmp':{'current_capture_status':a['fmp_current_source_pure_validation'],
           'retained_full_history_status':a['fmp_full_source_pure_validation'],
           'unchanged_historical_clock_skew_rows':a['fmp_historical_clock_skew_rows'],
           'limitations':['Current quote passes pure derivation; the whole retained quote history still rejects five September2 rows exceeding the five-second receipt-clock bound.']},
    'decision':'No new runtime defect is demonstrated by the known optional advisory evidence. Preserve raw rows and existing model exclusions; no repair, acquisition or retraining justified by this advisory.',
    'production_writes':0,'provider_calls':0,'orders_placed':0}
(OUT/'provider-advisory-resolution.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'current_cme_captures':len(captures),'unresolved_review_flags':[]}))
