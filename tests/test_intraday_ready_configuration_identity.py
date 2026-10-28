"""Offline ready admission for identical integer/float offset encodings."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket

import pytest

from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION, digest, encoded
from ml.intraday.split_handoff import ReadySnapshots, ready_scientific_configuration_hashes


REF = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
SYMBOLS = ("AAPL", "IONQ")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Ready-identity fixtures must not contact providers or brokers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def configuration(offset):
    return dict(models=["fixture"], lookback_days=None, symbol_workers=2,
        publication_offset_seconds=offset, synthetic_policy="include",
        source_dataset="EQUS.MINI", retraining_minutes=15, use_supervised_models=False,
        population_contract=None, market_inputs_enabled=False)


def science(config):
    return {key: value for key, value in config.items()
            if key not in {"symbol_workers", "selected_models_by_symbol"}}


def publish(owner, offset):
    config = configuration(offset)
    configuration_hash = digest(config)
    config["selected_models_by_symbol"] = {"AAPL": {"original_recipe": "fixture"}}
    cutoff = (REF + STEP + timedelta(seconds=30)).isoformat()
    cycle = dict(pc="Atlas", symbols=list(SYMBOLS), reference_at=REF.isoformat(),
        mode="live", experiment="original-data-session", configuration=config,
        configuration_hash=configuration_hash, started_at=cutoff, training_cutoff=cutoff,
        supervised_model_policy=None)
    cycle_id = digest({key: cycle[key] for key in
                       ("pc", "reference_at", "mode", "experiment", "configuration_hash")})
    snapshot = dict(X=[[0., 1.], [1., 0.]], y=[0, 1], inference_X=[[1., 2.]],
        feature_names=["first", "second"],
        reference_at=[(REF - 4 * STEP).isoformat(), (REF - 3 * STEP).isoformat()],
        target_close_at=[(REF - STEP).isoformat(), REF.isoformat()],
        label_available_at=[(REF - STEP).isoformat(), REF.isoformat()], cutoff=cutoff,
        inference=dict(symbol="AAPL", reference_at=REF.isoformat(), reference_open=100,
            information_available_at=(REF + STEP + timedelta(seconds=2)).isoformat(),
            availability_kind="actual"),
        manifest=dict(target_version=TARGET_VERSION, training_rows=2),
        dataset_scope=dict(synthetic_policy="include", lookback_days=None),
        source_manifest=dict(source_dataset="EQUS.MINI", immutable_receipt="fixture"),
        model_selection=dict(models=[{"name": "fixture", "resolved_recipe": "original"}],
            selection="selected_manifest"),
        candles=[dict(symbol="AAPL", opened_at=REF.isoformat(),
            available_at=(REF + STEP + timedelta(seconds=2)).isoformat())])
    record = owner.publish(cycle_id, "AAPL", cycle, (digest(snapshot), snapshot))
    return record, cycle, snapshot


def owner_at(tmp_path):
    return ReadySnapshots(tmp_path / "ready", actor="Atlas", symbols=SYMBOLS,
                          clock=lambda: REF + STEP + timedelta(seconds=31))


def immutable_bytes(owner):
    return {str(path.relative_to(owner.root)): path.read_bytes()
            for kind in ("records", "cycles", "snapshots", "claims")
            for path in (owner.root / kind).rglob("*.json")}


def ready(owner, config, now=REF + STEP + timedelta(seconds=31)):
    return owner.ready(now, configuration_hash=digest(config),
        scientific_configuration_hashes=ready_scientific_configuration_hashes(config))


@pytest.mark.parametrize("offset", [0, 30, 899, 0.0, 30.0, 899.0])
def test_only_exact_integral_offset_encoding_has_two_hashes(offset):
    config = configuration(offset)
    config["selected_models_by_symbol"] = {"private_original": ["unchanged"]}
    before = encoded(config)
    identities = ready_scientific_configuration_hashes(config)
    assert identities[0] == digest(science(config))
    assert set(identities) == {digest({**science(config), "publication_offset_seconds": int(offset)}),
        digest({**science(config), "publication_offset_seconds": float(offset)})}
    assert len(identities) == 2
    assert encoded(config) == before


@pytest.mark.parametrize("offset", [30.5, 0.125, True, False, "30", None, -1, 900, 10 ** 1000])
def test_fractional_boolean_and_out_of_domain_offsets_gain_no_alias(offset):
    config = configuration(offset)
    assert ready_scientific_configuration_hashes(config) == (digest(science(config)),)


@pytest.mark.parametrize("offset", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_offsets_never_gain_a_hash(offset):
    with pytest.raises(ValueError):
        ready_scientific_configuration_hashes(configuration(offset))


@pytest.mark.parametrize("producer,consumer", [(30.0, 30), (30, 30.0)])
def test_legacy_offset_encodings_reuse_exact_original_objects(tmp_path, producer, consumer):
    owner = owner_at(tmp_path)
    record, cycle, snapshot = publish(owner, producer)
    config = configuration(consumer)
    config["symbol_workers"] = 1
    before = immutable_bytes(owner)
    with owner.connect() as db:
        index_before = db.execute("SELECT * FROM ready").fetchall()
    # The original mismatch is reproduced independently of the new aliases.
    assert owner.ready(owner.clock(), configuration_hash=digest(config),
        scientific_configuration_hash=digest(science(config))) == []
    assert ready(owner, config) == [record]
    assert owner.load(record) == (cycle, snapshot)
    assert immutable_bytes(owner) == before
    with owner.connect() as db:
        assert db.execute("SELECT * FROM ready").fetchall() == index_before
    assert record["configuration_hash"] == cycle["configuration_hash"]
    assert record["scientific_configuration_hash"] == digest(science(cycle["configuration"]))
    assert record["target_close"] == (REF + 3 * STEP).isoformat()


@pytest.mark.parametrize("field,value", [("publication_offset_seconds", 30.01),
    ("models", ["different"]), ("synthetic_policy", "exclude"),
    ("source_dataset", "different"), ("retraining_minutes", 30),
    ("population_contract", "different"), ("market_inputs_enabled", True),
    ("lookback_days", 10), ("use_supervised_models", True)])
def test_changed_science_does_not_match_offset_aliases(tmp_path, field, value):
    owner = owner_at(tmp_path)
    publish(owner, 30.0)
    config = configuration(30)
    config[field] = value
    assert ready(owner, config) == []


def test_legacy_full_and_singular_scientific_arguments_still_match(tmp_path):
    owner = owner_at(tmp_path)
    record, cycle, _ = publish(owner, 30.0)
    assert owner.ready(owner.clock(), configuration_hash=cycle["configuration_hash"]) == [record]
    assert owner.ready(owner.clock(), configuration_hash="different",
        scientific_configuration_hash=record["scientific_configuration_hash"]) == [record]


def test_aliases_do_not_bypass_original_availability_expiry_or_record_integrity(tmp_path):
    owner = owner_at(tmp_path)
    record, _, _ = publish(owner, 30.0)
    config = configuration(30)
    assert ready(owner, config, REF + STEP) == []
    assert ready(owner, config, REF + 3 * STEP) == []
    path = owner._path("records", record["record_sha256"])
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + b" ")
    assert ready(owner, config) == []
    assert owner.rejected == [{"symbol": "AAPL", "error_type": "ContractError"}]


def test_aliases_do_not_bypass_index_identity_or_snapshot_hash(tmp_path):
    owner = owner_at(tmp_path)
    record, _, _ = publish(owner, 30.0)
    config = configuration(30)
    with owner.connect() as db:
        db.execute("UPDATE ready SET configuration_hash=?", (digest(config),))
    assert ready(owner, config) == []
    assert owner.rejected
    with owner.connect() as db:
        db.execute("UPDATE ready SET configuration_hash=?", (record["configuration_hash"],))
    assert ready(owner, config) == [record]
    path = owner._path("snapshots", record["snapshot_hash"])
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + b" ")
    with pytest.raises(ContractError, match="changed immutable"):
        owner.load(record)


def integration_environment(tmp_path):
    # Portable handoff checks above run even on a public source without the
    # separately reviewed Atlas model adapters. Import guarded fixtures lazily.
    from tests.intraday_split_package_support import require_split_integration
    require_split_integration(__file__, files=("tests/test_intraday_split_services.py",),
        interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                    ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                     ("prepared_source", "forecast_preparer", "datastore"))))
    from tests.test_intraday_split_services import _environment
    return _environment(tmp_path)


@pytest.mark.parametrize("producer,consumer", [(30.0, 30), (30, 30.0)])
def test_model_pass_publishes_original_predictions_across_offset_encodings(tmp_path, producer, consumer):
    from dataclasses import asdict, replace
    env = integration_environment(tmp_path)
    env.runner.config = replace(env.config, publication_offset_seconds=producer)
    env.service.pass_once()
    env.config = replace(env.config, publication_offset_seconds=consumer)
    config_before = deepcopy(asdict(env.config))
    before = immutable_bytes(env.handoff)
    result = env.consume()
    assert result["status"] == "PUBLISHED"
    assert result["symbols"] == list(env.symbols)
    assert len(env.outputs) == 2
    assert all(output["probability_not_down"] == .6 for output in env.outputs)
    assert asdict(env.config) == config_before
    assert type(env.config.publication_offset_seconds) is type(consumer)
    assert immutable_bytes(env.handoff) == before


@pytest.mark.parametrize("later_minutes", [15, 30])
def test_model_pass_keeps_latest_reference_and_expiry_fences(tmp_path, later_minutes):
    from dataclasses import replace
    env = integration_environment(tmp_path)
    env.service.pass_once()
    env.config = replace(env.config, publication_offset_seconds=30)
    before = immutable_bytes(env.handoff)
    env.now += timedelta(minutes=later_minutes)
    result = env.consume(runner_factory=lambda **kwargs: pytest.fail("Old inputs admitted model work"))
    assert result["status"] == "WAITING_FOR_READY"
    assert env.outputs == []
    assert immutable_bytes(env.handoff) == before
