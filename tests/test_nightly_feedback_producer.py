"""Exercise producer feedback provenance with synthetic, never-fitted models."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from gameplan_stats_fixture import write_review
from ml import gameplan_champions, gameplan_model_feedback, nightly_gameplan as nightly
from ml.artifacts import file_checksum, verify_manifest, write_manifest
from test_gameplan_model_feedback import proposal
from test_stock_only_gameplan import FROZEN_AT, SYMBOLS, publication_fixture


@pytest.fixture
def producer(publication_fixture, monkeypatch):
    """Keep real feedback/Stats and publication IO; replace data prep and fitting."""
    base = publication_fixture
    source = base.root / "ml/runs/loop-b-fixture"
    write_manifest(source, run_timestamp=FROZEN_AT, input_files=[],
                   output_files=["samples.parquet", "predictions.parquet"],
                   feature_columns=["mr__test"], configuration={"symbols": list(SYMBOLS)})
    manifest = verify_manifest(source)
    monkeypatch.setattr(nightly, "read_current_publication", lambda root: SimpleNamespace(
        run_directory=source, manifest=manifest))
    sources = pd.read_parquet(source / "samples.parquet").assign(
        source_feature_cutoff=pd.Timestamp("2026-09-05T00:00Z"))
    monkeypatch.setattr(nightly, "select_prior_session_sources", lambda *a, **k: sources)
    monkeypatch.setattr(nightly, "_current_overnight_sources", lambda *a, **k: (
        sources, pd.Timestamp("2026-09-08").date()))
    groups = {group: base.expected.loc[base.expected.model_group.eq(group)].copy()
              for group in nightly.MODEL_GROUPS}
    training = {group: frame.assign(target=[i % 2 for i in range(len(frame))])
                for group, frame in groups.items()}
    monkeypatch.setattr(nightly, "build_stock_training_groups", lambda *a, **k: training)
    monkeypatch.setattr(nightly, "build_stock_current_groups", lambda *a, **k: groups)
    calls, champion_calls = [], []

    def fit(frame, *, current, feature_columns, group, model_directory, trained_at, **kwargs):
        calls.append({"group": group, **copy.deepcopy(kwargs)})
        model_directory.mkdir(parents=True)
        path = model_directory / "model.joblib"
        path.write_bytes(b"synthetic model; no training performed")
        report = {
            "selected_family": "synthetic-challenger",
            "promotion_gate": {"status": current.model_status.iloc[0]},
            "partitions": {"assessment_rows": 10},
            "model_file": {"path": f"models/{group}/model.joblib", "size": path.stat().st_size,
                           "checksum_sha256": file_checksum(path)},
        }
        if "feedback_candidates" in kwargs:
            report["model_feedback"] = {"input_binding": kwargs["feedback_context"],
                "candidates": kwargs["feedback_candidates"], "assessment_used_for_selection": False,
                "selected_family": "synthetic-challenger", "group": group}
        return {"forecasts": current.copy(), "report": report}

    def champion(root, **kwargs):
        champion_calls.append(kwargs)
        return None

    monkeypatch.setattr(nightly, "_fit_group_model", fit)
    monkeypatch.setattr(gameplan_champions, "latest_promoted_champion", champion)
    stats = write_review(base.root, session="2026-09-06")
    review_run = gameplan_model_feedback.prepare_feedback(base.root, now=FROZEN_AT)
    response = proposal(review_run, [{"family": "logistic", "parameters": {"C": .1}}])
    review = gameplan_model_feedback.save_feedback_review(base.root, review_run, response, now=FROZEN_AT)
    return SimpleNamespace(root=base.root, source=source, stats=stats, review=review,
        proposal=response, calls=calls, champion_calls=champion_calls, fit=fit,
        kwargs={"stock_only": True, "independent_stock_horizons": True,
                "probability_target_contract": nightly.LEGACY_COST_TARGET, "reporter": None})


def run_feedback(fixture):
    return nightly.run_nightly_gameplan_once(fixture.root, model_feedback=fixture.review, **fixture.kwargs)


def test_producer_default_does_not_load_feedback_or_lookup_prior_sessions(producer, monkeypatch):
    monkeypatch.setattr(gameplan_model_feedback, "load_feedback_review",
                        lambda *a, **k: pytest.fail("Default path must not load feedback"))
    result = nightly.run_nightly_gameplan_once(producer.root, **producer.kwargs)
    publication = nightly.read_current_gameplan(producer.root)
    assert [call["group"] for call in producer.calls] == list(nightly.MODEL_GROUPS)
    assert all(set(call) == {"group"} for call in producer.calls)
    assert len(producer.champion_calls) == 1
    assert producer.champion_calls[0]["group"] == "1w"
    assert "allow_prior_sessions" not in producer.champion_calls[0]
    assert "model-feedback-results.json" not in publication.manifest["output_files"]
    assert not (result.run_directory / "model-feedback-results.json").exists()


def test_producer_binds_review_stats_models_and_forwards_each_group(producer):
    result = run_feedback(producer)
    publication = nightly.read_current_gameplan(producer.root)
    results_path = result.run_directory / "model-feedback-results.json"
    results = json.loads(results_path.read_text())
    binding = results["input_binding"]
    assert binding["review_sha256"] == file_checksum(producer.review)
    assert binding["samples_sha256"] == file_checksum(producer.source / "samples.parquet")
    assert binding["loop_b_manifest_sha256"] == file_checksum(producer.source / "manifest.json")
    assert binding["stats"]["receipt_sha256"] == file_checksum(producer.stats / "receipt.json")
    assert publication.manifest["output_files"]["model-feedback-results.json"]["checksum_sha256"] == file_checksum(results_path)
    inputs = {Path(entry["path"]).as_posix(): entry for entry in publication.manifest["input_files"]}
    for path in (producer.review, producer.review.parent / "diagnostics.json",
                 *(producer.stats / name for name in ("receipt.json", "manifest.json", "forecast-results.parquet", "report.json"))):
        entry = inputs[path.relative_to(producer.root).as_posix()]
        assert entry["status"] == "present"
        assert entry["checksum_sha256"] == file_checksum(path)
    assert len(producer.champion_calls) == 4
    assert all(call["allow_prior_sessions"] is True for call in producer.champion_calls)
    assert results["assessment_used_for_candidate_selection"] is False
    for call in producer.calls:
        group = call["group"]
        assert call["feedback_candidates"] == producer.proposal["groups"][group]["candidates"]
        assert call["feedback_context"] == binding
        assert call["incumbent_specification"] is None
        assert results["groups"][group]["candidate_evaluation"]["input_binding"] == binding
        assert results["groups"][group]["accepted_family"] == "synthetic-challenger"
        assert results["groups"][group]["accepted_model"]["path"] == f"models/{group}/model.joblib"


def test_retained_champion_keeps_new_challenger_evaluation(producer, monkeypatch):
    specification = {"family": "logistic", "parameters": {"C": 1.0}}
    evidence = producer.root / "retained-champion-input.json"
    evidence.write_text("{}")
    champion = {"files": (evidence,), "report": {"model_feedback": {"selected_specification": specification}}}
    monkeypatch.setattr(gameplan_champions, "latest_promoted_champion", lambda *a, **k: champion)
    retained = []

    def retain(challenger, *, champion, current, run, group, frozen_at):
        retained.append(group)
        name = f"models/{group}/champion.joblib"
        path = run / name
        path.write_bytes(b"synthetic retained champion")
        report = {**challenger["report"], "selected_family": "synthetic-retained",
            "promotion_gate": {"status": "PROMOTED"},
            "model_feedback": {"old_champion_feedback": True},
            "model_file": {"path": name, "size": path.stat().st_size, "checksum_sha256": file_checksum(path)},
            "deployment": {"status": "RETAINED_VERIFIED_CHAMPION"}}
        return {"forecasts": current.assign(model_status="PROMOTED"), "report": report}, (name,)

    monkeypatch.setattr(gameplan_champions, "retain_champion", retain)
    result = run_feedback(producer)
    publication = nightly.read_current_gameplan(producer.root)
    results = json.loads((result.run_directory / "model-feedback-results.json").read_text())
    assert retained == ["1w"]
    assert all(call["incumbent_specification"] == specification for call in producer.calls)
    weekly = results["groups"]["1w"]
    assert weekly["candidate_evaluation"]["selected_family"] == "synthetic-challenger"
    assert "old_champion_feedback" not in weekly["candidate_evaluation"]
    assert weekly["accepted_family"] == "synthetic-retained"
    assert weekly["accepted_model"]["path"] == "models/1w/champion.joblib"
    assert weekly["deployment"]["status"] == "RETAINED_VERIFIED_CHAMPION"
    assert "models/1w/champion.joblib" in publication.manifest["output_files"]
    assert any(Path(entry["path"]).as_posix() == evidence.relative_to(producer.root).as_posix()
               for entry in publication.manifest["input_files"])


@pytest.mark.parametrize("when", ["after_read", "during_fit", "after_manifest"])
@pytest.mark.parametrize("target", ["samples", "loop_b_manifest", "review", "new_stats"])
def test_mutated_feedback_inputs_preserve_previous_pointer(producer, monkeypatch, when, target):
    pointer = producer.root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b"previous immutable publication pointer")
    original = pointer.read_bytes()
    mutations = []
    original_samples = pd.read_parquet(producer.source / "samples.parquet")

    def mutate():
        assert not mutations
        mutations.append(target)
        if target == "samples":
            original_samples.assign(mr__test=99).to_parquet(producer.source / "samples.parquet", index=False)
        elif target == "loop_b_manifest":
            path = producer.source / "manifest.json"
            value = json.loads(path.read_text())
            value["fixture_revision"] = "replaced after consumption"
            path.write_text(json.dumps(value))
        elif target == "review":
            value = json.loads(producer.review.read_text())
            value["proposal"]["reviewed_by"] = "replacement fixture reviewer"
            producer.review.write_text(json.dumps(value))
        else:
            write_review(producer.root, session="2026-09-06", version="02")

    if when == "after_read":
        real_read = pd.read_parquet

        def read_then_mutate(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if Path(path) == producer.source / "samples.parquet" and not mutations:
                mutate()
            return value

        monkeypatch.setattr(pd, "read_parquet", read_then_mutate)
    elif when == "during_fit":
        def fit_then_mutate(*args, **kwargs):
            value = producer.fit(*args, **kwargs)
            if kwargs["group"] == "1h":
                mutate()
            return value

        monkeypatch.setattr(nightly, "_fit_group_model", fit_then_mutate)
    else:
        real_write = nightly.write_manifest

        def manifest_then_mutate(*args, **kwargs):
            value = real_write(*args, **kwargs)
            mutate()
            return value

        monkeypatch.setattr(nightly, "write_manifest", manifest_then_mutate)
    with pytest.raises((ValueError, RuntimeError), match="changed|differ|mismatch|review fresh"):
        run_feedback(producer)
    assert mutations == [target]
    assert pointer.read_bytes() == original
    assert not list((producer.root / "ml/nightly-gameplan-runs").glob("*/receipt.json"))


@pytest.mark.parametrize("with_feedback", [False, True])
def test_producer_cli_forwards_optional_feedback_path(tmp_path, monkeypatch, with_feedback):
    calls = []
    monkeypatch.setattr(nightly, "run_nightly_gameplan_once", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(nightly, "exclusive_runtime_lock", lambda *a, **k: nullcontext())
    review = tmp_path / "reviewed-proposal.json"
    argv = ["--datastore", str(tmp_path), "--stock-only", "--independent-stock-horizons"]
    if with_feedback:
        argv += ["--model-feedback", str(review)]
    assert nightly.main(argv) == 0
    assert len(calls) == 1
    assert calls[0][1]["model_feedback"] == (review if with_feedback else None)
    assert calls[0][1]["stock_only"] is True
    assert calls[0][1]["independent_stock_horizons"] is True
