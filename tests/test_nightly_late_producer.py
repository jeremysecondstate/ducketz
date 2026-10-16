"""Date-pinned producer checks without nightly dispatch, providers, or training."""
from contextlib import nullcontext
from datetime import date
from inspect import signature

import pandas as pd
import pytest

from ml import nightly_gameplan as nightly
from ml.gameplan_champions import latest_promoted_champion as _find_champion
from ml.nightly_continuation import ENVIRONMENT_KEY, POLICY
from test_stock_only_gameplan import (
    ACTION_DATE,
    publication_fixture,
    test_independent_target_publication_uses_new_labels_and_preserves_native_contract
    as _exercise_independent_publication,
)


@pytest.fixture(autouse=True)
def isolated_continuation_environment(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)


def _sources():
    return pd.DataFrame([
        {"symbol": symbol, "action_date": date(2026, 10, 9),
         "source_action_start": pd.Timestamp("2026-10-09T11:00Z"),
         "decision_timestamp": pd.Timestamp("2026-10-09T00:05Z"),
         "information_available_at": pd.Timestamp("2026-10-09T00:05Z")}
        for symbol in ("AAPL", "MSFT")])


def test_late_route_selects_original_causal_rows_without_backdating():
    original = _sources()
    now = pd.Timestamp("2026-10-09T15:00Z")
    with pytest.raises(RuntimeError, match="No future"):
        nightly._current_overnight_sources(original, symbols=("AAPL", "MSFT"), as_of=now)
    rows, day = nightly._current_overnight_sources(original, symbols=("AAPL", "MSFT"),
        as_of=now, late_action_date="2026-10-09")
    assert day == date(2026, 10, 9)
    pd.testing.assert_frame_equal(rows, original)
    assert now == pd.Timestamp("2026-10-09T15:00Z")


@pytest.mark.parametrize("day, now", [
    ("2026-10-08", "2026-10-09T15:00Z"),
    ("2026-10-12", "2026-10-09T15:00Z"),
    ("2026-10-09", "2026-10-09T10:59Z"),
    ("2026-10-09", "2026-10-10T00:00Z"),
])
def test_late_route_rejects_wrong_date_and_closed_window(day, now):
    with pytest.raises(ValueError, match="today's open action session"):
        nightly._current_overnight_sources(_sources(), symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp(now), late_action_date=day)


