"""Read-only audit of saved COST probes; no provider requests/publication."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sys
import databento as db
import pandas as pd

OUT = Path(__file__).resolve().parent
START = pd.Timestamp('2026-09-10T23:30:00Z')
END = pd.Timestamp('2026-09-11T00:00:00Z')
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def historical(path, dataset):
    store = db.DBNStore.from_file(path)
    try:
        m = store.metadata
        assert m.dataset == dataset and str(m.schema) == 'ohlcv-1m'
        assert m.start == START.value and m.end == END.value
        assert str(m.stype_in) == 'raw_symbol' and tuple(m.symbols) == ('COST',)
        assert not m.partial and not m.not_found
        frame = store.to_df(schema='ohlcv-1m', map_symbols=True, price_type='float')
        if len(frame):
            assert set(frame.symbol) == {'COST'}
            assert frame.index.min() >= START and frame.index.max() < END
        return {'status': 'COMPLETE', 'dataset': dataset, 'rows': len(frame),
                'bar_starts': [t.isoformat() for t in frame.index], 'raw_sha256': sha(path)}
    finally:
        store.reader.close()

result = {'verified_at': datetime.now(timezone.utc).isoformat(),
          'symbol': 'COST', 'session': '2026-09-10', 'schema': 'ohlcv-1m',
          'start': START.isoformat(), 'end': END.isoformat(), 'errors': []}
result['xnas_historical'] = historical(OUT/'historical.dbn', 'XNAS.ITCH')
original = pd.read_parquet(OUT/'historical.parquet')
assert result['xnas_historical']['rows'] == 3
assert original.timestamp.max() == pd.Timestamp('2026-09-10T23:46:00Z')
result['xnas_historical']['last_bar_completion'] = '2026-09-10T23:47:00+00:00'
result['xnas_historical']['closing_gap_minutes'] = 13
comparison = json.loads((OUT/'comparison.json').read_text())
assert comparison['live']['status'] == 'FAILED'
assert 'LIVE_ACCESS_DENIED' in comparison['live']['error']
result['xnas_live'] = comparison['live']
result['xnas_both_apis_confirmed'] = False

mini = OUT/'equs-mini'
result['equs_mini_historical'] = historical(mini/'historical.dbn', 'EQUS.MINI')
store = db.DBNStore.from_file(mini/'live.dbn')
try:
    m = store.metadata
    assert m.dataset == 'EQUS.MINI' and m.start == START.value and m.end is None
    assert m.schema is None and not m.partial and not m.not_found
    records = list(store)
    assert len(records) == 377
    ack, mapping, complete = records[0], records[1], records[-1]
    assert isinstance(ack, db.SystemMsg) and int(ack.code) == 1
    assert ack.msg == 'Subscription request 0 for ohlcv-1m data succeeded'
    assert isinstance(mapping, db.SymbolMappingMsg)
    assert mapping.stype_in_symbol == mapping.stype_out_symbol == 'COST'
    assert int(mapping.instrument_id) == 3572
    assert int(mapping.start_ts) <= START.value < END.value < int(mapping.end_ts)
    assert isinstance(complete, db.SystemMsg) and int(complete.code) == 3
    assert complete.msg == 'Finished ohlcv-1m replay'
    assert int(complete.ts_event) > END.value
    markers = records[2:-1]
    assert all(isinstance(r, db.SystemMsg) and int(r.code) == 4 and
               r.msg == 'End of interval for ohlcv-1m' for r in markers)
    marker_times = [int(r.ts_event) for r in markers]
    assert marker_times == list(range(START.value, marker_times[-1]+1, 60_000_000_000))
    assert all(t.value in marker_times for t in pd.date_range(START, END, freq='min', inclusive='left'))
    result['equs_mini_live'] = {'status': 'COMPLETE_EMPTY', 'dataset': 'EQUS.MINI',
        'rows': 0, 'raw_records': len(records), 'interval_markers': len(markers),
        'subscription_acknowledged': True, 'replay_completed': True, 'raw_sha256': sha(mini/'live.dbn')}
finally:
    store.reader.close()
assert result['equs_mini_historical']['rows'] == 0
result['equs_mini_both_apis_same_empty_window'] = True
result['equs_mini_does_not_confirm_xnas_live_coverage'] = True
result['diagnostic_note'] = ('The initial EQUS diagnostic reused an XNAS-specific validator, which '
    'rejects empty deliveries and EQUS symbol mappings with omitted stype fields. This offline '
    'audit independently verifies the saved EQUS native metadata, exact COST mapping, subscription '
    'acknowledgement, every minute interval marker and replay completion; no additional request was made.')

payload = json.loads((OUT/'schwab-raw.json').read_text(encoding='utf-8'))
assert payload['symbol'] == 'COST'
rows = []
for index, candle in enumerate(payload['candles']):
    timestamp = pd.to_datetime(candle['datetime'], unit='ms', utc=True)
    if not START <= timestamp < END:
        continue
    assert timestamp == timestamp.floor('min')
    values = {key: float(candle[key]) for key in ('open','high','low','close','volume')}
    assert all(pd.notna(v) and v > 0 for v in values.values())
    assert values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']
    assert values['volume'].is_integer()
    rows.append({'timestamp': timestamp.isoformat(), 'raw_row_index': index, **values})
assert len(rows) == 3 and len({r['timestamp'] for r in rows}) == 3
latest = max(pd.Timestamp(r['timestamp']) for r in rows) + pd.Timedelta(minutes=1)
assert latest == pd.Timestamp('2026-09-10T23:51Z')
result['schwab'] = {'status': 'COMPLETE', 'rows': rows,
    'last_bar_completion': latest.isoformat(), 'closing_gap_minutes': (END-latest).total_seconds()/60,
    'qualifies_for_five_minute_boundary': False, 'raw_sha256': sha(OUT/'schwab-raw.json')}
result['decision'] = {'fallback_enabled': False, 'production_code_changed': False,
    'production_data_published': False, 'pipeline_resumed': False, 'orders_placed': 0,
    'reason': 'XNAS Live cannot confirm the primary gap, and Schwab supplies no candle within five minutes of the boundary.'}
(OUT/'verified-results.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
print(json.dumps(result, indent=2))
