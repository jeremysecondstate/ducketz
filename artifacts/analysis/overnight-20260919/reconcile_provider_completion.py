"""Reconcile terminal metadata only, preserving the completed 66-file hash audit."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

OUT = Path(__file__).resolve().parent
RUN = Path('C:/DATASTORE/ml/overnight-runs/20260919T040748.657031Z')
HEALTH = Path('C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/health/current.json')
r = json.loads((OUT / 'provider-completion.json').read_text())
assert not r['issues'] and r['current_data_files_hashed'] == 66
assert all(all(p['checks'].values()) and len(p['actual_data_hashes']) == 2 for p in r['current_partitions'])
stage = json.loads((RUN / 'stage-report.json').read_text())
native = next(s for s in stage['stages'] if s['stage'] == 'loop_a_close_fetch')
assert native['status'] == 'COMPLETE' and native['exit_code'] == 0 and native['error'] is None
assert stage['owner_pid'] == 43916 and stage['deadline_at'] == '2026-09-21T11:00:00+00:00'
assert not stage['broker_orders_enabled'] and stage['orders_placed'] == 0
log_bytes = (RUN / 'loop_a_close_fetch.log').read_bytes()
lines = log_bytes.decode().splitlines()
summary = [line for line in lines if line.startswith('Options history maintenance finished:')]
assert len(summary) == 1
fields = dict(part.strip().split('=', 1) for part in summary[0].split(': ', 1)[1].split(';'))
expected = {'requested_scopes': '33', 'completed_scopes': '33', 'capacity_blocked_scopes': '0', 'failed_scopes': '0', 'bootstrap_required_scopes': '0', 'preflighted_scopes': '33', 'deferred_scopes': '0', 'live_replay_completed_scopes': '0', 'live_replay_bytes': '0', 'selected_estimated_download_bytes': str(r['observed_estimated_download_bytes']), 'selected_estimated_cost_usd': '0.0'}
assert fields == expected
exits = [line for line in lines if line.startswith('Loop A OPRA Strategy history maintenance:')]
assert exits == ['Loop A OPRA Strategy history maintenance: exit_code=0; attempt_date=2026-09-19']
health_bytes = HEALTH.read_bytes()
health = json.loads(health_bytes)
assert health['provider'] == 'databento-opra' and health['dataset'] == 'OPRA.PILLAR'
assert '2026-09-19T04:56:53' < health['observed_at'] < native['finished_at']
assert all(health[top] == sum(s.get(field, 0) for s in health['schemas'].values()) for top, field in [('total_partitions', 'partition_count'), ('total_rows', 'row_count'), ('total_raw_bytes', 'raw_bytes'), ('total_parquet_bytes', 'parquet_bytes')])
snapshots = [json.loads(line) for line in (RUN / 'health.jsonl').read_text().splitlines()]
active = [s for s in snapshots if s.get('stage') == 'loop_a_close_fetch' and s.get('log_bytes') == 118842 and s.get('process_count') == 5 and s.get('cpu_seconds')][-4:]
assert len(active) == 4 and active[-1]['cpu_seconds'] > active[0]['cpu_seconds'] and active[-1]['io_bytes'] > active[0]['io_bytes']
phase = r.get('native_health_phase_review', {})
phase.update(conclusion='EXPECTED_NATIVE_HEALTH_REBUILD_COMPLETED_WITHOUT_RESTART', recent_health_snapshots=active, cpu_delta_seconds=active[-1]['cpu_seconds'] - active[0]['cpu_seconds'], io_delta_bytes=active[-1]['io_bytes'] - active[0]['io_bytes'], native_health_observed_at=health['observed_at'], maintenance_terminal_present=True, refresh_terminal_present=True, counter_transition_note='After the options_history child exits, summed live-descendant CPU and I/O decrease. That drop is a process-set change, not reversed work or a failed/stalled rebuild.')
r.update(status='PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE', pending=[], completed_stage_reconciled_at=datetime.now(timezone.utc).isoformat(), stage_snapshot=stage, loop_a_stage_completion=native, native_maintenance_summary=summary, native_exit_summary=exits, native_health_phase_review=phase, health_inventory={'path': str(HEALTH), 'payload': health, 'fresh_current_run': True, 'inventory_totals_consistent': True, 'sha256_at_completion': hashlib.sha256(health_bytes).hexdigest()}, log_sha256_at_completion=hashlib.sha256(log_bytes).hexdigest(), completion_reconciliation_scope='Read only final native log/stage report, small health JSON and native health metrics. Preserved the original 04:57:26 current-session 66-file hash results; no archive rescan, data reread or provider/process action.')
(OUT / 'provider-completion.json').write_text(json.dumps(r, indent=2) + '\n')
md = (OUT / 'provider-completion.md').read_text()
md = md.replace('Status: **PENDING_NATIVE_PROVIDER_COMPLETION**.', 'Status: **PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE**.')
md = md.replace('Native Loop A stage completion: **PENDING**. Native OPRA terminal summary: **PENDING**. Health inventory freshness: **False**; inventory totals consistent: **True**. Pending native work is not a provider failure.', f"Native Loop A stage is **COMPLETE, exit 0**, at **{native['finished_at']}**. The final OPRA summary verifies **33 requested/preflighted/completed**, zero capacity-blocked/failed/bootstrap-required/deferred scopes, zero Live replay scopes/bytes, the same **4,875,287,440-byte** estimate and exactly **$0** selected estimated cost. Native maintenance exits 0 for September 19.")
md = md.replace('Outstanding evidence checks: **2**.', 'Outstanding provider evidence checks: **0**.')
md = md.replace('Prior optional advisory evidence is retained at [provider-preflight-review.md](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md). The previous CME stale partition context and FMP historical clock-skew rejections do not establish a new defect; current evidence must still be compared before claiming unchanged status.', 'Current optional advisories were reconciled against [prior evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md); the detailed current review below establishes the repeated limitations with unchanged native gates.')
md = md.split('## Native health phase observation')[0].rstrip()
md += '\n\n## Native health completion\n\n'
md += f"Terminal reconciliation at **{r['completed_stage_reconciled_at']}** preserves the original **{r['hash_verification_observed_at']}** verification of the 66 current-session data files. The pipeline has advanced to **{stage['current_stage']}** under the same owner and original September 21 04:00 Pacific deadline; this report establishes provider-stage completion, not whole-overnight completion.\n\n"
md += f"Fresh native health is observed at **{health['observed_at']}** and contains **{health['total_partitions']:,} selected verified partitions**, **{health['total_rows']:,} rows**, **{health['total_parquet_bytes']:,} normalized bytes** and **{health['total_raw_bytes']:,} raw bytes**. Schema sums match all four totals. Production schema counts are " + '; '.join(f"`{s}`: **{health['schemas'][s]['partition_count']:,} partitions / {health['schemas'][s]['row_count']:,} rows**" for s in ['ohlcv-1h', 'cbbo-1m', 'definition']) + '.\n\n'
md += f"Before completion, exact native process lineage and a retained OPRA file handle supported the expected [health refresh call](C:/dev/ducketz/datafetching/options_runtime.py:1249). Last active-child snapshots `{active[0]['heartbeat_at']}` through `{active[-1]['heartbeat_at']}` show CPU advancing **{phase['cpu_delta_seconds']:.3f} seconds** and I/O **{phase['io_delta_bytes']:,} bytes**, with zero issues. Native [publish_health](C:/dev/ducketz/datafetching/databento_opra_history.py:903) atomically publishes only after iterating/validating its selected inventory, explaining the quiet log and unchanged previous health JSON during that phase. Descendant-summed counters decrease after child exit; that is an expected process-set transition. No restart was needed.\n\n"
md += 'The health inventory omits skipped/invalid retained directories and is not proof that every archive directory is valid. Final reconciliation read only terminal log/stage metadata, the small health JSON and recorded process metrics. It performed no whole-archive scan or repeated data hashes, made no provider/broker calls, and preserved source files, quality gates and all prior advisory evidence.\n'
(OUT / 'provider-completion.md').write_text(md)
print(json.dumps({'status': r['status'], 'reconciled_at': r['completed_stage_reconciled_at'], 'loop_a_finished_at': native['finished_at'], 'health_observed_at': health['observed_at'], 'health_total_partitions': health['total_partitions'], 'health_total_rows': health['total_rows'], 'next_stage': stage['current_stage'], 'issues': r['issues']}, indent=2))