@pytest.mark.parametrize("field", ["decision_timestamp", "information_available_at"])
@pytest.mark.parametrize("invalid", ["2026-10-09T11:00Z", "2026-10-09T12:00Z", None])
def test_late_route_rejects_same_day_or_missing_feature_clocks(field, invalid):
    rows = _sources()
    rows.loc[0, field] = pd.Timestamp(invalid) if invalid is not None else pd.NaT
    with pytest.raises(ValueError, match="predate"):
        nightly._current_overnight_sources(rows, symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-09T15:00Z"), late_action_date="2026-10-09")


def test_late_route_still_requires_complete_symbol_coverage():
    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        nightly._current_overnight_sources(_sources().iloc[:1], symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-09T15:00Z"), late_action_date="2026-10-09")


def test_continuation_keeps_causal_inputs_after_session_close(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-09")
    frame = _sources()
    before = frame.copy(deep=True)
    result, day = nightly._current_overnight_sources(frame, symbols=("AAPL", "MSFT"),
        as_of=pd.Timestamp("2026-10-12T15:00Z"), late_action_date="2026-10-09")
    assert day.isoformat() == "2026-10-09"
    pd.testing.assert_frame_equal(result, before)
    frame.loc[0, "information_available_at"] = pd.Timestamp("2026-10-09T12:00Z")
    with pytest.raises(ValueError, match="predate"):
        nightly._current_overnight_sources(frame, symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-12T15:00Z"), late_action_date="2026-10-09")


def test_continuation_for_another_night_cannot_select_closed_session(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-12")
    with pytest.raises(ValueError, match="today's open action session"):
        nightly._current_overnight_sources(_sources(), symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-12T15:00Z"), late_action_date="2026-10-09")


def test_preopen_dated_context_selects_the_original_session(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-09")
    original = _sources()
    rows, day = nightly._current_overnight_sources(original, symbols=("AAPL", "MSFT"),
        as_of=pd.Timestamp("2026-10-09T05:00Z"), late_action_date="2026-10-09")
    assert day == date(2026, 10, 9)
    pd.testing.assert_frame_equal(rows, original)


def test_cli_forwards_the_date_and_existing_preparation_options(tmp_path, monkeypatch):
    calls = []
    supports_recovery = "recovery_spec" in signature(nightly.run_nightly_gameplan_once).parameters
    monkeypatch.setattr(nightly, "resolve_datastore_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr(nightly, "exclusive_runtime_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(nightly, "run_nightly_gameplan_once",
                        lambda root, **kwargs: calls.append((root, kwargs)))
    feedback = tmp_path / "proposal.json"
    assert nightly.main([
        "--datastore", str(tmp_path), "--once", "--stock-only", "--independent-stock-horizons",
        "--late-action-date", "2026-10-09", "--archive-history",
        "--stock-price-source", "xnas-itch-archive-v1", "--model-feedback", str(feedback),
        "--probability-target-contract", nightly.RAW_DIRECTION_TARGET,
    ]) == 0
    assert calls == [(tmp_path, {
        "stock_only": True, "independent_stock_horizons": True,
        "stock_price_source": "xnas-itch-archive-v1", "archive_history": True,
        "model_feedback": feedback, "late_action_date": "2026-10-09",
        "probability_target_contract": nightly.RAW_DIRECTION_TARGET,
        **({"recovery_spec": None} if supports_recovery else {}),
    })]


def test_late_completion_at_close_preserves_pointer_and_unpublished_outputs(publication_fixture, monkeypatch):
    fixture = publication_fixture
    close = nightly._local_timestamp(ACTION_DATE, nightly.ACTION_END_HOUR)
    pointer = fixture.root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b"previous immutable publication")
    before = pointer.read_bytes()
    original_fit = fixture.fit

    def finish_at_close(*args, **kwargs):
        result = original_fit(*args, **kwargs)
        if kwargs["group"] == nightly.MODEL_GROUPS[-1]:
            fixture.clock[0] = close
        return result

    fixture.fit = finish_at_close
    with pytest.raises(RuntimeError, match="17:00 PT"):
        _exercise_independent_publication(fixture, monkeypatch, "canonical-equity-minute-v1", True)
    assert fixture.fits == list(nightly.MODEL_GROUPS)
    assert pointer.read_bytes() == before
    assert not list((fixture.root / "ml/nightly-gameplan-runs").glob("*/receipt.json"))
    assert len(list((fixture.root / "ml/nightly-gameplan-runs").glob("*/forecasts.parquet"))) == 1


def test_continued_completion_keeps_original_freeze_and_actual_publication(publication_fixture, monkeypatch):
    fixture = publication_fixture
    created = pd.Timestamp("2026-09-09T02:00Z")
    completed = pd.Timestamp("2026-09-12T03:00Z")
    original_fit = fixture.fit
    read_current = nightly.read_current_gameplan
    publications = []

    def capture_verified_publication(root):
        publication = read_current(root)
        publications.append(publication)
        return publication

    def finish_days_later(*args, **kwargs):
        assert kwargs["trained_at"] == created
        result = original_fit(*args, **kwargs)
        if kwargs["group"] == nightly.MODEL_GROUPS[-1]:
            fixture.clock[0] = completed
        return result

    fixture.fit = finish_days_later
    monkeypatch.setattr(nightly, "read_current_gameplan", capture_verified_publication)
    _exercise_independent_publication(fixture, monkeypatch, "canonical-equity-minute-v1", "continued")
    # The shared exerciser ends with intentional model-report corruption checks;
    # retain the fully verified publication it read before those negative cases.
    assert len(publications) == 2
    assert publications[0].receipt == publications[1].receipt
    publication = publications[0]
    configuration = publication.manifest["configuration"]
    forecasts = pd.read_parquet(publication.run_directory / "forecasts.parquet")
    assert forecasts.frozen_at.eq(created).all()
    assert pd.Timestamp(publication.receipt["published_at"]) == completed
    assert publication.receipt["action_date"] == ACTION_DATE.isoformat()
    assert configuration["continuation_policy"] == POLICY
    assert pd.Timestamp(configuration["training_information_cutoff"]) == (
        nightly._local_timestamp(ACTION_DATE, nightly.ACTION_START_HOUR) - pd.Timedelta(microseconds=1))


@pytest.mark.parametrize("created, continued", [
    ("2026-09-08T15:00Z", False),
    ("2026-09-09T02:00Z", True),
    ("2026-09-08T05:00Z", True),
])
def test_archive_clock_forwarding_preserves_causal_cutoff_and_real_acquisition(
        publication_fixture, monkeypatch, created, continued):
    fixture = publication_fixture
    fixture.clock[0] = pd.Timestamp(created)
    if continued:
        monkeypatch.setenv(ENVIRONMENT_KEY, ACTION_DATE.isoformat())
    cutoff = min(fixture.clock[0], nightly._local_timestamp(ACTION_DATE, nightly.ACTION_START_HOUR)
                 - pd.Timedelta(microseconds=1))
    selected = pd.DataFrame({"symbol": ["AAPL"]})
    archive = object()
    calls = []

    def select(*args, **kwargs):
        calls.append(("operational", kwargs["available_at"]))
        return selected

    def load(*args, **kwargs):
        calls.append(("archive", kwargs["available_at"], kwargs["evidence_available_at"]))
        return archive

    def combine(loaded, operational, **kwargs):
        assert loaded is archive and operational is selected
        return archive

    class CapturedArchiveClocks(Exception):
        pass

    def overlap(*args, **kwargs):
        calls.append(("seconds", kwargs["available_at"], kwargs["evidence_available_at"]))
        raise CapturedArchiveClocks

    monkeypatch.setattr(nightly, "select_prior_session_sources", select)
    monkeypatch.setattr("ml.gameplan_archive_features.load_archive_feature_sources", load)
    monkeypatch.setattr("ml.gameplan_archive_integration.combine_archive_sources", combine)
    monkeypatch.setattr("ml.gameplan_archive_seconds.verify_second_minute_overlap", overlap)
    with pytest.raises(CapturedArchiveClocks):
        nightly.run_nightly_gameplan_once(fixture.root, stock_only=True, independent_stock_horizons=True,
            archive_history=True, stock_price_source="xnas-itch-archive-v1",
            late_action_date=ACTION_DATE.isoformat(), reporter=None)
    assert calls == [
        ("operational", cutoff),
        ("archive", cutoff, fixture.clock[0]),
        ("seconds", cutoff, fixture.clock[0]),
    ]
    assert fixture.fits == []
    assert not (fixture.root / "ml/nightly-gameplan-latest/run.json").exists()


from gameplan_stats_fixture import write_review
from ml import gameplan_champions, gameplan_model_feedback
from test_gameplan_model_feedback import proposal
from test_nightly_feedback_producer import producer


@pytest.mark.parametrize("stats_session, causal", [("2026-09-05", True), ("2026-09-11", False)])
def test_continued_feedback_uses_only_preopen_outcomes(
        producer, publication_fixture, monkeypatch, stats_session, causal):
    now = pd.Timestamp("2026-09-12T06:00Z")
    publication_fixture.clock[0] = now
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-09-08")
    write_review(producer.root, session=stats_session)
    review_run = gameplan_model_feedback.prepare_feedback(producer.root, now=now)
    candidate = {"family": "logistic", "parameters": {"C": .1}}
    review = gameplan_model_feedback.save_feedback_review(
        producer.root, review_run, proposal(review_run, [candidate]), now=now)
    captured = []

    class ReachedCausalFit(Exception):
        pass

    def before_fit(*args, **kwargs):
        captured.append(kwargs)
        raise ReachedCausalFit

    monkeypatch.setattr(nightly, "_fit_group_model", before_fit)
    if causal:
        with pytest.raises(ReachedCausalFit):
            nightly.run_nightly_gameplan_once(producer.root, model_feedback=review,
                late_action_date="2026-09-08", **producer.kwargs)
        assert captured[0]["feedback_candidates"] == [candidate]
        assert pd.Timestamp(captured[0]["feedback_context"]["stats"]["outcomes_through"]) < (
            nightly._local_timestamp(ACTION_DATE, nightly.ACTION_START_HOUR))
    else:
        pointer = producer.root / "ml/nightly-gameplan-latest/run.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_bytes(b"prior publication remains selected")
        with pytest.raises(ValueError, match="feedback.*cutoff"):
            nightly.run_nightly_gameplan_once(producer.root, model_feedback=review,
                late_action_date="2026-09-08", **producer.kwargs)
        assert captured == []
        assert pointer.read_bytes() == b"prior publication remains selected"
        assert not (producer.root / "ml/nightly-gameplan-runs").exists()


@pytest.mark.parametrize("feedback", [False, True])
def test_continued_champion_lookup_excludes_postcutoff_publications(
        producer, publication_fixture, monkeypatch, feedback):
    from test_gameplan_champions import publish, DAY, SOURCE, STOCK_TRADER_SYMBOLS
    candidate_root = producer.root / "saved-champion-fixture"
    pre = publish(candidate_root, "20260908T080000.000000Z")
    post = publish(candidate_root, "20260909T020000.000000Z")
    now = pd.Timestamp("2026-09-12T06:00Z")
    publication_fixture.clock[0] = now
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-09-08")
    # The producer fixture replaces selection. Restore the actual saved-artifact
    # selector here to demonstrate which publication the producer admits.
    selected = []

    def choose(root, **kwargs):
        champion = _find_champion(candidate_root, group="1d", action_date=DAY,
            symbols=STOCK_TRADER_SYMBOLS, price_source=SOURCE, before=kwargs["before"],
            allow_prior_sessions=kwargs.get("allow_prior_sessions", False))
        selected.append(champion["run"])
        # Keep this probe separate from the producer fixture's model/grid shape.
        return None

    monkeypatch.setattr(gameplan_champions, "latest_promoted_champion", choose)
    nightly.run_nightly_gameplan_once(producer.root,
        model_feedback=producer.review if feedback else None,
        late_action_date="2026-09-08", **producer.kwargs)
    assert selected == [pre] * (4 if feedback else 1)
    assert post != pre


def test_continued_empty_feedback_baseline_keeps_default_candidates(
        producer, publication_fixture, monkeypatch):
    from ml.gameplan_actuals_review import publish_completed_session_review
    now = pd.Timestamp("2026-09-12T06:00Z")
    publication_fixture.clock[0] = now
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-09-08")
    publish_completed_session_review(producer.root, action_date="2026-09-11",
        clock=lambda: now, price_loader=lambda *a, **k: pytest.fail("No saved forecast to fetch"))
    review_run = gameplan_model_feedback.prepare_feedback(producer.root, now=now,
        probability_target=nightly.LEGACY_COST_TARGET)
    review = gameplan_model_feedback.save_feedback_review(
        producer.root, review_run, proposal(review_run), now=now)
    captured = []

    class ReachedDefaultFit(Exception):
        pass

    def before_fit(*args, **kwargs):
        captured.append(kwargs)
        raise ReachedDefaultFit

    monkeypatch.setattr(nightly, "_fit_group_model", before_fit)
    with pytest.raises(ReachedDefaultFit):
        nightly.run_nightly_gameplan_once(producer.root, model_feedback=review,
            late_action_date="2026-09-08", **producer.kwargs)
    assert captured[0]["feedback_candidates"] == []
    assert captured[0]["feedback_context"]["stats"]["baseline_no_saved_predictions"] is True
