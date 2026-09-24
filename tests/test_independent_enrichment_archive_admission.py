"""Archive optional-input admission; native synthetic target fixtures only."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from ml.gameplan_archive_features import ARCHIVE_FEATURE_CONTRACT
from ml.independent_stock_targets import stock_target_windows
from ml.stock_trader import independent_training as training
from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES


MARKET = list(INDEPENDENT_MARKET_FEATURE_NAMES)
TRAINED = pd.Timestamp('2026-02-02T08:00:00Z')


def cohort(group='1h'):
    rows = []
    for day in ('2026-01-05', '2026-01-06'):
        spec = next(row for row in stock_target_windows(pd.Timestamp(day).date())
                    if row['model_group'] == group and row['execution_eligible'])
        for symbol, close in (('AAPL', 101.), ('AMZN', 99.)):
            start, end = spec['target_window_start'], spec['target_window_end']
            rows.append({**spec, 'symbol': symbol, 'action_date': pd.Timestamp(day).date(),
                'source_selection_contract': ARCHIVE_FEATURE_CONTRACT,
                'decision_timestamp': start - pd.Timedelta(hours=2),
                'information_available_at': start - pd.Timedelta(hours=3),
                'observed_open_timestamp': start, 'observed_close_timestamp': end,
                'target_open': 100., 'target_close': close, 'observed_return': close / 100. - 1.,
                'assumed_round_trip_cost': .001, 'target_boundary_aligned': True,
                'target_price_source_contract': 'xnas-itch-archive-v1', 'target_price_dataset': 'XNAS.ITCH',
                **{name: float(index + .125) for index, name in enumerate(MARKET)}})
    return pd.DataFrame(rows)


def admit(frame, group='1h', **kwargs):
    return training._admit_targets(frame, group=group, trained_at=TRAINED,
                                   allow_missing_archive_market_features=True, **kwargs)


@pytest.mark.parametrize('group', ('1h', '4h', '1d', '1w'))
def test_archive_admission_keeps_every_finite_row_and_discloses_absent_inputs(group):
    frame = cohort(group)
    frame.loc[0, MARKET] = np.nan
    original = frame.copy(deep=True)
    actual = admit(frame, group)
    strict_finite = training._admit_targets(frame.drop(index=0), group=group, trained_at=TRAINED)
    pd.testing.assert_frame_equal(actual, strict_finite)
    pd.testing.assert_frame_equal(frame, original)
    assert np.isfinite(actual[MARKET]).all().all()
    report = actual.attrs['market_feature_admission']
    assert report['policy'] == training.ARCHIVE_MARKET_ADMISSION_POLICY
    assert report['input_execution_rows'] == 4
    assert report['admitted_rows'] == 3
    assert report['excluded_missing_market_rows'] == 1
    assert report['excluded_by_symbol'] == {'AAPL': 1}
    assert report['excluded_by_feature'] == dict.fromkeys(MARKET, 1)
    assert re.fullmatch('[0-9a-f]{64}', report['excluded_rows_sha256'])


def test_complete_archive_rows_are_unchanged_and_no_exclusions_are_invented():
    frame = cohort()
    actual = admit(frame)
    strict = training._admit_targets(frame, group='1h', trained_at=TRAINED)
    pd.testing.assert_frame_equal(actual, strict)
    report = actual.attrs['market_feature_admission']
    assert report['input_execution_rows'] == report['admitted_rows'] == 4
    assert report['excluded_missing_market_rows'] == 0
    assert not any(report['excluded_by_symbol'].values())
    assert not any(report['excluded_by_feature'].values())


def test_all_absent_archive_rows_return_explicit_unavailable_training_evidence():
    frame = cohort()
    frame.loc[:, MARKET] = np.nan
    actual = admit(frame)
    assert actual.empty
    report = actual.attrs['market_feature_admission']
    assert report['input_execution_rows'] == report['excluded_missing_market_rows'] == 4
    assert report['admitted_rows'] == 0
    assert report['excluded_by_symbol'] == {'AAPL': 2, 'AMZN': 2}


def test_exclusion_digest_binds_the_missing_row_identity():
    frame = cohort()
    frame.loc[0, MARKET] = np.nan
    first = admit(frame).attrs['market_feature_admission']
    changed = frame.copy()
    changed.loc[0, 'symbol'] = 'GOOG'
    second = admit(changed).attrs['market_feature_admission']
    assert first['excluded_rows_sha256'] != second['excluded_rows_sha256']
    assert first['excluded_missing_market_rows'] == second['excluded_missing_market_rows'] == 1


@pytest.mark.parametrize('contract', [None, 'independent-gameplan-prior-session-features-v1', 'unknown'])
def test_legacy_or_unknown_feature_contract_cannot_use_archive_absence_exception(contract):
    frame = cohort()
    frame.loc[0, MARKET] = np.nan
    if contract is None:
        frame = frame.drop(columns='source_selection_contract')
    else:
        frame['source_selection_contract'] = contract
    with pytest.raises(ValueError):
        admit(frame)


def test_default_admission_remains_strict_for_absent_archive_features():
    frame = cohort()
    frame.loc[0, MARKET] = np.nan
    with pytest.raises(ValueError, match='finite causal observations'):
        training._admit_targets(frame, group='1h', trained_at=TRAINED)


def test_nullable_unknown_archive_identity_cannot_silently_drop_missing_rows():
    frame = cohort()
    frame['source_selection_contract'] = frame.source_selection_contract.astype('string')
    frame.loc[0, 'source_selection_contract'] = pd.NA
    frame.loc[0, MARKET] = np.nan
    with pytest.raises(ValueError, match='finite causal observations'):
        admit(frame)


@pytest.mark.parametrize('mutation', ['one_null', 'seven_null', 'positive_inf', 'negative_inf',
                                      'malformed', 'all_malformed', 'missing_column'])
def test_archive_exception_rejects_partial_missing_or_supplied_invalid_inputs(mutation):
    frame = cohort()
    if mutation == 'one_null':
        frame.loc[0, MARKET[0]] = np.nan
    elif mutation == 'seven_null':
        frame.loc[0, MARKET[1:]] = np.nan
    elif mutation in {'positive_inf', 'negative_inf'}:
        frame.loc[0, MARKET[0]] = np.inf if mutation == 'positive_inf' else -np.inf
    elif mutation in {'malformed', 'all_malformed'}:
        columns = MARKET if mutation == 'all_malformed' else MARKET[:1]
        frame[columns] = frame[columns].astype(object)
        frame.loc[0, columns] = 'not-a-market-observation'
    else:
        frame = frame.drop(columns=MARKET[0])
    with pytest.raises(ValueError):
        admit(frame)


@pytest.mark.parametrize('mutation, message', [
    ('false_return', 'observed target prices'),
    ('false_window', 'exact clock contract'),
    ('future_feature', 'future features'),
    ('stale_boundary', 'boundary aligned'),
    ('mixed_source', 'mix or omit'),
    ('duplicate', 'repeats a natural target'),
])
def test_absent_features_do_not_hide_corrupt_target_evidence(mutation, message):
    frame = cohort()
    frame.loc[0, MARKET] = np.nan
    if mutation == 'false_return':
        frame.loc[0, 'observed_return'] = .9
    elif mutation == 'false_window':
        frame.loc[0, 'target_window_end'] += pd.Timedelta(hours=1)
    elif mutation == 'future_feature':
        frame.loc[0, 'information_available_at'] = frame.loc[0, 'target_window_end']
    elif mutation == 'stale_boundary':
        frame.loc[0, 'observed_close_timestamp'] -= pd.Timedelta(minutes=6)
    elif mutation == 'mixed_source':
        frame.loc[0, 'target_price_source_contract'] = 'canonical-equity-minute-v1'
    else:
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match=message):
        admit(frame)
