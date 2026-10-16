"""Offline publication provenance checks; no publication or trading is started."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml.independent_stock_targets import stock_target_windows
from ml.artifacts import file_checksum, write_manifest
from ml.nightly_continuation import POLICY
from ml.nightly_gameplan import _publish_gameplan, read_current_gameplan
from ml.stock_trader import independent_signals as signals_module
from ml.stock_trader.independent_signals import (
    _validated_independent_forecasts,
    late_publication_time,
    load_current_independent_gameplan_signals,
)
from test_independent_stock_signals import ACTION_DATE, SYMBOLS, _frame, _publish


DAY = ACTION_DATE.isoformat()
PUBLISHED = "2026-09-08T16:30:00Z"


def opening(day=DAY):
    # Construct the actual wall-clock opening independently of elapsed-time math.
    return pd.Timestamp(f"{day} 04:00", tz="America/Los_Angeles").tz_convert("UTC")


def continuation(day=DAY, *, published=PUBLISHED):
    config = {
        "publication_mode": "NIGHTLY_CONTINUATION",
        "action_date": day,
        "late_action_date": day,
        "continuation_policy": POLICY,
        "training_information_cutoff": (opening(day) - pd.Timedelta(microseconds=1)).isoformat(),
    }
    return config, {"action_date": day, "published_at": published}


def recovery(day=DAY):
    start = opening(day)
    config = {
        "publication_mode": "LATE_RECOVERY",
        "action_date": day,
        "late_action_date": day,
        "training_information_cutoff": (start - pd.Timedelta(microseconds=1)).isoformat(),
        "late_preparation": {
            "sha256": "a" * 64,
            "authorization": {
                "schema_version": "nightly-preparation-recovery-v1",
                "action_date": day,
                "operator_authorized": True,
                "orders_authorized": False,
                "actor": "Scout",
                "authorization_reason": "Complete the dated preparation using retained causal inputs",
                "requested_at": (start + pd.Timedelta(hours=3)).isoformat(),
                "expires_at": (start + pd.Timedelta(hours=8)).isoformat(),
                "original_deadline_at": start.isoformat(),
                "training_information_cutoff": (start - pd.Timedelta(microseconds=1)).isoformat(),
            },
        },
    }
    return config, {"action_date": day, "published_at": (start + pd.Timedelta(hours=5)).isoformat()}


def validate(frame, published=PUBLISHED, *, day=DAY):
    return _validated_independent_forecasts(
        frame, action_date=day, symbols=SYMBOLS, late_publication_at=published,
    )


def dated_frame(day):
    specs = {spec["route"]: spec for spec in stock_target_windows(pd.Timestamp(day).date())}
    start = opening(day)
    return pd.DataFrame([
        {
            **row,
            **specs[row["route"]],
            "action_date": day,
            "decision_timestamp": start - pd.Timedelta(hours=5),
            "information_available_at": start - pd.Timedelta(hours=5),
            "frozen_at": start + pd.Timedelta(hours=5),
        }
        for row in _frame().to_dict("records")
    ])


def test_late_publication_preserves_exact_windows_and_input_times():
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00Z")
    before = frame.copy(deep=True)
    with pytest.raises(ValueError, match="future information"):
        validate(frame, None)
    result = validate(frame)
    for column in ("id", "decision_timestamp", "information_available_at", "frozen_at",
                   "target_window_start", "target_window_end"):
        pd.testing.assert_series_equal(result[column], before[column])
    pd.testing.assert_frame_equal(frame, before)
    assert result is not frame


@pytest.mark.parametrize("fault", [
    "input_at_open", "decision_after_open", "future_freeze", "before_open", "naive", "invalid",
])
def test_late_context_does_not_allow_future_inputs_or_fabricated_times(fault):
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00Z")
    published = PUBLISHED
    if fault == "input_at_open":
        frame["information_available_at"] = opening()
    elif fault == "decision_after_open":
        frame["decision_timestamp"] = opening() + pd.Timedelta(minutes=1)
    elif fault == "future_freeze":
        frame["frozen_at"] = pd.Timestamp("2026-09-08T16:31Z")
    elif fault == "before_open":
        published = (opening() - pd.Timedelta(minutes=1)).isoformat()
    elif fault == "naive":
        published = "2026-09-08T16:30"
    elif fault == "invalid":
        published = "NaT"
    before = frame.copy(deep=True)
    with pytest.raises(ValueError):
        validate(frame, published)
    pd.testing.assert_frame_equal(frame, before)


def test_only_explicit_matching_late_source_supplies_context():
    receipt = {"action_date": DAY, "published_at": PUBLISHED}
    assert late_publication_time({"publication_mode": "NIGHTLY"}, receipt) is None
    config = {"publication_mode": "LATE_RECOVERY", "action_date": DAY, "late_action_date": DAY}
    assert late_publication_time(config, receipt) == receipt["published_at"]
    for changed in (dict(config, late_action_date="2026-09-09"), dict(config, action_date="2026-09-09")):
        with pytest.raises(ValueError):
            late_publication_time(changed, receipt)
    with pytest.raises(ValueError):
        late_publication_time(config, {"action_date": DAY})


@pytest.mark.parametrize("mode", [None, "NIGHTLY"])
def test_ordinary_publication_does_not_acquire_late_context(mode):
    config, receipt = continuation()
    config["publication_mode"] = mode
    assert late_publication_time(config, receipt) is None
    # An absent late context still admits a normal causal, pre-opening forecast.
    pd.testing.assert_series_equal(validate(_frame(), None)["frozen_at"], _frame()["frozen_at"])


@pytest.mark.parametrize("fault", ["action_date", "late_action_date", "policy", "receipt_date"])
def test_continuation_requires_the_same_action_date_and_policy(fault):
    config, receipt = continuation()
    if fault == "policy":
        config["continuation_policy"] = "unknown-policy"
    elif fault == "receipt_date":
        receipt.pop("action_date")
    else:
        config[fault] = "2026-09-09"
    before = deepcopy((config, receipt))
    with pytest.raises(ValueError, match="inconsistent source evidence"):
        late_publication_time(config, receipt)
    assert (config, receipt) == before


@pytest.mark.parametrize("field", ["published_at", "training_information_cutoff"])
@pytest.mark.parametrize("value", [None, "NaT", "2026-09-08T10:00", "not-a-time"])
def test_continuation_rejects_missing_invalid_or_naive_times(field, value):
    config, receipt = continuation()
    (receipt if field == "published_at" else config)[field] = value
    with pytest.raises(ValueError):
        late_publication_time(config, receipt)


@pytest.mark.parametrize("offset", [pd.Timedelta(0), pd.Timedelta(microseconds=1)])
def test_continuation_inputs_must_precede_opening(offset):
    config, receipt = continuation()
    config["training_information_cutoff"] = (opening() + offset).isoformat()
    with pytest.raises(ValueError, match="causal inputs"):
        late_publication_time(config, receipt)


def test_continuation_cannot_publish_before_its_training_cutoff():
    config, receipt = continuation(published=(opening() - pd.Timedelta(minutes=1)).isoformat())
    with pytest.raises(ValueError, match="truthful publication times"):
        late_publication_time(config, receipt)


@pytest.mark.parametrize("offset", [-1, 0, 5])
def test_continuation_selects_late_context_only_at_or_after_opening(offset):
    published = (opening() + pd.Timedelta(hours=offset)).tz_convert("America/Los_Angeles").isoformat()
    config, receipt = continuation(published=published)
    config["training_information_cutoff"] = (opening() - pd.Timedelta(hours=2)).isoformat()
    before = deepcopy((config, receipt))
    assert late_publication_time(config, receipt) == (None if offset < 0 else published)
    assert (config, receipt) == before


@pytest.mark.parametrize("day,utc_open", [
    ("2026-03-06", "2026-03-06T12:00Z"),
    ("2026-03-09", "2026-03-09T11:00Z"),
    ("2026-10-30", "2026-10-30T11:00Z"),
    ("2026-11-02", "2026-11-02T12:00Z"),
])
def test_valid_trading_days_around_dst_use_actual_local_four_am(day, utc_open):
    start = pd.Timestamp(utc_open)
    assert opening(day) == start
    published = (start + pd.Timedelta(hours=5, minutes=30)).isoformat()
    config, receipt = continuation(day, published=published)
    assert late_publication_time(config, receipt) == published
    frame = dated_frame(day)
    before = frame.copy(deep=True)
    normalized = validate(frame, published, day=day)
    for column in ("id", "decision_timestamp", "information_available_at", "frozen_at",
                   "target_window_start", "target_window_end"):
        pd.testing.assert_series_equal(normalized[column], before[column])
    pd.testing.assert_frame_equal(frame, before)
    config["training_information_cutoff"] = start.isoformat()
    with pytest.raises(ValueError, match="causal inputs"):
        late_publication_time(config, receipt)
    frame["information_available_at"] = start
    with pytest.raises(ValueError, match="causal pre-session inputs"):
        validate(frame, published, day=day)


@pytest.mark.parametrize("actor", ["Scout", "Atlas"])
@pytest.mark.parametrize("mode", ["NIGHTLY", "LATE_RECOVERY"])
def test_bound_recovery_metadata_supplies_context_without_mutation(actor, mode):
    config, receipt = recovery()
    config["publication_mode"] = mode
    config["late_preparation"]["authorization"]["actor"] = actor
    before = deepcopy((config, receipt))
    assert late_publication_time(config, receipt) == receipt["published_at"]
    assert (config, receipt) == before


@pytest.mark.parametrize("field,value", [
    ("schema_version", "unknown"), ("action_date", "2026-09-09"),
    ("operator_authorized", False), ("operator_authorized", 1),
    ("orders_authorized", True), ("orders_authorized", 0),
    ("actor", "other"), ("authorization_reason", ""),
])
def test_recovery_metadata_does_not_expand_authority(field, value):
    config, receipt = recovery()
    config["late_preparation"]["authorization"][field] = value
    with pytest.raises(ValueError, match="inconsistent recovery evidence"):
        late_publication_time(config, receipt)


@pytest.mark.parametrize("fault", ["not_mapping", "missing_hash", "short_hash", "wrong_action_date", "late_action_date"])
def test_recovery_requires_bound_matching_source_metadata(fault):
    config, receipt = recovery()
    if fault == "not_mapping":
        config["late_preparation"] = []
    elif fault == "missing_hash":
        config["late_preparation"].pop("sha256")
    elif fault == "short_hash":
        config["late_preparation"]["sha256"] = "abc"
    elif fault == "wrong_action_date":
        config["action_date"] = "2026-09-09"
    else:
        config["late_action_date"] = "2026-09-09"
    with pytest.raises(ValueError):
        late_publication_time(config, receipt)


@pytest.mark.parametrize("field", ["requested_at", "expires_at", "original_deadline_at", "training_information_cutoff"])
@pytest.mark.parametrize("value", [None, "NaT", "2026-09-08T14:00"])
def test_recovery_rejects_missing_or_naive_authorization_times(field, value):
    config, receipt = recovery()
    config["late_preparation"]["authorization"][field] = value
    with pytest.raises(ValueError):
        late_publication_time(config, receipt)


@pytest.mark.parametrize("fault", [
    "deadline", "cutoff_not_last_microsecond", "configuration_cutoff", "requested_before_open",
    "publication_before_request", "publication_at_expiry", "more_than_seven_hours", "after_close",
])
def test_recovery_remains_within_its_fixed_window(fault):
    config, receipt = recovery()
    auth = config["late_preparation"]["authorization"]
    start = opening()
    if fault == "deadline":
        auth["original_deadline_at"] = (start + pd.Timedelta(seconds=1)).isoformat()
    elif fault == "cutoff_not_last_microsecond":
        auth["training_information_cutoff"] = (start - pd.Timedelta(seconds=1)).isoformat()
        config["training_information_cutoff"] = auth["training_information_cutoff"]
    elif fault == "configuration_cutoff":
        config["training_information_cutoff"] = (start - pd.Timedelta(seconds=1)).isoformat()
    elif fault == "requested_before_open":
        auth["requested_at"] = (start - pd.Timedelta(microseconds=1)).isoformat()
    elif fault == "publication_before_request":
        receipt["published_at"] = (start + pd.Timedelta(hours=2)).isoformat()
    elif fault == "publication_at_expiry":
        receipt["published_at"] = auth["expires_at"]
    elif fault == "more_than_seven_hours":
        auth["expires_at"] = (start + pd.Timedelta(hours=10, microseconds=1)).isoformat()
    else:
        auth["requested_at"] = (start + pd.Timedelta(hours=9)).isoformat()
        receipt["published_at"] = (start + pd.Timedelta(hours=10)).isoformat()
        auth["expires_at"] = (start + pd.Timedelta(hours=13, microseconds=1)).isoformat()
    with pytest.raises(ValueError, match="fixed recovery window or information cutoff"):
        late_publication_time(config, receipt)


def test_recovery_accepts_exact_opening_and_seven_hour_boundaries():
    config, receipt = recovery()
    auth = config["late_preparation"]["authorization"]
    auth["requested_at"] = opening().isoformat()
    auth["expires_at"] = (opening() + pd.Timedelta(hours=7)).isoformat()
    receipt["published_at"] = auth["requested_at"]
    assert late_publication_time(config, receipt) == receipt["published_at"]


@pytest.mark.parametrize("field", ["decision_timestamp", "information_available_at"])
def test_late_causality_includes_outlook_and_research_rows(field):
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00Z")
    frame.loc[~frame["execution_eligible"], field] = opening()
    with pytest.raises(ValueError, match="causal pre-session inputs"):
        validate(frame)


@pytest.mark.parametrize("fault", ["orders_enabled", "execution_authority", "research_as_execution"])
def test_late_context_does_not_grant_trading_or_promote_research_targets(fault):
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00Z")
    if fault == "orders_enabled":
        frame["broker_orders_enabled"] = True
    elif fault == "execution_authority":
        frame["execution_authority"] = "LIVE_ORDERS_AUTHORIZED"
    else:
        frame.loc[frame["route"].eq("1h@gap"), "target_role"] = "EXECUTION"
    before = frame.copy(deep=True)
    with pytest.raises(ValueError):
        validate(frame)
    pd.testing.assert_frame_equal(frame, before)


@pytest.fixture
def late_published_gameplan(tmp_path, monkeypatch):
    """Use real manifest/receipt verification over synthetic temporary outputs."""
    monkeypatch.setattr(signals_module, "STOCK_TRADER_SYMBOLS", SYMBOLS)
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00:15Z")
    run = _publish(tmp_path, frame)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    config, receipt = continuation(published="2026-09-08T16:00:30Z")
    write_manifest(
        run, run_timestamp=manifest["run_timestamp"], input_files=(),
        output_files=tuple(manifest["output_files"]),
        configuration={**manifest["configuration"], **config},
    )
    _publish_gameplan(
        tmp_path, run=run, action_date=ACTION_DATE,
        published_at=pd.Timestamp(receipt["published_at"]),
        source_loop_b="fixture", source_strategy=None,
    )
    return tmp_path, run, frame


def test_continuation_caller_emits_only_current_due_hour_and_preserves_grace(late_published_gameplan):
    root, run, frame = late_published_gameplan
    before = {path: file_checksum(path) for path in root.rglob("*") if path.is_file()}
    publication = read_current_gameplan(root)
    assert publication.manifest["configuration"]["publication_mode"] == "NIGHTLY_CONTINUATION"
    assert publication.receipt["published_at"] == "2026-09-08T16:00:30+00:00"
    for observed_at in ("2026-09-08T16:00:30Z", "2026-09-08T16:01:00Z"):
        signals, sources = load_current_independent_gameplan_signals(root, as_of=observed_at)
        assert set(signals) == {(symbol, "1h") for symbol in SYMBOLS}
        assert len(sources) == 6
        for (symbol, _), signal in signals.items():
            expected = frame.loc[frame.symbol.eq(symbol) & frame.route.eq("1h@09:00")].iloc[0]
            assert signal.prediction_id == expected["id"]
            assert pd.Timestamp(signal.prediction_created_at) == expected["frozen_at"]
            assert pd.Timestamp(signal.target_window_start) == expected["target_window_start"]
            assert pd.Timestamp(signal.target_window_end) == expected["target_window_end"]
            assert signal.actionable_until == "2026-09-08T16:05:00+00:00"
    expired, _ = load_current_independent_gameplan_signals(root, as_of="2026-09-08T16:05:00Z")
    assert expired == {}
    assert publication.receipt["broker_orders_enabled"] is False
    assert publication.receipt["orders_placed"] == 0
    pd.testing.assert_frame_equal(pd.read_parquet(run / "forecasts.parquet"), frame)
    assert {path: file_checksum(path) for path in root.rglob("*") if path.is_file()} == before


def test_continuation_caller_rejects_a_publication_not_yet_available(late_published_gameplan):
    root, _, _ = late_published_gameplan
    before = {path: file_checksum(path) for path in root.rglob("*") if path.is_file()}
    # The forecast already exists, but this publication has not yet happened.
    with pytest.raises(ValueError, match="publication|available"):
        load_current_independent_gameplan_signals(root, as_of="2026-09-08T16:00:20Z")
    assert {path: file_checksum(path) for path in root.rglob("*") if path.is_file()} == before
