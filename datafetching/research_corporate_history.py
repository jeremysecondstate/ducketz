"""Resumable company research archives, with no data earlier than the chosen floor."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

import pandas as pd
import requests

from datafetching.symbol_onboarding import _write, _now


class SecReader:
    """One shared fair-access limiter across this importer's requests."""
    def __init__(self):
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.headers = {'User-Agent': os.environ['SEC_USER_AGENT'], 'Accept-Encoding': 'gzip, deflate'}

    def get(self, url: str) -> bytes:
        if not url.startswith(('https://data.sec.gov/submissions/', 'https://www.sec.gov/Archives/edgar/data/')):
            raise ValueError('Corporate archive requests must use official SEC locations')
        for attempt in range(4):
            with self.lock:
                time.sleep(max(0, self.next_request-time.monotonic()))
                self.next_request = time.monotonic()+0.16
            try:
                response = requests.get(url, headers=self.headers, timeout=60)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3: raise RuntimeError('SEC archive transport failed') from None
                time.sleep(2**attempt); continue
            if response.ok: return response.content
            if response.status_code in (429,500,502,503,504) and attempt<3:
                time.sleep(2**(attempt+1)); continue
            raise RuntimeError(f'SEC archive returned HTTP {response.status_code}')
        raise RuntimeError('SEC archive attempts exhausted')


def submission_rows(payload: dict) -> list[dict]:
    columns = payload.get('filings', {}).get('recent', payload)
    if not isinstance(columns.get('accessionNumber'), list):
        raise ValueError('SEC submission metadata has no accession array')
    return [{key: value[i] for key,value in columns.items() if isinstance(value,list) and len(value)==len(columns['accessionNumber'])}
            for i in range(len(columns['accessionNumber']))]


def fetch_corporate_history(plan: dict, output: Path, *, sec_reader: SecReader | None = None) -> dict:
    from app.services.fmp_corporate_data import FmpCorporateDataProvider
    from datafetching.history_scope import read_history_policy, price_floor
    root, symbol, cik = Path(plan['datastore_root']), plan['symbol'], str(plan['cik'])
    floor = plan['history_floor']
    policy = read_history_policy(root,symbol)
    first_price_date = price_floor(policy).date().isoformat() if policy else max(floor,plan['listing_date'])
    destination = root/'stocks'/symbol/'corporate'/'research-history'
    destination.mkdir(parents=True, exist_ok=True)
    reader = sec_reader or SecReader()
    index_path = destination/'sec-index.json'
    if index_path.exists():
        index = json.loads(index_path.read_text())
        if index.get('plan_id') != plan['plan_id']:
            raise ValueError('SEC index belongs to a different historical scope')
    else:
        raw = json.loads(reader.get(f'https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json'))
        rows = submission_rows(raw)
        for older in raw.get('filings',{}).get('files',[]):
            if older.get('filingTo','') < floor: continue
            name = older['name']
            if not re.fullmatch(r'CIK\d{10}-submissions-\d+\.json', name):
                raise ValueError('Unexpected SEC submission index filename')
            rows.extend(submission_rows(json.loads(reader.get('https://data.sec.gov/submissions/'+name))))
        selected = {r['accessionNumber']:r for r in rows if floor <= r.get('filingDate','') <= datetime.now(timezone.utc).date().isoformat()}
        index = {'plan_id':plan['plan_id'], 'cik':cik, 'history_floor':floor, 'captured_at':_now(), 'filings':list(selected.values())}
        _write(index_path,index)

    def filing(row):
        accession = row['accessionNumber']
        if not re.fullmatch(r'\d{10}-\d{2}-\d{6}',accession):
            raise ValueError('Invalid SEC accession')
        path = destination/'sec-submissions'/f'{accession}.txt.gz'
        receipt_path = path.with_suffix('.receipt.json')
        if path.exists() and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError('Retained SEC submission checksum mismatch')
            return receipt
        url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{accession}.txt'
        raw = reader.get(url)
        if b'<SEC-DOCUMENT>' not in raw[:1000] and b'<SEC-HEADER>' not in raw[:1000]:
            raise ValueError('SEC response is not a complete filing submission')
        compressed = gzip.compress(raw,mtime=0)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(compressed); temporary.replace(path)
        receipt = {'accession':accession, 'filing_date':row['filingDate'], 'form':row['form'], 'url':url,
            'stored_bytes':len(compressed), 'uncompressed_bytes':len(raw),
            'sha256':hashlib.sha256(compressed).hexdigest(), 'fetched_at':_now()}
        _write(receipt_path,receipt)
        return receipt

    receipts = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(filing,row) for row in index['filings']]
        try:
            for number,future in enumerate(as_completed(futures),1):
                receipts.append(future.result())
                if number%50==0:
                    print(f'SEC_HISTORY {symbol} {number}/{len(futures)}',flush=True)
                    _write(output,{'plan_id':plan['plan_id'],'status':'FETCHING','completed_filings':number,'total_filings':len(futures)})
        except BaseException:
            for future in futures: future.cancel()
            raise
    fmp = FmpCorporateDataProvider()
    prices = fmp._get_json('historical-price-eod/full',{'symbol':symbol,'from':first_price_date,
        'to':datetime.now(timezone.utc).date().isoformat()})
    if not isinstance(prices,list) or not prices: raise ValueError('FMP daily price archive is empty')
    prices = [row for row in prices if str(row.get('date',''))[:10]>=first_price_date]
    price_path = destination/'fmp-daily-prices.parquet'
    pd.DataFrame(prices).to_parquet(price_path,index=False)
    receipt = {'plan_id':plan['plan_id'],'status':'COMPLETE','completed_at':_now(),'history_floor':floor,
        'sec_filing_count':len(receipts),'sec_stored_bytes':sum(r['stored_bytes'] for r in receipts),
        'sec_uncompressed_bytes':sum(r['uncompressed_bytes'] for r in receipts),
        'sec_index_sha256':hashlib.sha256(index_path.read_bytes()).hexdigest(),
        'fmp_daily_rows':len(prices),'fmp_daily_sha256':hashlib.sha256(price_path.read_bytes()).hexdigest()}
    _write(output,receipt)
    return receipt


if __name__ == '__main__':
    import argparse
    from datafetching.cme_runtime import load_repository_environment
    from datafetching.research_onboarding import load_batch
    from datafetching.symbol_onboarding import load_plan
    from datafetching.runtime_lock import exclusive_runtime_lock
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-plan',type=Path,required=True)
    args=parser.parse_args()
    load_repository_environment()
    batch=load_batch(args.batch_plan)
    with exclusive_runtime_lock(args.batch_plan.parent/'corporate-history.lock',process_name='Research company archives'):
        reader=SecReader()
        for symbol in batch['selected_symbols']:
            p=args.batch_plan.parent/symbol/'plan.json'
            print(json.dumps(fetch_corporate_history(load_plan(p),p.parent/'corporate-history.json',sec_reader=reader)),flush=True)
