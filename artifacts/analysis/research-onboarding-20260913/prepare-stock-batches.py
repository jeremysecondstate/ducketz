"""Prepare stock jobs under the already running batch's supervision ownership."""
from pathlib import Path
import json
import os
import threading
import databento
from datafetching.cme_runtime import load_repository_environment
from datafetching.research_onboarding import load_batch, prepare_stock_batches
from ml.overnight_runtime import overnight_status

load_repository_environment()
path = Path(__file__).resolve().parent/'plan.json'
batch = load_batch(path)
supervision = overnight_status(Path(batch['datastore_root']))['supervision']
expected = json.loads((path.parent/'supervision-owner.json').read_text())['owner_token']
if not supervision.get('active') or supervision.get('owner_token') != expected:
    raise RuntimeError('This batch no longer owns supervision')
print(json.dumps(prepare_stock_batches(path,batch,databento.Historical(os.environ['DATABENTO_API_KEY']),threading.Event())))
