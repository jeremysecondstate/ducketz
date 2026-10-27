"""Publish later original outcomes as explicit immutable Gameplan Stats revisions.

Completed-session handoffs keep their original closing cutoff. This separate
scientific-only contract updates their saved outcomes after longer targets
mature, without changing the original plan, forecast, package or publication.
The existing Stats reader consumes each newly materialized revision normally.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from app.ui.gameplan_stats_data import _outcome, load_gameplan_stats
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, verify_manifest, write_manifest, utc_timestamp
from ml.gameplan_actuals_review import RUNS, VERSION, _write_json
from ml.gameplan_evaluation import read_evaluation_history
from ml.gameplan_stats_handoff import ROW_FIELDS, _combined_report
from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE
from ml.stock_direction_policy import saved_direction_policy_summary

REVISION_VERSION = "cross-pc-gameplan-stats-outcome-revision-v1"
DIRECT_REVISION_VERSION = "cross-pc-gameplan-stats-outcome-revision-v2"
_DIRECT_NATIVE_SOURCES = {
    "databento:XNAS.ITCH:ohlcv-1m": ("XNAS.ITCH", 60),
    "databento:EQUS.MINI:ohlcv-1m": ("EQUS.MINI", 60),
    "schwab:pricehistory:1m": ("SCHWAB.PRICEHISTORY", 60),
    "schwab:pricehistory:5m": ("SCHWAB.PRICEHISTORY", 300),
    "fmp:historical-chart:5min": ("FMP.HISTORICAL_CHART_5MIN", 300),
}
_DIRECT_CONTRACTS = {
    "observed-multi-source-stock-v1": ("OBSERVED_SOURCE_SELECTION_V1", tuple(_DIRECT_NATIVE_SOURCES)[:4]),
    "observed-multi-source-stock-v2": ("OBSERVED_SOURCE_SELECTION_V2", tuple(_DIRECT_NATIVE_SOURCES)),
}
_OUTCOMES = ("actuals_status", "direction_correct", "model_observed_target", "model_brier_score",
             "actual_return", "actual_start_observed_at", "actual_end_observed_at")
_EVIDENCE = ("evaluated_at", "target_observation_source", "target_observation_dataset",
             "target_observation_entry_input_sha256", "target_observation_exit_input_sha256")
_TOP = {"schema_version", "action_date", "reviewed_at", "outcomes_through", "parent",
        "source_evaluation", "rows", "updates", "newly_mature_awaiting_ids", "orders_placed", "broker_orders_enabled"}
_FILES = ("receipt.json", "manifest.json", "forecast-results.parquet", "report.json")
# Older completed-session producers omit these optional fields from ROW_FIELDS.
# A revision still freezes every recorded policy field, including absence.
_ROW_FIELDS = tuple(dict.fromkeys((*ROW_FIELDS, "direction_policy_version",
                                 "direction_up_threshold", "direction_down_threshold")))


def _scalar(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def _instant(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Outcome revision requires timezone-aware evidence")
    return stamp.tz_convert("UTC")


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _projection(frame):
    rows = [{name: _scalar(row[name]) for name in _ROW_FIELDS if name in row}
            for row in frame.to_dict("records")]
    for row in rows:
        for key in ("target_window_start", "target_window_end", "actual_start_observed_at", "actual_end_observed_at"):
            if row.get(key) is not None:
                row[key] = _instant(row[key]).isoformat()
    return rows


def _equal(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _row_equal(left, right):
    if set(left) != set(right):
        return False
    return all((_instant(left[key]) == _instant(right[key]) if "observed_at" in key or key.startswith("target_window_")
                else _equal(left[key], right[key])) if left[key] is not None and right[key] is not None
               else left[key] is right[key] for key in left)


def _base(root, run, expected_receipt):
    root, run = Path(root).resolve(), Path(run).resolve()
    frozen = {name: file_checksum(run / name) for name in _FILES}
    if frozen["receipt.json"] != expected_receipt:
        raise ValueError("Outcome revision parent receipt differs from its reviewed hash")
    review = load_gameplan_stats(root, run_directory=run, expected_receipt_sha256=expected_receipt)
    frame = pd.read_parquet(run / "forecast-results.parquet")
    report = json.loads((run / "report.json").read_bytes())
    if report.get("review_mode") not in {"combined-atlas-scout", "original-outcome-revision"}:
        raise ValueError("Outcome revisions require a combined saved Stats publication")
    if frame.empty:
        raise ValueError("Outcome revision has no original forecasts")
    producers=report.get("producers",{})
    if (set(producers)!={"Atlas","Scout"} or any(not isinstance(item,dict)
            or not _digest(item.get("package_sha256")) for item in producers.values())):
        raise ValueError("Outcome revision parent lacks exact Atlas and Scout package provenance")
    projected = _projection(frame)
    for row in projected:
        _outcome(row)  # Validate excluded research rows as well as displayed rows.
    verify_manifest(run)
    if any(file_checksum(run / name) != digest for name,digest in frozen.items()):
        raise ValueError("Outcome revision parent changed during its snapshot")
    parent = dict(frozen)
    parent.update(action_date=review.session, revision_sha256=report.get("outcome_revision_sha256"),
        scientific_snapshot_sha256=hashlib.sha256(_bytes(projected)).hexdigest(),
        outcomes_through=_instant(review.outcomes_through).isoformat(),
        producer_package_sha256={actor: value["package_sha256"] for actor,value in report["producers"].items()})
    return review, frame, report, parent


def _compatible(original, evaluation):
    fields = {"id": "source_forecast_id", "symbol": "symbol", "action_date": "action_date",
              "route": "route", "model_group": "model_group", "model_status": "model_status",
              "calibrated_probability": "predicted_probability", "direction": "direction",
              "direction_policy_version": "direction_policy_version", "assumed_round_trip_cost": "assumed_round_trip_cost",
              "probability_target_contract": "probability_target_contract", "gameplan_variant": "gameplan_variant"}
    for left, right in fields.items():
        if left in original and not _equal(original[left], _scalar(evaluation.get(right))):
            return False
    return all(_instant(original[key]) == _instant(evaluation[key]) for key in ("target_window_start", "target_window_end"))


def _validate_update(original, update, cutoff):
    if set(update) != {"id", *_OUTCOMES, *_EVIDENCE} or update["id"] != original["id"]:
        raise ValueError("Outcome revision update has unsupported fields or identity")
    if update["actuals_status"] != "EVALUATED":
        raise ValueError("Outcome revision may add only evaluated original outcomes")
    candidate = dict(original, **{key: update[key] for key in _OUTCOMES})
    outcome = _outcome(candidate)
    stamp = _instant(update["evaluated_at"])
    start, end = _instant(outcome.start), _instant(outcome.end)
    opened, closed = _instant(outcome.observed_start), _instant(outcome.observed_end)
    if not start < end <= stamp <= cutoff or not opened < closed <= stamp:
        raise ValueError("Outcome revision precedes its target maturity or evidence availability")
    gap = outcome.role == "OPENING_GAP_RESEARCH"
    for boundary, observed, close in ((start, opened, gap), (end, closed, not gap)):
        distance = boundary - observed if close else observed - boundary
        if not pd.Timedelta(0) <= distance <= STOCK_TARGET_BOUNDARY_TOLERANCE:
            raise ValueError("Outcome revision endpoint differs from its original target boundary")
    if not all(isinstance(update[key], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",update[key]) for key in _EVIDENCE[1:3]):
        raise ValueError("Outcome revision lacks its original observation source")
    if not all(_digest(update[key]) for key in _EVIDENCE[3:]):
        raise ValueError("Outcome revision lacks exact original observation hashes")
    return candidate


def build_revision(root, *, base_run, expected_base_receipt_sha256, reviewed_at=None):
    """Derive a portable revision from verified local cumulative science only.

    No acquisition, scoring, model, broker or execution operation is performed.
    Without new observed outcomes or actual clock maturity, callers reuse the
    prior publication instead of producing another revision.
    """
    root = Path(root).resolve()
    now = _instant(utc_timestamp(reviewed_at))
    review, frame, report, parent = _base(root, base_run, expected_base_receipt_sha256)
    if now < _instant(review.reviewed_at) or now < _instant(review.outcomes_through):
        raise ValueError("Outcome revision cannot precede its parent publication")
    evaluation_pointer = root / "ml/gameplan-evaluation-latest/run.json"
    if not evaluation_pointer.is_file():
        raise ValueError("No verified cumulative scientific outcomes are available")
    pointer_sha = file_checksum(evaluation_pointer)
    pointer = json.loads(evaluation_pointer.read_bytes())
    history_run = (root / pointer["current"]["run_path"]).resolve()
    if history_run.parent != root / "ml/gameplan-evaluation-runs":
        raise ValueError("Cumulative scientific source escapes its saved runs")
    source_files = ("receipt.json", "manifest.json", "evaluations.parquet", "summary.json")
    source_hashes = {name:file_checksum(history_run / name) for name in source_files}
    history = read_evaluation_history(root)
    if history is None:
        raise ValueError("No verified cumulative scientific outcomes are available")
    history_report = json.loads((history.run_directory / "summary.json").read_bytes())
    if _instant(history_report["observed_at"]) > now:
        raise ValueError("Cumulative scientific evidence is newer than this review")
    original_rows = _projection(frame)
    updated_rows, updates, mature_ids = [], [], []
    for original in original_rows:
        # A completed-session score remains the original score even when a
        # different scientific evaluation later observed another endpoint.
        if original["actuals_status"] == "EVALUATED":
            updated_rows.append(original)
            continue
        eligible = history.evaluations.loc[
            history.evaluations.source_forecast_id.eq(original["id"])
            & history.evaluations.evaluation_status.eq("EVALUATED")]
        matches = [item for item in eligible.to_dict("records") if _compatible(original, item)]
        if len(matches) > 1:
            raise ValueError("Outcome revision original identity is ambiguous across saved editions")
        if not matches:
            if original["actuals_status"] == "PENDING_MATURITY" and _instant(original["target_window_end"]) <= now:
                updated_rows.append(dict(original,actuals_status="MATURE_AWAITING_DATA"))
                mature_ids.append(original["id"])
            else:
                updated_rows.append(original)
            continue
        item = matches[0]
        required_proof = (*_EVIDENCE, "observed_open_timestamp", "observed_close_timestamp")
        if any(key not in item or _scalar(item[key]) is None for key in required_proof):
            raise ValueError("Cumulative original outcome lacks recorded endpoint provenance; retain original inputs and produce supported observed evidence")
        change = float(item["observed_return"])
        direction = original["direction"]
        correct = None if direction == "NO_EDGE" else change > 0 if direction == "BULLISH" else change < 0
        update = dict(id=original["id"], actuals_status="EVALUATED", direction_correct=correct,
            model_observed_target=_scalar(item["observed_target"]), model_brier_score=_scalar(item["brier_score"]),
            actual_return=change, actual_start_observed_at=_scalar(item["observed_open_timestamp"]),
            actual_end_observed_at=_scalar(item["observed_close_timestamp"]),
            **{key: _scalar(item[key]) for key in _EVIDENCE})
        candidate = _validate_update(original, update, now)
        updates.append(update)
        updated_rows.append(candidate)
    if not updates and not mature_ids:
        raise ValueError("No new compatible evaluated original outcomes; reuse the saved publication")
    payload = dict(schema_version=REVISION_VERSION, action_date=review.session,
        reviewed_at=now.isoformat(), outcomes_through=now.isoformat(), parent=parent,
        source_evaluation={name: source_hashes[name]
                           for name in ("receipt.json", "manifest.json", "evaluations.parquet")},
        rows=updated_rows, updates=updates, newly_mature_awaiting_ids=mature_ids,orders_placed=0, broker_orders_enabled=False)
    _validated_frame(payload, frame, parent, now)
    verify_manifest(history_run)
    if (file_checksum(evaluation_pointer) != pointer_sha or history.run_directory != history_run
            or any(file_checksum(history_run / name) != digest for name,digest in source_hashes.items())):
        raise ValueError("Cumulative scientific source changed during revision snapshot")
    if _base(root, base_run, expected_base_receipt_sha256)[3] != parent:
        raise ValueError("Outcome revision parent changed during derivation")
    return payload


def _validated_frame(payload, base_frame, parent, now):
    direct = isinstance(payload, dict) and payload.get("schema_version") == DIRECT_REVISION_VERSION
    expected_top = _TOP | {"source_observations"} if direct else _TOP
    if (not isinstance(payload, dict) or set(payload) != expected_top
            or payload.get("schema_version") not in {REVISION_VERSION, DIRECT_REVISION_VERSION}
            or payload.get("orders_placed") != 0 or isinstance(payload.get("orders_placed"), bool)
            or payload.get("broker_orders_enabled") is not False):
        raise ValueError("Unsupported or unsanitized original-outcome revision")
    origin = payload["parent"]
    stable = {"action_date", "revision_sha256", "scientific_snapshot_sha256", "outcomes_through", "producer_package_sha256"}
    if (not isinstance(origin, dict) or set(origin) != set(parent)
            or any(origin.get(key) != parent[key] for key in stable)
            or not all(_digest(origin.get(key)) for key in _FILES)
            or payload["action_date"] != parent["action_date"]):
        raise ValueError("Outcome revision parent publication differs")
    reviewed, cutoff = _instant(payload["reviewed_at"]), _instant(payload["outcomes_through"])
    if cutoff != reviewed or reviewed > now:
        raise ValueError("Outcome revision cutoff or review time is invalid")
    if direct:
        _validate_direct_source(payload)
    elif (not isinstance(payload["source_evaluation"], dict)
            or set(payload["source_evaluation"]) != {"receipt.json", "manifest.json", "evaluations.parquet"}
            or not all(_digest(x) for x in payload["source_evaluation"].values())):
        raise ValueError("Outcome revision scientific provenance is invalid")
    original_rows = _projection(base_frame)
    if (not isinstance(payload["rows"], list) or len(payload["rows"]) != len(original_rows)
            or not isinstance(payload["updates"], list) or not isinstance(payload["newly_mature_awaiting_ids"],list)
            or not(payload["updates"] or payload["newly_mature_awaiting_ids"])):
        raise ValueError("Outcome revision must preserve the complete original population")
    originals = {row["id"]: row for row in original_rows}
    updates = {}
    for update in payload["updates"]:
        if not isinstance(update, dict) or update.get("id") not in originals or update["id"] in updates:
            raise ValueError("Outcome revision has a missing or duplicate original identity")
        original = originals[update["id"]]
        if original["actuals_status"] == "EVALUATED":
            raise ValueError("Outcome revision cannot replace a previously evaluated original")
        updates[update["id"]] = _validate_update(original, update, cutoff)
    frame = base_frame.copy(deep=True)
    expected_mature = [row["id"]for row in original_rows if row["id"]not in updates
        and row["actuals_status"]=="PENDING_MATURITY"and _instant(row["target_window_end"])<=cutoff]
    if payload["newly_mature_awaiting_ids"]!=expected_mature:
        raise ValueError("Outcome revision missing-input maturity dispositions differ from actual cutoff")
    for index, (original, proposed) in enumerate(zip(original_rows, payload["rows"])):
        expected = updates.get(original["id"],dict(original,actuals_status="MATURE_AWAITING_DATA")
            if original["id"]in expected_mature else original)
        if not isinstance(proposed, dict) or not _row_equal(proposed, expected):
            raise ValueError("Outcome revision changes original forecast or previously saved outcome fields")
        _outcome(proposed)
        if original["id"] in updates:
            for key in _OUTCOMES:
                value = proposed[key]
                if key.endswith("observed_at") and value is not None and isinstance(frame[key].dtype, pd.DatetimeTZDtype):
                    value = pd.Timestamp(value)
                frame.at[frame.index[index], key] = value
        elif original["id"]in expected_mature:
            frame.at[frame.index[index],"actuals_status"]="MATURE_AWAITING_DATA"
    return frame


def export_revision(root, *, base_run, expected_base_receipt_sha256, destination, reviewed_at=None):
    """Write exact sanitized revision bytes once; a retry reuses identical bytes."""
    payload = build_revision(root, base_run=base_run,
        expected_base_receipt_sha256=expected_base_receipt_sha256, reviewed_at=reviewed_at)
    destination = Path(destination).resolve()
    raw = _bytes(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if destination.read_bytes() != raw:
            raise ValueError("An immutable outcome revision already exists with different bytes") from None
    return destination


def _parse(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Outcome revision contains duplicate JSON fields")
            value[key] = item
        return value
    def invalid(value):
        raise ValueError("Outcome revision contains a nonfinite JSON number")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Outcome revision exceeds the bounded scientific package size")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _validate_direct_source(payload):
    """Direct observations name their own provenance, never a peer receipt."""
    proof = payload.get("source_observations")
    fields = {"schema_version", "producer", "source_owner", "source_action_date",
              "source_plan_file_sha256", "source_plan_package_sha256", "source_forecasts_sha256",
              "source_inputs_sha256", "original_ids", "price_source_contract", "price_dataset",
              "updates_sha256", "observation_artifact_sha256", "observed_at"}
    if (payload.get("source_evaluation") is not None or not isinstance(proof, dict)
            or set(proof) != fields or proof.get("schema_version") != "native-stock-original-outcomes-v1"
            or proof.get("producer") not in {"Atlas", "Scout"}
            or proof.get("source_owner") not in {"atlas", "scout"}
            or not isinstance(proof.get("source_action_date"), str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", proof["source_action_date"])):
        raise ValueError("Direct outcome revision scientific provenance is invalid")
    for key in ("source_plan_file_sha256", "source_plan_package_sha256", "source_forecasts_sha256", "updates_sha256", "observation_artifact_sha256"):
        if not _digest(proof[key]):
            raise ValueError("Direct outcome revision lacks an exact source hash")
    hashes, identities = proof["source_inputs_sha256"], proof["original_ids"]
    parent_ids = {row.get("id") for row in payload["rows"] if isinstance(row, dict)}
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 4096
            or any(not _digest(item) for item in hashes) or hashes != sorted(set(hashes))
            or not isinstance(identities, list) or not identities
            or any(not isinstance(item, str) or not item.startswith(proof["source_action_date"]+":")
                   or item not in parent_ids for item in identities)
            or identities != sorted(set(identities))):
        raise ValueError("Direct outcome revision input identities are invalid")
    contract = proof["price_source_contract"]
    if contract not in _DIRECT_CONTRACTS or proof["price_dataset"] != _DIRECT_CONTRACTS[contract][0]:
        raise ValueError("Direct outcome revision price contract differs")
    if proof["updates_sha256"] != hashlib.sha256(_bytes(payload["updates"])).hexdigest():
        raise ValueError("Direct outcome revision observed results changed")
    if _instant(proof["observed_at"]) > _instant(payload["reviewed_at"]):
        raise ValueError("Direct outcome revision observation proof is newer than its review")
    allowed_sources = set(_DIRECT_CONTRACTS[contract][1])
    source_datasets = {"databento:XNAS.ITCH:ohlcv-1m": "XNAS.ITCH",
        "databento:EQUS.MINI:ohlcv-1m": "EQUS.MINI",
        "schwab:pricehistory:1m": "SCHWAB.PRICEHISTORY", "schwab:pricehistory:5m": "SCHWAB.PRICEHISTORY",
        "fmp:historical-chart:5min": "FMP.HISTORICAL_CHART_5MIN"}
    for update in payload["updates"]:
        if (not isinstance(update, dict) or update.get("id") not in identities
                or update.get("target_observation_source") not in allowed_sources
                or update.get("target_observation_dataset") != source_datasets.get(update.get("target_observation_source"))
                or any(update.get(key) not in hashes for key in _EVIDENCE[3:])):
            raise ValueError("Direct outcome revision lacks native endpoint input proof")


def _native_observed_point(point, *, source, inputs, proof_clock, close):
    """Validate a hash-bound admitted native row before deriving its endpoint."""
    import math
    fields = {"timestamp", "interval_seconds", "recorded_at", "input_sha256",
              "open", "high", "low", "close", "volume", "native_flags"}
    if not isinstance(point, dict) or set(point) != fields:
        raise ValueError("Direct observation lacks its complete native point proof")
    flags = point["native_flags"]
    native_markers = {"synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty"}
    if (not isinstance(flags, dict) or not set(flags).issubset(native_markers)
            or any(type(value) is not bool or value for value in flags.values())):
        raise ValueError("Direct observation contains synthetic/no-trade or invalid native markers")
    values = [point[key] for key in ("open", "high", "low", "close", "volume")]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Direct observation native OHLCV is unavailable or invalid")
    if point["high"] < max(values[:4]) or point["low"] > min(values[:4]):
        raise ValueError("Direct observation native OHLC bounds disagree")
    expected_interval = _DIRECT_NATIVE_SOURCES[source][1]
    if type(point["interval_seconds"]) is not int or point["interval_seconds"] != expected_interval:
        raise ValueError("Direct observation native interval differs from its recorded source")
    if point["input_sha256"] not in inputs:
        raise ValueError("Direct observation native input hash is not retained")
    started, recorded = _instant(point["timestamp"]), _instant(point["recorded_at"])
    ended = started + pd.Timedelta(seconds=expected_interval)
    if not started < ended <= recorded <= proof_clock:
        raise ValueError("Direct observation precedes native completion or receipt availability")
    return (point["close"], ended) if close else (point["open"], started)


def export_observed_revision(root, *, base_run, expected_base_receipt_sha256,
                             source_plan, expected_source_plan_sha256, forecast_ids,
                             observation_proof, expected_observation_proof_sha256,
                             producer, destination, reviewed_at=None):
    """Derive Stats from selected originals and independently retained native proof.

    This reads an exact private scientific observation artifact. It validates
    native OHLCV, receipt/event clocks, source contracts and hashes, and computes
    returns and original target scores itself. Native acquisition/parser adapters
    retain raw receipts separately. No peer or cumulative receipt is invented,
    and no canonical forecast history, model, account or trader state is changed.
    """
    from ml.joint_capital_plan import content_sha256
    from ml.gameplan_probability_target import observed_probability_target
    root, source_plan = Path(root).resolve(), Path(source_plan).resolve()
    observation_proof = Path(observation_proof).resolve()
    if not _digest(expected_source_plan_sha256) or file_checksum(source_plan) != expected_source_plan_sha256:
        raise ValueError("Direct outcome revision source plan differs from its exact selected hash")
    if not _digest(expected_observation_proof_sha256) or file_checksum(observation_proof) != expected_observation_proof_sha256:
        raise ValueError("Direct outcome revision observation artifact differs from its exact selected hash")
    package = _parse(source_plan.read_bytes())
    if (not isinstance(package, dict) or package.get("schema_version") != "joint-capital-owner-package-v1"
            or package.get("owner_id") not in {"atlas", "scout"}
            or not isinstance(package.get("forecasts"), list)
            or not _digest(package.get("package_sha256"))
            or content_sha256({key: value for key, value in package.items() if key != "package_sha256"}) != package["package_sha256"]
            or package.get("content_sha256", {}).get("forecasts") != content_sha256(package["forecasts"])
            or not _digest(package.get("source_hashes", {}).get("forecasts_sha256"))):
        raise ValueError("Direct outcome revision original source package binding is invalid")
    proof = _parse(observation_proof.read_bytes())
    if (not isinstance(proof, dict) or set(proof) != {"schema_version", "source_plan_file_sha256",
            "observed_at", "source_inputs_sha256", "observations", "missing_original_ids"}
            or proof.get("schema_version") != "stock-original-native-observations-v1"
            or proof.get("source_plan_file_sha256") != expected_source_plan_sha256
            or not isinstance(proof.get("observations"), list)):
        raise ValueError("Direct outcome revision native observation artifact is invalid")
    now, proof_clock = _instant(utc_timestamp(reviewed_at)), _instant(proof["observed_at"])
    if proof_clock > now:
        raise ValueError("Direct outcome revision observation proof is newer than its review")
    review, base_frame, _, parent = _base(root, base_run, expected_base_receipt_sha256)
    if now < _instant(review.reviewed_at) or now < _instant(review.outcomes_through):
        raise ValueError("Direct outcome revision cannot precede its parent")
    ids = list(forecast_ids)
    if not ids or any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("Direct outcome revision requires unique exact original identities")
    originals = {row["id"]: row for row in _projection(base_frame)}
    selected = [row for row in package["forecasts"] if row.get("id") in ids]
    if (len(selected) != len(ids) or len({row["id"] for row in selected}) != len(ids)
            or any(item not in originals for item in ids)
            or any(originals[item]["actuals_status"] == "EVALUATED" for item in ids)):
        raise ValueError("Direct outcome revision must select existing unscored originals")
    for row in selected:
        for key, value in originals[row["id"]].items():
            if key not in _OUTCOMES and (key not in row or not _row_equal({key: value}, {key: _scalar(row[key])})):
                raise ValueError("Direct outcome revision source changes a frozen original")
        if _instant(row["target_window_end"]) > now:
            raise ValueError("Direct outcome revision original has not matured")
    contracts = {(row.get("target_price_source_contract"), row.get("target_price_dataset")) for row in selected}
    if len(contracts) != 1 or next(iter(contracts))[0] not in _DIRECT_CONTRACTS:
        raise ValueError("Direct outcome revision requires one recorded native price contract")
    contract, dataset = next(iter(contracts))
    if dataset != _DIRECT_CONTRACTS[contract][0]:
        raise ValueError("Direct outcome revision price dataset differs from its original contract")
    inputs = proof["source_inputs_sha256"]
    if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 4096
            or any(not _digest(value) for value in inputs) or inputs != sorted(set(inputs))):
        raise ValueError("Direct outcome revision native input hashes are invalid")
    observed = {}
    for row in proof["observations"]:
        if (not isinstance(row, dict) or set(row) != {"id", "source", "dataset", "entry", "exit"}
                or row.get("id") not in ids or row["id"] in observed
                or row.get("source") not in _DIRECT_CONTRACTS[contract][1]
                or row.get("dataset") != _DIRECT_NATIVE_SOURCES[row["source"]][0]):
            raise ValueError("Direct outcome revision native source identity differs")
        observed[row["id"]] = row
    if proof["missing_original_ids"] != sorted(set(ids) - set(observed)):
        raise ValueError("Direct outcome revision missing native observations are not explicit")
    updates = []
    for row in selected:
        if row["id"] not in observed:
            continue
        native = observed[row["id"]]
        gap = row["target_role"] == "OPENING_GAP_RESEARCH"
        entry, opened = _native_observed_point(native["entry"], source=native["source"], inputs=inputs, proof_clock=proof_clock, close=gap)
        finish, closed = _native_observed_point(native["exit"], source=native["source"], inputs=inputs, proof_clock=proof_clock, close=not gap)
        change = finish / entry - 1
        probability = row["calibrated_probability"]
        target = observed_probability_target(change, row.get("assumed_round_trip_cost", .001), row["probability_target_contract"])
        direction = row["direction"]
        update = dict(id=row["id"], actuals_status="EVALUATED",
            direction_correct=None if direction == "NO_EDGE" else change > 0 if direction == "BULLISH" else change < 0,
            model_observed_target=target, model_brier_score=(probability-target)**2,
            actual_return=change, actual_start_observed_at=opened.isoformat(), actual_end_observed_at=closed.isoformat(),
            evaluated_at=now.isoformat(), target_observation_source=native["source"], target_observation_dataset=native["dataset"],
            target_observation_entry_input_sha256=native["entry"]["input_sha256"],
            target_observation_exit_input_sha256=native["exit"]["input_sha256"])
        _validate_update(originals[row["id"]], update, now)
        updates.append(update)
    by_id = {row["id"]: row for row in updates}
    rows, mature = [], []
    for original in _projection(base_frame):
        if original["id"] in by_id:
            rows.append(_validate_update(original, by_id[original["id"]], now))
        elif original["actuals_status"] == "PENDING_MATURITY" and _instant(original["target_window_end"]) <= now:
            rows.append(dict(original, actuals_status="MATURE_AWAITING_DATA"))
            mature.append(original["id"])
        else:
            rows.append(original)
    if not updates and not mature:
        raise ValueError("No new compatible evaluated original outcomes; reuse the saved publication")
    source = dict(schema_version="native-stock-original-outcomes-v1", producer=producer,
        source_owner=package["owner_id"], source_action_date=package["action_date"],
        source_plan_file_sha256=expected_source_plan_sha256, source_plan_package_sha256=package["package_sha256"],
        source_forecasts_sha256=package["source_hashes"]["forecasts_sha256"],
        source_inputs_sha256=inputs, original_ids=sorted(ids), price_source_contract=contract, price_dataset=dataset,
        observation_artifact_sha256=expected_observation_proof_sha256, observed_at=proof_clock.isoformat(),
        updates_sha256=hashlib.sha256(_bytes(updates)).hexdigest())
    payload = dict(schema_version=DIRECT_REVISION_VERSION, action_date=review.session,
        reviewed_at=now.isoformat(), outcomes_through=now.isoformat(), parent=parent,
        source_evaluation=None, source_observations=source, rows=rows, updates=updates,
        newly_mature_awaiting_ids=mature, orders_placed=0, broker_orders_enabled=False)
    _validated_frame(payload, base_frame, parent, now)
    if (file_checksum(source_plan) != expected_source_plan_sha256
            or file_checksum(observation_proof) != expected_observation_proof_sha256
            or _base(root, base_run, expected_base_receipt_sha256)[3] != parent):
        raise ValueError("Direct outcome revision source changed during derivation")
    raw = _bytes(payload)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if destination.read_bytes() != raw:
            raise ValueError("An immutable outcome revision already exists with different bytes") from None
    return destination


def adopt_revision(root, *, package, expected_sha256, base_run, expected_base_receipt_sha256, reviewed_at=None):
    """Adopt one exact reviewed revision into the existing Stats publication shape.

    The caller supplies the original local parent and exact portable bytes.
    An older corrected date changes only its dated pointer when a newer date is
    already selected. Retries return an existing identical revision unchanged.
    """
    root, package = Path(root).resolve(), Path(package).resolve()
    raw = package.read_bytes()
    if not _digest(expected_sha256) or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Outcome revision package bytes differ from their reviewed hash")
    payload = _parse(raw)
    if not isinstance(payload, dict) or not {"reviewed_at", "outcomes_through"} <= set(payload):
        raise ValueError("Unsupported original-outcome revision metadata")
    now = _instant(utc_timestamp(reviewed_at))
    review, base_frame, base_report, parent = _base(root, base_run, expected_base_receipt_sha256)
    if _instant(payload["reviewed_at"]) < _instant(review.reviewed_at) or _instant(payload["outcomes_through"]) < _instant(review.outcomes_through):
        raise ValueError("Outcome revision precedes its parent")
    frame = _validated_frame(payload, base_frame, parent, now)
    with exclusive_runtime_lock(root / ".ducketz-gameplan-stats-outcome-revision.lock", process_name="Gameplan Stats outcome revision"):
        # Revalidate frozen parent bytes after lock acquisition, before writes.
        _base(root, base_run, expected_base_receipt_sha256)
        existing = None
        for receipt_path in (root / RUNS).glob("*/receipt.json"):
            report_path = receipt_path.parent / "report.json"
            if report_path.is_file():
                report = json.loads(report_path.read_bytes())
                if report.get("outcome_revision_sha256") == expected_sha256:
                    load_gameplan_stats(root, run_directory=receipt_path.parent,
                        expected_receipt_sha256=file_checksum(receipt_path))
                    if (receipt_path.parent / "outcome-revision.json").read_bytes() != raw:
                        raise ValueError("Existing outcome revision bytes differ from the reviewed package")
                    pd.testing.assert_frame_equal(pd.read_parquet(receipt_path.parent / "forecast-results.parquet"), frame, check_exact=True, check_dtype=False)
                    if report.get("outcome_revision_parent") != parent:
                        raise ValueError("Existing outcome revision local parent differs")
                    existing = receipt_path.parent
                    break
        dated = root / "ml/gameplan-actuals-review-by-date" / review.session
        dated_pointer = dated / "run.json"
        latest = root / "ml/gameplan-actuals-review-latest/run.json"
        allowed = {expected_base_receipt_sha256}
        if existing is not None:
            allowed.add(file_checksum(existing / "receipt.json"))
        for selected in (dated_pointer, latest):
            if selected.is_file():
                current = json.loads(selected.read_bytes())["current"]
                if current["action_date"] == review.session and current["receipt_sha256"] not in allowed:
                    raise ValueError("A newer dated Stats publication must be reviewed before adoption")
        report = dict(base_report, review_mode="original-outcome-revision",
            reviewed_at=payload["reviewed_at"], outcomes_through=payload["outcomes_through"],
            outcome_revision_sha256=expected_sha256, outcome_revision_parent=parent,
            outcome_revision_source_evaluation=payload["source_evaluation"],
            newly_evaluated_original_ids=[row["id"] for row in payload["updates"]],
            newly_mature_awaiting_ids=payload["newly_mature_awaiting_ids"],
            orders_placed=0, broker_orders_enabled=False)
        if payload["schema_version"] == DIRECT_REVISION_VERSION:
            report["outcome_revision_source_observations"] = payload["source_observations"]
        report.update(saved_direction_policy_summary(_projection(frame)))
        report["outcome_revision_origin_parent"] = payload["parent"]
        if existing is None:
            run = create_timestamp_directory(root / RUNS, timestamp=utc_timestamp(payload["reviewed_at"]))
            frame.to_parquet(run / "forecast-results.parquet", index=False)
            _write_json(run / "report.json", report)
            (run / "Gameplan-results.md").write_text(_combined_report(frame, report), encoding="utf8")
            (run / "outcome-revision.json").write_bytes(raw)
            write_manifest(run, run_timestamp=utc_timestamp(payload["reviewed_at"]), input_files=[],
                output_files=["forecast-results.parquet", "report.json", "Gameplan-results.md", "outcome-revision.json"],
                configuration={key: report[key] for key in ("schema_version", "action_date", "probability_target_contract", "review_mode", "producers", "coverage_status", "direction_policy_evaluation", "direction_policy_versions", "direction_policy_cutoffs", "unrecorded_direction_policy_forecasts", "outcomes_through", "outcome_revision_sha256", "outcome_revision_parent")})
            verify_manifest(run)
            receipt = dict(schema_version=VERSION, status="COMPLETE", action_date=review.session,
                run_path=run.relative_to(root).as_posix(), manifest_sha256=file_checksum(run / "manifest.json"),
                orders_placed=0, broker_orders_enabled=False)
            _write_json(run / "receipt.json", receipt)
        else:
            run = existing
            receipt = json.loads((run / "receipt.json").read_bytes())
        digest = file_checksum(run / "receipt.json")
        load_gameplan_stats(root, review.session, run_directory=run, expected_receipt_sha256=digest)
        pointer = dict(schema_version=VERSION, current=dict(run_path=receipt["run_path"],
            action_date=review.session, receipt_sha256=digest))
        _write_json(dated_pointer, pointer)
        temp = dated / "Gameplan-results.tmp"
        temp.write_bytes((run / "Gameplan-results.md").read_bytes())
        temp.replace(dated / "Gameplan-results.md")
        current_date = None
        if latest.is_file():
            current_date = json.loads(latest.read_bytes())["current"]["action_date"]
        if current_date is None or current_date <= review.session:
            _write_json(latest, pointer)
        return run
