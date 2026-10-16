"""Real recovery evidence through synthetic publication; never train or trade."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from gameplan_stats_fixture import write_review
from ml import gameplan_champions, gameplan_model_feedback, nightly_gameplan as nightly
from ml.gameplan_champions import latest_promoted_champion as select_champion
from ml.artifacts import file_checksum
from ml.nightly_recovery import make_recovery, verify_recovery, verify_saved_recovery
from ml.stock_trader.independent_signals import late_publication_time, _validated_independent_forecasts
from test_gameplan_model_feedback import proposal
from test_independent_stock_signals import _frame, SYMBOLS
from test_nightly_feedback_producer import producer
from test_stock_only_gameplan import publication_fixture


DAY = "2026-09-08"
OPENING = pd.Timestamp("2026-09-08T11:00Z")
REQUESTED = pd.Timestamp("2026-09-08T15:00Z")
CUTOFF = OPENING - pd.Timedelta(microseconds=1)


@pytest.fixture(autouse=True)
def independent_environment(monkeypatch):
    monkeypatch.delenv("DUCKETZ_NIGHTLY_ACTION_DATE", raising=False)


def save_recovery(root, requested=REQUESTED, *, actor="Scout", transform=None):
    payload = make_recovery({"actor": actor, "datastore": root}, requested)
    if transform is not None:
        transform(payload)
    path = root / "original-recovery.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def consumer_source(root, *, requested=REQUESTED, published=REQUESTED, actor="Scout", transform=None):
    path, payload = save_recovery(root, requested, actor=actor, transform=transform)
    evidence = verify_recovery(root, path, published, action_date=DAY)
    configuration = {"publication_mode": "NIGHTLY", "action_date": DAY, "late_action_date": None,
        "late_preparation": evidence,
        "training_information_cutoff": min(pd.Timestamp(requested), CUTOFF).isoformat()}
    return path, configuration, {"action_date": DAY, "published_at": pd.Timestamp(published).isoformat()}


@pytest.mark.parametrize("actor", ["Scout", "Atlas"])
@pytest.mark.parametrize("published", ["2026-09-08T15:30Z", "2026-09-08T22:00Z", "2026-09-12T03:00Z"])
def test_actual_recovery_roundtrip_survives_expiry_without_rewriting(tmp_path, actor, published):
    path, configuration, receipt = consumer_source(tmp_path, actor=actor, published=published)
    original = path.read_bytes()
    before = deepcopy((configuration, receipt))
    assert configuration["late_preparation"]["authorization"]["operator_authorized"] is False
    assert late_publication_time(configuration, receipt) == receipt["published_at"]
    assert verify_saved_recovery(tmp_path, configuration["late_preparation"], published)["sha256"] == file_checksum(path)
    assert path.read_bytes() == original
    assert (configuration, receipt) == before


@pytest.mark.parametrize("historical", ["absent", "false", "true", "numeric", "empty_reason"])
def test_historical_operator_fields_do_not_gate_actual_recovery(tmp_path, historical):
    def transform(payload):
        if historical == "absent":
            payload.pop("operator_authorized")
            payload.pop("authorization_reason")
        elif historical == "empty_reason":
            payload["authorization_reason"] = ""
        else:
            payload["operator_authorized"] = {"false": False, "true": True, "numeric": 1}[historical]
    _, configuration, receipt = consumer_source(tmp_path, published="2026-09-12T03:00Z", transform=transform)
    assert late_publication_time(configuration, receipt) == receipt["published_at"]


@pytest.mark.parametrize("published", ["2026-09-08T05:30Z", "2026-09-08T11:00Z", "2026-09-12T03:00Z"])
def test_preopen_recovery_keeps_earlier_information_cutoff(tmp_path, published):
    _, configuration, receipt = consumer_source(tmp_path, requested="2026-09-08T05:00Z", published=published)
    expected = None if pd.Timestamp(published) < OPENING else receipt["published_at"]
    assert late_publication_time(configuration, receipt) == expected
    assert pd.Timestamp(configuration["training_information_cutoff"]) < CUTOFF


def test_consumer_uses_bound_recovery_metadata_without_reading_private_path(tmp_path, monkeypatch):
    _, configuration, receipt = consumer_source(tmp_path)
    configuration["late_preparation"]["path"] = "Z:/peer-private/must-not-be-opened.json"
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Consumer must not read private recovery paths"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **k: pytest.fail("Consumer must not read private recovery paths"))
    assert late_publication_time(configuration, receipt) == receipt["published_at"]


@pytest.mark.parametrize("field", ["requested_at", "expires_at", "original_deadline_at", "training_information_cutoff"])
@pytest.mark.parametrize("invalid", [None, "NaT", "2026-09-08T15:00"])
def test_actual_recovery_consumer_still_rejects_invalid_clocks(tmp_path, field, invalid):
    _, configuration, receipt = consumer_source(tmp_path)
    configuration["late_preparation"]["authorization"][field] = invalid
    with pytest.raises(ValueError):
        late_publication_time(configuration, receipt)


@pytest.mark.parametrize("fault", ["future_request", "empty_window", "window_extension", "orders", "configuration_cutoff", "future_cutoff"])
def test_actual_recovery_consumer_retains_chronology_and_nontrading_scope(tmp_path, fault):
    _, configuration, receipt = consumer_source(tmp_path)
    auth = configuration["late_preparation"]["authorization"]
    if fault == "future_request":
        receipt["published_at"] = (REQUESTED - pd.Timedelta(seconds=1)).isoformat()
    elif fault == "empty_window":
        auth["expires_at"] = auth["requested_at"]
    elif fault == "window_extension":
        auth["expires_at"] = (REQUESTED + pd.Timedelta(hours=7, microseconds=1)).isoformat()
    elif fault == "orders":
        auth["orders_authorized"] = True
    elif fault == "configuration_cutoff":
        configuration["training_information_cutoff"] = OPENING.isoformat()
    else:
        _, configuration, receipt = consumer_source(tmp_path, requested="2026-09-08T05:00Z", published="2026-09-08T05:30Z")
        configuration["training_information_cutoff"] = "2026-09-08T06:00:00Z"
    with pytest.raises(ValueError):
        late_publication_time(configuration, receipt)


def use_independent_grid(monkeypatch):
    frame = _frame().drop(columns=["id"]).assign(
        forecast_anchor_local=lambda rows: rows.route.str.split("@", n=1).str[1],
        decision_timestamp=pd.Timestamp("2026-09-08T04:00Z"),
        information_available_at=pd.Timestamp("2026-09-08T04:00Z"),
        raw_probability=0.6)
    groups = {group: frame.loc[frame.model_group.eq(group)].copy() for group in nightly.MODEL_GROUPS}
    monkeypatch.setattr(nightly, "build_stock_current_groups", lambda *a, **k: groups)


@pytest.mark.parametrize("created,completed", [
    ("2026-09-08T05:00Z", "2026-09-08T05:30Z"),
    ("2026-09-08T15:00Z", "2026-09-08T15:30Z"),
    ("2026-09-08T15:00Z", "2026-09-12T03:00Z"),
    ("2026-09-12T02:00Z", "2026-09-12T03:00Z"),
])
def test_synthetic_producer_publication_roundtrip_retains_real_clocks_and_windows(
        producer, publication_fixture, monkeypatch, created, completed):
    requested = min(pd.Timestamp(created), REQUESTED)
    path, _ = save_recovery(producer.root, requested)
    original = path.read_bytes()
    publication_fixture.clock[0] = pd.Timestamp(created)
    use_independent_grid(monkeypatch)
    def fit(*args, **kwargs):
        assert kwargs["trained_at"] == pd.Timestamp(created)
        result = producer.fit(*args, **kwargs)
        if kwargs["group"] == nightly.MODEL_GROUPS[-1]:
            publication_fixture.clock[0] = pd.Timestamp(completed)
        return result
    monkeypatch.setattr(nightly, "_fit_group_model", fit)
    result = nightly.run_nightly_gameplan_once(producer.root, model_feedback=producer.review,
        recovery_spec=path, **producer.kwargs)
    publication = nightly.read_current_gameplan(producer.root)
    configuration = publication.manifest["configuration"]
    late = late_publication_time(configuration, publication.receipt)
    assert late == (None if pd.Timestamp(completed) < OPENING else publication.receipt["published_at"])
    forecasts = pd.read_parquet(result.run_directory / "forecasts.parquet")
    before = forecasts.copy(deep=True)
    checked = _validated_independent_forecasts(forecasts, action_date=DAY, symbols=SYMBOLS, late_publication_at=late)
    assert forecasts.frozen_at.eq(pd.Timestamp(created)).all()
    assert pd.Timestamp(publication.receipt["published_at"]) == pd.Timestamp(completed)
    assert pd.Timestamp(configuration["training_information_cutoff"]) == min(pd.Timestamp(created), CUTOFF)
    assert configuration["late_preparation"]["sha256"] == file_checksum(path)
    assert publication.receipt["orders_placed"] == 0
    for column in ("id", "decision_timestamp", "information_available_at", "frozen_at", "target_window_start", "target_window_end"):
        pd.testing.assert_series_equal(checked[column], before[column])
    pd.testing.assert_frame_equal(forecasts, before)
    assert path.read_bytes() == original


@pytest.mark.parametrize("causal", [False, True])
def test_recovery_feedback_admits_only_preopen_stats(producer, publication_fixture, monkeypatch, causal):
    now = pd.Timestamp("2026-09-12T06:00Z")
    publication_fixture.clock[0] = now
    path, _ = save_recovery(producer.root)
    write_review(producer.root, session="2026-09-05" if causal else "2026-09-11")
    run = gameplan_model_feedback.prepare_feedback(producer.root, now=now)
    candidate = {"family": "logistic", "parameters": {"C": .1}}
    review = gameplan_model_feedback.save_feedback_review(producer.root, run, proposal(run, [candidate]), now=now)
    captured = []
    class ReachedFit(Exception):
        pass
    def fit(*args, **kwargs):
        captured.append(kwargs)
        raise ReachedFit
    monkeypatch.setattr(nightly, "_fit_group_model", fit)
    if causal:
        with pytest.raises(ReachedFit):
            nightly.run_nightly_gameplan_once(producer.root, model_feedback=review, recovery_spec=path, **producer.kwargs)
        assert captured[0]["feedback_candidates"] == [candidate]
        assert pd.Timestamp(captured[0]["feedback_context"]["stats"]["outcomes_through"]) <= CUTOFF
    else:
        pointer = producer.root / "ml/nightly-gameplan-latest/run.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_bytes(b"previous immutable pointer")
        with pytest.raises(ValueError, match="feedback.*cutoff"):
            nightly.run_nightly_gameplan_once(producer.root, model_feedback=review, recovery_spec=path, **producer.kwargs)
        assert captured == []
        assert pointer.read_bytes() == b"previous immutable pointer"
        assert not (producer.root / "ml/nightly-gameplan-runs").exists()


def test_recovery_champion_selection_excludes_postcutoff_publications(producer, publication_fixture, monkeypatch):
    from test_gameplan_champions import publish, DAY as CHAMPION_DAY, SOURCE, STOCK_TRADER_SYMBOLS
    candidate_root = producer.root / "saved-champion-fixture"
    pre = publish(candidate_root, "20260908T080000.000000Z")
    post = publish(candidate_root, "20260909T020000.000000Z")
    publication_fixture.clock[0] = pd.Timestamp("2026-09-12T06:00Z")
    path, _ = save_recovery(producer.root)
    selected = []
    def choose(root, **kwargs):
        champion = select_champion(candidate_root, group="1d", action_date=CHAMPION_DAY,
            symbols=STOCK_TRADER_SYMBOLS, price_source=SOURCE, before=kwargs["before"],
            allow_prior_sessions=kwargs.get("allow_prior_sessions", False))
        selected.append(champion["run"])
        return None
    monkeypatch.setattr(gameplan_champions, "latest_promoted_champion", choose)
    nightly.run_nightly_gameplan_once(producer.root, model_feedback=producer.review, recovery_spec=path, **producer.kwargs)
    assert selected == [pre] * 4
    assert post != pre


def test_changed_recovery_bytes_keep_previous_pointer_and_unpublished_outputs(producer, publication_fixture, monkeypatch):
    path, payload = save_recovery(producer.root)
    publication_fixture.clock[0] = REQUESTED
    pointer = producer.root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b"previous immutable pointer")
    def fit(*args, **kwargs):
        result = producer.fit(*args, **kwargs)
        if kwargs["group"] == nightly.MODEL_GROUPS[-1]:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return result
    monkeypatch.setattr(nightly, "_fit_group_model", fit)
    with pytest.raises(ValueError, match="changed since this run"):
        nightly.run_nightly_gameplan_once(producer.root, model_feedback=producer.review, recovery_spec=path, **producer.kwargs)
    assert pointer.read_bytes() == b"previous immutable pointer"
    runs = producer.root / "ml/nightly-gameplan-runs"
    assert not list(runs.glob("*/receipt.json"))
    assert len(list(runs.glob("*/forecasts.parquet"))) == 1
