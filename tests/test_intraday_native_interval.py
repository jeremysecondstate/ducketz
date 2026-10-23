"""Independent contract regressions using fabricated local observations only."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import IntervalResponse, SessionBounds, completed_15m_observation
from ml.intraday.native_interval import (
    CONTRACT, FEATURE_VERSION, build_native_interval_dataset,
    completed_native_interval, load_native_interval_history,
    resolve_native_interval, candle_matches_forecast,
)
from ml.intraday.prediction import ContractError


OPEN = pd.Timestamp("2026-05-11T14:00:00Z")
CLOSE = OPEN + pd.Timedelta(minutes=15)
SESSION = SessionBounds("2026-05-11", "2026-05-11T08:00:00Z", "2026-05-12T00:00:00Z")


def native_rows():
    return (
        dict(opened_at=(OPEN + pd.Timedelta(minutes=2)).isoformat(),
             open=100.0, high=103.0, low=99.0, close=102.0, volume=11.0),
        dict(opened_at=(OPEN + pd.Timedelta(minutes=12)).isoformat(),
             open=107.0, high=109.0, low=106.0, close=108.0, volume=17.0),
    )


def response(**changes):
    original = IntervalResponse(
        "native-attempt-1", "TEST", "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
        OPEN.isoformat(), CLOSE.isoformat(),
        (CLOSE + pd.Timedelta(seconds=2)).isoformat(),
        (CLOSE + pd.Timedelta(seconds=3)).isoformat(), "success", native_rows(),
        OPEN.isoformat(), CLOSE.isoformat(),
    )
    return replace(original, **changes)


def resolved():
    return resolve_native_interval(stable_responses(), session=SESSION)["candle"]


def stable_responses(**changes):
    return [response(**changes), response(attempt_id="native-attempt-2",
        request_started_at=(CLOSE + pd.Timedelta(seconds=7)).isoformat(),
        request_ended_at=(CLOSE + pd.Timedelta(seconds=8)).isoformat(), **changes)]


def test_sparse_interval_uses_actual_first_last_and_extrema_without_fill():
    result = resolve_native_interval(stable_responses(), session=SESSION)
    assert result["status"] == "observed"
    candle = result["candle"]
    assert tuple(candle[x] for x in ("open", "high", "low", "close", "volume")) == (100, 109, 99, 108, 28)
    assert candle["native_interval_contract"] == CONTRACT
    assert pd.Timestamp(candle["opened_at"]) == OPEN
    assert pd.Timestamp(candle["available_at"]) == CLOSE + pd.Timedelta(seconds=8)
    assert candle["observed_constituents"] == 2
    assert candle["source_empty_constituents"] == 13
    assert candle["synthetic_constituents"] == 0
    assert not candle["synthetic"] and not candle["no_trade"]
    assert completed_native_interval(candle)
    assert completed_15m_observation(candle)


def test_wholly_empty_source_has_no_price_or_synthetic_candle():
    result = resolve_native_interval(stable_responses(rows=()), session=SESSION)
    assert result["status"] == "missing"
    assert result["reason"] == "source_empty_interval"
    assert result["candle"] is None


def test_single_sparse_response_waits_for_stable_source_snapshot():
    result = resolve_native_interval([response()], session=SESSION)
    assert result["status"] == "retry"
    assert result["candle"] is None


@pytest.mark.parametrize("changes", [
    dict(attempt_id="native-attempt-1"),
    dict(request_started_at=(CLOSE + pd.Timedelta(seconds=6)).isoformat()),
    dict(rows=({**native_rows()[0], "close": 101.0}, native_rows()[1])),
])
def test_sparse_recheck_must_be_distinct_waited_and_unchanged(changes):
    earlier, later = stable_responses()
    result = resolve_native_interval([earlier, replace(later, **changes)], session=SESSION)
    assert result["status"] == "retry"
    assert result["candle"] is None


def test_reappearing_older_snapshot_must_not_bypass_latest_changed_revision():
    earlier, final = stable_responses()
    changed = replace(earlier, attempt_id="changed", rows=({**native_rows()[0], "close": 101}, native_rows()[1]),
                      request_started_at=(CLOSE + pd.Timedelta(seconds=7)).isoformat(),
                      request_ended_at=(CLOSE + pd.Timedelta(seconds=8)).isoformat())
    final = replace(final, attempt_id="final",
                    request_started_at=(CLOSE + pd.Timedelta(seconds=12)).isoformat(),
                    request_ended_at=(CLOSE + pd.Timedelta(seconds=13)).isoformat())
    result = resolve_native_interval([earlier, changed, final], session=SESSION)
    assert result["status"] == "retry"
    assert result["candle"] is None


def test_complete_fifteen_minute_source_does_not_require_empty_minute_confirmation():
    rows = tuple({**native_rows()[0], "opened_at": (OPEN + pd.Timedelta(minutes=i)).isoformat()} for i in range(15))
    result = resolve_native_interval([response(rows=rows)], session=SESSION)
    assert result["status"] == "observed"
    assert result["candle"]["observed_constituents"] == 15
    assert result["candle"]["source_empty_constituents"] == 0


@pytest.mark.parametrize("changes", [
    dict(coverage_open=None, coverage_close=None),
    dict(coverage_open=(OPEN + pd.Timedelta(minutes=1)).isoformat()),
    dict(coverage_close=(CLOSE - pd.Timedelta(minutes=1)).isoformat()),
    dict(status="unavailable_coverage", rows=()),
    dict(status="temporary_error", rows=()),
])
def test_unknown_or_incomplete_coverage_cannot_supply_price(changes):
    result = resolve_native_interval([response(**changes)], session=SESSION)
    assert result["status"] == "retry"
    assert result["candle"] is None


def test_latest_failure_does_not_reuse_an_earlier_success():
    late = response(attempt_id="native-attempt-2", status="temporary_error", rows=(),
                    request_started_at=(CLOSE + pd.Timedelta(seconds=7)).isoformat(),
                    request_ended_at=(CLOSE + pd.Timedelta(seconds=8)).isoformat())
    result = resolve_native_interval([response(), late], session=SESSION)
    assert result["status"] == "retry"
    assert result["candle"] is None


@pytest.mark.parametrize("mutator", [
    lambda rows: (*rows, rows[0]),
    lambda rows: ({**rows[0], "opened_at": (OPEN + pd.Timedelta(seconds=30)).isoformat()}, rows[1]),
    lambda rows: ({**rows[0], "opened_at": CLOSE.isoformat()}, rows[1]),
    lambda rows: ({**rows[0], "opened_at": (OPEN - pd.Timedelta(minutes=1)).isoformat()}, rows[1]),
    lambda rows: ({**rows[0], "high": 90}, rows[1]),
    lambda rows: ({**rows[0], "volume": -1}, rows[1]),
    lambda rows: ({**rows[0], "close": float("nan")}, rows[1]),
])
def test_invalid_constituent_rows_are_rejected(mutator):
    with pytest.raises(ContractError):
        resolve_native_interval([response(rows=mutator(native_rows()))], session=SESSION)


@pytest.mark.parametrize("changes", [
    dict(symbol="OTHER"),
    dict(source_dataset="XNAS.ITCH"),
    dict(source_id="databento:OTHER:ohlcv-1m"),
    dict(interval_open=(OPEN - pd.Timedelta(minutes=15)).isoformat()),
])
def test_retry_identity_must_remain_exact(changes):
    with pytest.raises(ContractError):
        resolve_native_interval([response(), response(attempt_id="second", **changes)], session=SESSION)


@pytest.mark.parametrize("field,value", [
    ("open", 101), ("close", 107), ("high", 110), ("low", 98), ("volume", 29),
    ("observed_constituents", 15), ("source_empty_constituents", 0),
    ("synthetic_constituents", 13), ("synthetic", True),
    ("synthetic_open", True), ("synthetic_close", True),
    ("native_interval_contract", "unreviewed-contract"),
    ("available_at", (CLOSE + pd.Timedelta(seconds=1)).isoformat()),
    ("evidence_json", "{}"),
])
def test_saved_contract_rejects_tampered_aggregate_or_proof(field, value):
    candle = resolved()
    candle[field] = value
    assert not completed_native_interval(candle)
    assert not completed_15m_observation(candle)


def test_removed_contract_tag_cannot_turn_sparse_interval_into_legacy_bar():
    candle = resolved()
    del candle["native_interval_contract"]
    assert not completed_native_interval(candle)
    assert not completed_15m_observation(candle)


@pytest.mark.parametrize("change", [
    lambda proof: proof.pop("stable_previous"),
    lambda proof: proof["stable_previous"].__setitem__("attempt_id", proof["attempt_id"]),
    lambda proof: proof["stable_previous"].__setitem__("source_dataset", "XNAS.ITCH"),
    lambda proof: proof["stable_previous"].__setitem__("status", "temporary_error"),
    lambda proof: proof["stable_previous"].__setitem__("request_ended_at", (CLOSE + pd.Timedelta(seconds=4)).isoformat()),
    lambda proof: proof["stable_previous"]["observations"][0].__setitem__("close", 101),
])
def test_saved_sparse_candle_retains_and_validates_stability_receipt(change):
    candle = resolved()
    evidence = json.loads(candle["evidence_json"])
    change(evidence["coverage"])
    candle["evidence_json"] = json.dumps(evidence)
    assert not completed_native_interval(candle)
    assert not completed_15m_observation(candle)


def test_native_proof_survives_dataframe_roundtrip_and_old_rows_still_validate():
    native = resolved()
    legacy = dict(symbol="OLD", opened_at=OPEN.isoformat(), available_at=CLOSE.isoformat(),
                  interval_seconds=900, open=10, high=10, low=10, close=10, volume=2,
                  synthetic=False, no_trade=False, evidence_json="{}")
    records = pd.DataFrame([native, legacy]).to_dict("records")
    assert completed_native_interval(records[0], from_frame=True)
    assert completed_15m_observation(records[0], from_frame=True)
    assert not completed_native_interval(records[1], from_frame=True)
    assert completed_15m_observation(records[1], from_frame=True)
    assert completed_15m_observation(legacy)


def test_missing_history_is_an_explicit_error(tmp_path):
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


def write_history_pair(root, *, rows=None, name="fixture", raw_changes=None, norm_changes=None):
    observations = pd.DataFrame(native_rows() if rows is None else rows)
    raw = observations.rename(columns={"opened_at": "ts_event"}).copy()
    raw["symbol"] = "TEST"
    raw["provider_dataset"] = "EQUS.MINI"
    raw["source_schema"] = "ohlcv-1m"
    raw["price_basis"] = "unadjusted_market_scale"
    raw["aggregation_method"] = "native"
    raw["range_start"] = OPEN.isoformat()
    raw["range_end"] = (OPEN + pd.Timedelta(hours=10)).isoformat()
    norm = observations.rename(columns={"opened_at": "timestamp"}).copy()
    if raw_changes:
        raw_changes(raw)
    if norm_changes:
        norm_changes(norm)
    base = root / "stocks/TEST/bars/1m/databento"
    raw_path = base / "raw" / f"{name}_ohlcv-1m_1m_raw.parquet"
    norm_path = base / "normalized" / f"{name}_ohlcv-1m_1m.parquet"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.parent.mkdir(parents=True, exist_ok=True)
    raw.to_parquet(raw_path, index=False)
    norm.to_parquet(norm_path, index=False)
    return raw_path, norm_path


def test_history_excludes_source_empty_quarters_and_marks_receipt_assumption(tmp_path):
    rows = native_rows() + tuple({**row, "opened_at": (pd.Timestamp(row["opened_at"]) + pd.Timedelta(minutes=30)).isoformat()}
                                for row in native_rows())
    write_history_pair(tmp_path, rows=rows)
    history = load_native_interval_history(tmp_path, "TEST")
    assert history.frame.opened_at.tolist() == [OPEN, OPEN + pd.Timedelta(minutes=30)]
    assert history.frame.source_empty_constituents.tolist() == [13, 13]
    assert history.frame.availability_kind.eq("assumed_historical_close").all()
    assert "unknown" in history.manifest["availability_limitation"].lower()
    assert len(history.manifest["files"]) == 2
    assert all(len(item["sha256"]) == 64 for item in history.manifest["files"])
    assert all(completed_native_interval(row, from_frame=True) for row in history.frame.to_dict("records"))


@pytest.mark.parametrize("field,bad", [
    ("provider_dataset", "XNAS.ITCH"), ("source_schema", "ohlcv-15m"),
    ("price_basis", "adjusted"), ("aggregation_method", "derived"),
    ("symbol", "OTHER"), ("range_start", None), ("range_end", None),
    ("price_basis", None),
])
def test_each_raw_row_requires_matching_complete_source_provenance(tmp_path, field, bad):
    def corrupt(frame):
        frame.loc[0, field] = bad
    write_history_pair(tmp_path, raw_changes=corrupt)
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


def test_raw_and_normalized_prices_must_match_exactly(tmp_path):
    def corrupt(frame):
        frame.loc[0, "close"] += 0.01
    write_history_pair(tmp_path, norm_changes=corrupt)
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


def test_raw_and_normalized_numeric_volume_dtypes_can_differ(tmp_path):
    write_history_pair(tmp_path, norm_changes=lambda frame: frame.__setitem__("volume", frame.volume.astype(int)))
    assert len(load_native_interval_history(tmp_path, "TEST").frame) == 1


def test_identical_overlap_is_deduplicated_but_conflicting_native_correction_is_rejected(tmp_path):
    write_history_pair(tmp_path, name="first")
    write_history_pair(tmp_path, name="same")
    history = load_native_interval_history(tmp_path, "TEST")
    assert history.manifest["minute_rows"] == 2
    altered = ({**native_rows()[0], "close": 101.0}, native_rows()[1])
    write_history_pair(tmp_path, rows=altered, name="correction")
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


@pytest.mark.parametrize("second_rows", [native_rows()[:1], ({**native_rows()[0], "opened_at": (OPEN + pd.Timedelta(minutes=20)).isoformat()},)])
def test_fully_covered_history_snapshots_cannot_silently_union_different_occupancy(tmp_path, second_rows):
    write_history_pair(tmp_path, name="first")
    write_history_pair(tmp_path, name="different-occupancy", rows=second_rows)
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


@pytest.mark.parametrize("second_offset,expected_counts", [(0, [2]), (20, [1, 1])])
def test_canonical_merged_file_uses_whole_snapshot_not_per_row_request_slices(tmp_path, second_offset, expected_counts):
    # A canonical upsert retains an older row while a later successful fetch
    # supplies another row. Range metadata describes each row's acquisition;
    # it cannot reconstruct either request's complete original response.
    rows = (native_rows()[0], {**native_rows()[1],
        "opened_at": (pd.Timestamp(native_rows()[1]["opened_at"]) + pd.Timedelta(minutes=second_offset)).isoformat()})
    def merged_ranges(frame):
        frame.loc[0, "range_start"] = OPEN.isoformat()
        frame.loc[0, "range_end"] = (OPEN + pd.Timedelta(hours=1)).isoformat()
        second_open = pd.Timestamp(rows[1]["opened_at"]).floor("15min")
        frame.loc[1, "range_start"] = second_open.isoformat()
        frame.loc[1, "range_end"] = (second_open + pd.Timedelta(minutes=15)).isoformat()
    write_history_pair(tmp_path, rows=rows, raw_changes=merged_ranges)
    history = load_native_interval_history(tmp_path, "TEST")
    assert history.frame.observed_constituents.tolist() == expected_counts
    assert history.frame.volume.sum() == 28
    assert history.frame.availability_kind.eq("assumed_historical_close").all()
    for row in history.frame.to_dict("records"):
        proof = json.loads(row["evidence_json"])
        assert proof["coverage"]["observation_basis"] == "canonical_native_file_after_successful_fetch_merges"
        assert completed_native_interval(row, from_frame=True)


@pytest.mark.parametrize("basis", [None, "actual_exact_response"])
def test_historical_proof_cannot_omit_or_misrepresent_canonical_snapshot_basis(tmp_path, basis):
    write_history_pair(tmp_path)
    candle = load_native_interval_history(tmp_path, "TEST").frame.iloc[0].to_dict()
    proof = json.loads(candle["evidence_json"])
    if basis is None:
        proof["coverage"].pop("observation_basis")
    else:
        proof["coverage"]["observation_basis"] = basis
    candle["evidence_json"] = json.dumps(proof)
    assert not completed_native_interval(candle, from_frame=True)


def test_partial_historical_query_cannot_prove_full_quarter_occupancy(tmp_path):
    def partial(frame):
        frame["range_start"] = (OPEN + pd.Timedelta(minutes=1)).isoformat()
        frame["range_end"] = (OPEN + pd.Timedelta(minutes=14)).isoformat()
    write_history_pair(tmp_path, raw_changes=partial)
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


def test_history_rejects_off_grid_native_minutes_even_when_raw_normalized_agree(tmp_path):
    rows = ({**native_rows()[0], "opened_at": (OPEN + pd.Timedelta(seconds=30)).isoformat()}, native_rows()[1])
    write_history_pair(tmp_path, rows=rows)
    with pytest.raises(ContractError):
        load_native_interval_history(tmp_path, "TEST")


def dataset_frame():
    records = []
    for i in range(40):
        offset = pd.Timedelta(minutes=15 * i)
        price_shift = float(i % 7)
        rows = tuple({**row, "opened_at": (pd.Timestamp(row["opened_at"]) + offset).isoformat(),
                      **{field: row[field] + price_shift for field in ("open", "high", "low", "close")}}
                     for row in native_rows())
        attempts = [replace(item, attempt_id=f"{i}-{item.attempt_id}",
                    interval_open=(OPEN + offset).isoformat(), interval_close=(CLOSE + offset).isoformat(),
                    coverage_open=(OPEN + offset).isoformat(), coverage_close=(CLOSE + offset).isoformat(),
                    request_started_at=(pd.Timestamp(item.request_started_at) + offset).isoformat(),
                    request_ended_at=(pd.Timestamp(item.request_ended_at) + offset).isoformat(), rows=rows)
                    for item in stable_responses()]
        records.append(resolve_native_interval(attempts, session=SESSION)["candle"])
    return pd.DataFrame(records)


def test_native_dataset_versions_snapshot_and_preserves_exact_target_gap():
    frame = dataset_frame()
    missing = OPEN + pd.Timedelta(minutes=15 * 29)
    frame = frame.loc[pd.to_datetime(frame.opened_at).ne(missing)].reset_index(drop=True)
    reference = OPEN + pd.Timedelta(minutes=15 * 35)
    data = build_native_interval_dataset(frame, cutoff=reference + pd.Timedelta(minutes=16), reference_at=reference)
    assert missing - pd.Timedelta(minutes=30) not in data.reference_at
    assert data.inference["feature_version"] == FEATURE_VERSION
    assert data.inference["snapshot"]["feature_version"] == FEATURE_VERSION
    assert data.inference["snapshot"]["native_interval_contract"] == CONTRACT
    assert data.manifest["native_interval_contract"] == CONTRACT
    assert pd.Timestamp(data.inference["target_open"]) == reference + pd.Timedelta(minutes=30)
    with pytest.raises(ContractError):
        build_native_interval_dataset(frame, cutoff=reference + pd.Timedelta(minutes=16), reference_at=missing)


def test_native_dataset_future_observations_cannot_change_causal_input():
    frame = dataset_frame()
    reference = OPEN + pd.Timedelta(minutes=15 * 35)
    cutoff = reference + pd.Timedelta(minutes=16)
    original = build_native_interval_dataset(frame, cutoff=cutoff, reference_at=reference)
    truncated = frame.loc[pd.to_datetime(frame.opened_at).le(reference)].copy()
    bounded = build_native_interval_dataset(truncated, cutoff=cutoff, reference_at=reference)
    np.testing.assert_array_equal(original.X, bounded.X)
    np.testing.assert_array_equal(original.y, bounded.y)
    assert original.inference["snapshot_sha256"] == bounded.inference["snapshot_sha256"]


def test_native_dataset_rejects_unversioned_legacy_candle():
    frame = dataset_frame()
    frame.loc[0, "native_interval_contract"] = None
    reference = OPEN + pd.Timedelta(minutes=15 * 35)
    with pytest.raises(ContractError):
        build_native_interval_dataset(frame, cutoff=reference + pd.Timedelta(minutes=16), reference_at=reference)


def frozen_recipe():
    from ml.intraday import live_model_recipe as recipes
    from ml.intraday.runtime import ResearchConfig
    recipe = recipes.make_manual_recipe(pc="Scout", symbols=("TEST", "OTHER"),
        installed_at="2026-05-10T12:00:00Z", authority_reference="fixture local authority")
    return recipes.migrate_manual_recipe(recipe, pc="Scout", symbols=("TEST", "OTHER"),
        config=ResearchConfig(source_dataset="EQUS.MINI", lookback_days=None))


def test_population_migration_preserves_local_models_and_default_symbol_contract():
    from ml.intraday import live_model_recipe as recipes
    original = frozen_recipe()
    revised = recipes.migrate_native_interval_recipe(original, pc="Scout", symbols=("TEST", "OTHER"),
        native_symbols=("TEST",), installed_at="2026-05-11T12:00:00Z", authority_reference="fixture local authority")
    assert revised["models"] == original["models"]
    assert revised["training_support"] == original["training_support"]
    assert revised["dataset_scope"] == original["dataset_scope"]
    assert revised["feature_contract"] == original["feature_contract"]
    assert set(revised["symbol_feature_contracts"]) == {"TEST"}
    assert revised["symbol_feature_contracts"]["TEST"]["native_interval_contract"] == CONTRACT
    assert revised["migrated_from_sha256"] == original["sha256"]
    with pytest.raises(ContractError):
        recipes.migrate_native_interval_recipe(original, pc="Atlas", symbols=("TEST", "OTHER"),
            native_symbols=("TEST",), installed_at="2026-05-11T12:00:00Z", authority_reference="fixture")
    with pytest.raises(ContractError):
        recipes.migrate_native_interval_recipe(original, pc="Scout", symbols=("TEST", "OTHER"),
            native_symbols=("OUTSIDE",), installed_at="2026-05-11T12:00:00Z", authority_reference="fixture")


def test_population_recipe_latches_until_next_start_and_does_not_weaken_support(tmp_path):
    from ml.intraday import live_model_recipe as recipes
    from ml.intraday.runtime import ResearchConfig
    original = frozen_recipe()
    revised = recipes.migrate_native_interval_recipe(original, pc="Scout", symbols=("TEST", "OTHER"),
        native_symbols=("TEST",), installed_at="2026-05-11T12:00:00Z", authority_reference="fixture local authority")
    path = tmp_path / "intraday/scout/manual-model-recipe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(revised), encoding="utf-8")
    config, runtime = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
                                                   pc="Scout", symbols=("TEST", "OTHER"))
    assert recipes.native_interval_symbols_for_runtime(runtime) == ("TEST",)
    path.write_text(json.dumps(original), encoding="utf-8")
    assert recipes.native_interval_symbols_for_runtime(runtime) == ("TEST",)
    _, restarted = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
                                                 pc="Scout", symbols=("TEST", "OTHER"))
    assert recipes.native_interval_symbols_for_runtime(restarted) == ()
    frame = dataset_frame()
    reference = OPEN + pd.Timedelta(minutes=15 * 35)
    with pytest.raises(recipes.ManualRecipeSupportError) as rejected:
        runtime["dataset_builder"](frame, cutoff=reference + pd.Timedelta(minutes=16), reference_at=reference,
                                   lookback_days=config.lookback_days, synthetic_policy="exclude")
    assert FEATURE_VERSION in str(rejected.value)
    with pytest.raises(ContractError):
        restarted["dataset_builder"](frame, cutoff=reference + pd.Timedelta(minutes=16), reference_at=reference,
                                     lookback_days=None, synthetic_policy="exclude")


def issued_forecast(*, native):
    from ml.intraday.prediction import TARGET_VERSION
    reference = OPEN - pd.Timedelta(minutes=30)
    value = dict(pc="Scout", symbol="TEST", cycle_id="native" if native else "legacy",
        reference_at=reference.isoformat(), reference_open=100.0, target_open=OPEN.isoformat(),
        target_close=CLOSE.isoformat(), issued_at=(reference + pd.Timedelta(minutes=16)).isoformat(),
        information_available_at=(reference + pd.Timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(reference + pd.Timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        source_id="databento:EQUS.MINI:ohlcv-1m", source_dataset="EQUS.MINI",
        mode="live", availability_basis="observed", probability_not_down=.6, configured_count=1,
        successful_count=1, ensemble_status="full", models=[dict(name="fixture", status="success", probability_not_down=.6)])
    if native:
        value.update(feature_version=FEATURE_VERSION, native_interval_contract=CONTRACT)
    return value


def test_native_target_cannot_reinterpret_legacy_forecast_and_reverse():
    candle = resolved()
    legacy = dict(symbol="TEST", opened_at=OPEN.isoformat(), available_at=CLOSE.isoformat(),
                  interval_seconds=900, open=100, high=109, low=99, close=108, volume=28,
                  synthetic=False, no_trade=False, evidence_json="{}")
    assert candle_matches_forecast(candle, issued_forecast(native=True))
    assert candle_matches_forecast(legacy, issued_forecast(native=False))
    assert not candle_matches_forecast(candle, issued_forecast(native=False))
    assert not candle_matches_forecast(legacy, issued_forecast(native=True))


@pytest.mark.parametrize("native_forecast,native_candle", [(True, False), (False, True)])
def test_store_direct_observe_rejects_opposite_target_contract(tmp_path, native_forecast, native_candle):
    from ml.intraday.store import ShadowStore
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    forecast = store.save_forecast(issued_forecast(native=native_forecast))
    candle = resolved()
    if not native_candle:
        candle = {key: value for key, value in candle.items() if key not in {
            "native_interval_contract", "source_empty_constituents", "observed_constituents", "synthetic_constituents"}}
        candle["evidence_json"] = "{}"
    with pytest.raises(ContractError):
        store.observe(forecast, as_of=CLOSE + pd.Timedelta(seconds=9), candle=candle)
    assert store.revisions(forecast["forecast_id"]) == []


def test_store_direct_observe_validates_native_proof_and_accepts_valid_target(tmp_path):
    from ml.intraday.store import ShadowStore
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    forecast = store.save_forecast(issued_forecast(native=True))
    candle = resolved()
    corrupted = {**candle, "close": candle["close"] + 1}
    with pytest.raises(ContractError):
        store.observe(forecast, as_of=CLOSE + pd.Timedelta(seconds=9), candle=corrupted)
    assert store.revisions(forecast["forecast_id"]) == []
    result = store.observe(forecast, as_of=CLOSE + pd.Timedelta(seconds=9), candle=candle)
    assert result["status"] == "scored"
    assert result["target_not_down"] is True


@pytest.mark.parametrize("variant", ["unknown_tag", "stripped_tag", "stripped_counts"])
def test_store_direct_observe_cannot_launder_native_proof_into_legacy_contract(tmp_path, variant):
    from ml.intraday.store import ShadowStore
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    forecast = store.save_forecast(issued_forecast(native=False))
    candle = resolved()
    if variant == "unknown_tag":
        candle["native_interval_contract"] = "unreviewed-native-contract"
    else:
        candle.pop("native_interval_contract")
        if variant == "stripped_counts":
            for name in ("source_empty_constituents", "observed_constituents", "synthetic_constituents"):
                candle.pop(name)
    assert not candle_matches_forecast(candle, forecast)
    with pytest.raises(ContractError):
        store.observe(forecast, as_of=CLOSE + pd.Timedelta(seconds=9), candle=candle)
    assert store.revisions(forecast["forecast_id"]) == []


@pytest.mark.parametrize("absent", [None, float("nan")])
def test_nullable_optional_native_columns_preserve_legacy_frame_compatibility(absent):
    legacy = dict(symbol="TEST", opened_at=OPEN.isoformat(), available_at=CLOSE.isoformat(),
                  interval_seconds=900, open=100, high=109, low=99, close=108, volume=28,
                  synthetic=False, no_trade=False, evidence_json="{}", native_interval_contract=absent,
                  source_empty_constituents=absent, observed_constituents=absent, synthetic_constituents=absent)
    assert completed_15m_observation(legacy, from_frame=True)
    assert candle_matches_forecast(legacy, issued_forecast(native=False))
    assert not candle_matches_forecast(legacy, issued_forecast(native=True))
