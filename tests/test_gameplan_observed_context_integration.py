import json

import pandas as pd
import pytest

from ml.gameplan_champions import retain_champion
from ml.gameplan_input_evidence import FEATURE_EVIDENCE_COLUMNS
from ml.gameplan_source_selection import (
    ARCHIVE_SOURCE_SELECTION_VERSION,
    OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION,
    source_selection_contract,
)
from ml.independent_stock_targets import build_stock_current_groups
from ml.nightly_gameplan import _verify_source_selection_metadata


def test_new_context_contract_and_lineage_survive_target_expansion():
    source = pd.DataFrame({
        'symbol': ['DOCU'], 'action_date': ['2026-10-19'],
        'decision_timestamp': pd.to_datetime(['2026-10-17T00:05Z']),
        'information_available_at': pd.to_datetime(['2026-10-17T00:05Z']),
        'source_selection_contract': [OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION],
        'mr__technical_score': [0.2],
        'observed_technical_contract': ['gameplan-observed-technicals-v1'],
        'observed_technical_raw_input_hashes_json': [json.dumps(['a' * 64])],
        'observed_technical_available_at': pd.to_datetime(['2026-10-17T00:05Z']),
        'observed_technical_history_rows': [100],
    })
    assert source_selection_contract(source) == OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION
    result = build_stock_current_groups(source, feature_columns=('mr__technical_score',),
        price_source_contract='observed-multi-source-stock-v2')
    for frame in result.values():
        assert frame.source_selection_contract.eq(OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION).all()
        assert frame.mr__technical_score.eq(0.2).all()
        for name in set(FEATURE_EVIDENCE_COLUMNS).intersection(source.columns):
            assert frame[name].eq(source.iloc[0][name]).all()


def test_legacy_technical_champion_cannot_predict_new_context(tmp_path):
    current = pd.DataFrame({'source_selection_contract': [OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION]})
    champion = {'payload': {}, 'report': {'source_selection_contract': ARCHIVE_SOURCE_SELECTION_VERSION}}
    with pytest.raises(RuntimeError, match='another source selection contract'):
        retain_champion({}, champion=champion, current=current, run=tmp_path,
                        group='1h', frozen_at=pd.Timestamp('2026-10-17T14:00Z'))


def test_new_context_publication_requires_bound_reconstruction(tmp_path):
    contract = OBSERVED_TECHNICAL_SOURCE_SELECTION_VERSION
    selection = {'source_selection_contract': contract}
    config = {'source_selection_contract': contract, 'target_contract_version': 'independent-stock-targets-v1',
              'source_selection': selection, 'archive_history': True}
    history = {**selection, 'orders_placed': 0,
               'training_cohorts': {name: {} for name in ('1h', '4h', '1d', '1w')}}
    (tmp_path / 'archive-history.json').write_text(json.dumps(history))
    with pytest.raises(RuntimeError, match='reconstruction is not manifest-bound'):
        _verify_source_selection_metadata(tmp_path, {
            'configuration': config, 'output_files': {'archive-history.json': {}}})
