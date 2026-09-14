"""Read retained evidence and verify the repair on a separate local copy."""
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from datafetching import databento_opra_history as history

root = Path('C:/DATASTORE')
source = root / 'market-data/databento/opra/OPRA.PILLAR/.staging/status/CROX.OPT/2024-07-25/full-day/attempt-001'
artifact = Path(__file__).resolve().parent
raw = source / 'provider.dbn.zst'
normalized = source / 'normalized.parquet'
raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
before = pd.read_parquet(normalized)
copy = artifact / 'status-repair-verification.parquet'
shutil.copy2(normalized, copy)
identity = history._add_source_record_identity(raw, copy, schema='status')
validation = history.validate_parquet(copy, schema='status')
after = pd.read_parquet(copy)
pd.testing.assert_frame_equal(before, after.drop(columns=['source_record_ordinal']))
assert len(after) == 23754
assert after['source_record_ordinal'].tolist() == list(range(len(after)))
assert validation['duplicate_natural_key_rows'] == 0
assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_hash
legacy = history.partition_directory(root, schema='status', day='2024-07-17', symbols=('CROX.OPT',))
legacy_result = history.verify_partition(legacy, datastore_root=root)
receipt = {
    'status': 'PASSED', 'native_sha256': raw_hash,
    'preserved_rows': len(after), 'identical_source_rows': int(before.duplicated().sum()),
    'source_record_identity': identity,
    'duplicate_natural_key_rows_after': validation['duplicate_natural_key_rows'],
    'legacy_partition_verified': str(legacy),
    'schema_reference': 'https://databento.com/docs/schemas-and-data-formats/status',
}
(artifact / 'status-repair-verification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
print(json.dumps(receipt))
