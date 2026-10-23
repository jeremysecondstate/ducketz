"""Receipt-bound native history additions for an explicitly selected local recipe.

Canonical originals and old historical assumptions are not rewritten. A newly
recovered quarter becomes usable at its actual receipt; its historical indicator
cutoff remains a separately declared reconstruction assumption. Conflicting or
incomplete revisions remain evidence, never an automatic price replacement.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import pandas as pd

from .data import History, IntervalResponse, STEP, equity_sessions, utc, validate_candles
from .prediction import ContractError

CONTRACT = "intraday-covered-native-recovery-v2"
FIELDS = ("open", "high", "low", "close", "volume")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def valid_recovery_coverage(candle, coverage):
    """Check persisted clocks and source identity, without dereferencing files."""
    try:
        opened = utc(candle["opened_at"])
        return bool(
            candle.get("native_recovery_contract") == CONTRACT
            and coverage.get("kind") == CONTRACT
            and utc(candle["feature_cutoff_at"]) == opened + STEP
            and coverage.get("feature_cutoff_policy") == "historical_quarter_close_reconstruction"
            and coverage.get("source_schema") == "ohlcv-1m"
            and coverage.get("source_dataset") == "EQUS.MINI"
            and coverage.get("symbol") == candle["symbol"]
            and coverage.get("status") == "success"
            and utc(coverage["request_started_at"]) >= utc(coverage["end"])
            and utc(coverage["received_at"]) >= utc(coverage["request_started_at"])
            and utc(candle["available_at"]) == utc(coverage["received_at"])
            and utc(coverage["start"]) <= opened
            and utc(coverage["end"]) >= opened + STEP
            and all(_hash(coverage.get(k)) for k in (
                "request_id", "receipt_sha256", "raw_sha256", "normalized_sha256")))
    except (KeyError, TypeError, ValueError, ContractError):
        return False


@lru_cache(maxsize=16)
def _verified_receipt(receipt_name, receipt_sha, raw_sha, normalized_sha):
    import databento
    receipt_path = Path(receipt_name)
    raw_path, normalized_path = receipt_path.with_name("raw.dbn"), receipt_path.with_name("native.parquet")
    if (_sha(receipt_path), _sha(raw_path), _sha(normalized_path)) != (receipt_sha, raw_sha, normalized_sha):
        raise ContractError("Native recovery receipt or immutable payload hash differs")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    request = receipt["request"]
    identity = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if (receipt.get("status") != "success" or receipt.get("request_id") != identity
            or receipt.get("raw_sha256") != raw_sha or receipt.get("normalized_sha256") != normalized_sha
            or request.get("dataset") != "EQUS.MINI" or request.get("schema") != "ohlcv-1m"
            or request.get("stype_in") != "raw_symbol"
            or utc(receipt["started_at"]) < utc(request["end"])
            or utc(receipt["received_at"]) < utc(receipt["started_at"])):
        raise ContractError("Native recovery receipt scope or clocks differ")
    saved = databento.DBNStore.from_file(raw_path)
    if (saved.dataset != request["dataset"] or str(saved.schema) != request["schema"]
            or set(saved.symbols) != set(request["symbols"])
            or utc(saved.start) != utc(request["start"]) or utc(saved.end) != utc(request["end"])):
        raise ContractError("Native recovery DBN identity differs from its request")
    native = saved.to_df(price_type="float", pretty_ts=True, map_symbols=True).reset_index()
    normalized = pd.read_parquet(normalized_path)
    columns = ["symbol", "ts_event", *FIELDS]
    if (len(native) != receipt["rows"] or not set(columns).issubset(normalized)
            or not native[columns].equals(normalized[columns])):
        raise ContractError("Native recovery projection differs from original DBN values")
    if not set(native.symbol).issubset(request["symbols"]):
        raise ContractError("Native recovery response contains unrequested symbols")
    frame = normalized[columns].rename(columns={"ts_event": "opened_at"})
    frame["opened_at"] = pd.to_datetime(frame.opened_at, utc=True)
    if (frame.opened_at.lt(utc(request["start"])).any()
            or (frame.opened_at + pd.Timedelta(minutes=1)).gt(utc(request["end"])).any()
            or frame.duplicated(["symbol", "opened_at"]).any()):
        raise ContractError("Native recovery observations escape or duplicate their request")
    return receipt, frame


def _actual_receipt_overlay(current, *, installed, directory, symbol, pc):
    """Replay only explicitly pinned, bounded immutable live receipts locally.

    This bridge preserves the original actual clocks during preparation without
    scanning the large forecast database or treating file times as receipt times.
    Future live collection retains its own normal durable candle journal.
    """
    from .native_interval import _rows, resolve_native_interval
    entries = installed.get("actual_receipts", [])
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ContractError("Pinned actual minute receipt overlay exceeds its explicit bound")
    if len({entry.get("path") for entry in entries}) != len(entries):
        raise ContractError("Pinned actual minute receipts must be unique")
    grouped, files, exclusions = {}, [], []
    for entry in entries:
        path = (directory.parent / entry["path"]).resolve()
        expected_root = directory.parent / "minute-fetches"
        if not path.is_relative_to(expected_root) or path.name != "response.json":
            raise ContractError("Actual minute receipt escapes the selected PC archive")
        persistence_path = path.with_name("persistence.json")
        if (_sha(path), _sha(persistence_path)) != (entry["response_sha256"], entry["persistence_sha256"]):
            raise ContractError("Actual minute receipt bytes changed")
        response, persisted = json.loads(path.read_bytes()), json.loads(persistence_path.read_bytes())
        request, selected = response["request"], response["selected_range"]
        if symbol not in request["symbols"]:
            continue
        if (response.get("version") != "intraday-native-minute-persistence-v1"
                or persisted.get("version") != response["version"] or persisted.get("status") != "complete"
                or request.get("pc") != pc or request.get("dataset") != "EQUS.MINI"
                or request.get("schema") != "ohlcv-1m" or selected.get("schema") != "ohlcv-1m"
                or persisted.get("pc") != pc or persisted.get("start") != request["start"]
                or persisted.get("end") != request["end"]):
            raise ContractError("Actual minute persistence identity differs")
        op, cl = utc(request["start"]), utc(request["end"])
        if cl - op != STEP or request.get("range_policy") != "single_completed_quarter":
            exclusions.append({"receipt": entry["path"], "reason": "not_an_exact_live_quarter"})
            continue
        if utc(selected["start"]) != op or utc(selected["end"]) != cl:
            raise ContractError("Actual minute response coverage differs from its request")
        records = [item for item in response["symbols"] if item["symbol"] == symbol]
        if len(records) != 1 or persisted["symbols"][symbol]["status"] != "complete":
            raise ContractError("Actual minute response omitted or duplicated selected symbol")
        item = records[0]
        raw = item.get("raw")
        if raw is None:
            if item["bars"]:
                raise ContractError("Actual minute observations lack raw native values")
            rows = []
        else:
            raw_path = (path.parent / raw["file"]).resolve()
            if not raw_path.is_relative_to(path.parent) or _sha(raw_path) != raw["sha256"]:
                raise ContractError("Actual minute raw bytes changed or escaped their receipt")
            frame = pd.read_parquet(raw_path)
            if len(frame) != raw["rows"] or len(frame) != len(item["bars"]):
                raise ContractError("Actual minute raw observations were lost in parsing")
            if len(frame) and ("symbol" not in frame or not frame.symbol.eq(symbol).all()):
                raise ContractError("Actual minute raw symbol differs")
            projected = frame.rename(columns={"ts_event": "opened_at"}).to_dict("records")
            try:
                rows = _rows(projected, opened=op, closed=cl)
            except ContractError as exc:
                exclusions.append({"receipt": entry["path"], "reason": "invalid_native_observation",
                                   "error": str(exc)})
                continue
            bars = sorted(item["bars"], key=lambda value: utc(value["timestamp"]))
            for observed, bar in zip(rows, bars):
                if (utc(observed["opened_at"]) != utc(bar["timestamp"])
                        or bar.get("symbol") != symbol or bar.get("source") != "databento"
                        or bar.get("timeframe") != "1m"
                        or any(observed[field] != float(bar[field]) for field in FIELDS)):
                    raise ContractError("Actual minute stored bars differ from native raw values")
        attempt = IntervalResponse(response["attempt_id"], symbol, "databento:EQUS.MINI:ohlcv-1m",
            "EQUS.MINI", op.isoformat(), cl.isoformat(), request["requested_at"], request["observed_at"],
            "success", tuple(rows), selected["start"], selected["end"]).validate()
        grouped.setdefault(op, []).append(attempt)
        files.append({"path": entry["path"], "sha256": entry["response_sha256"]})
    overlay, unresolved, conflicts, preserved_newer = 0, [], [], []
    for opened, attempts in sorted(grouped.items()):
        sessions = equity_sessions(opened, opened + STEP)
        session = next((s for s in sessions if s.contains(opened)), None)
        if session is None:
            continue
        attempts.sort(key=lambda item: (utc(item.request_started_at), utc(item.request_ended_at)))
        result = resolve_native_interval(attempts, session=session)
        candle = result.get("candle")
        if candle is None:
            unresolved.append({"opened_at": opened.isoformat(), "reason": result.get("reason"),
                               "attempts": len(attempts)})
            continue
        prior = current.get(opened)
        if prior is not None:
            if (prior.get("availability_kind") == "actual"
                    and utc(prior["available_at"]) > utc(candle["available_at"])):
                preserved_newer.append({"opened_at": opened.isoformat(),
                    "available_at": utc(prior["available_at"]).isoformat(),
                    "reason": "newer_actual_snapshot_already_present"})
                continue
            old = json.loads(prior["evidence_json"])["observations"]
            new = json.loads(candle["evidence_json"])["observations"]
            if old != new:
                conflicts.append({"opened_at": opened.isoformat(),
                    "reason": "actual_source_snapshot_selected_canonical_revision_retained_in_original_files"})
        current[opened] = candle
        overlay += 1
    return {"pinned_receipts": len(entries), "symbol_receipts_verified": len(files),
        "actual_candles_restored": overlay, "unresolved": unresolved, "excluded": exclusions,
        "canonical_snapshot_differences": conflicts, "preserved_newer_actual": preserved_newer,
        "receipt_files": files}


def merge_recovered_history(history, *, root, symbol, pc):
    """Read the pinned local manifest and add whole verified source snapshots."""
    from .native_interval import _rows, _candle
    if pc not in {"Atlas", "Scout"}:
        raise ContractError("Recovery history requires explicit local PC")
    directory = Path(root).resolve() / "intraday" / pc.lower() / "minute-recovery"
    manifest_path = directory / "installed.json"
    if not manifest_path.is_file():
        raise ContractError("Selected native recovery manifest is absent")
    manifest_bytes = manifest_path.read_bytes()
    installed = json.loads(manifest_bytes)
    if (installed.get("contract") != CONTRACT or installed.get("pc") != pc
            or symbol not in installed.get("symbols", [])):
        raise ContractError("Installed recovery manifest differs from local recipe scope")
    entries = installed.get("receipts")
    if (not isinstance(entries, list) or len(entries) > 4096
            or len({entry.get("path") for entry in entries}) != len(entries)):
        raise ContractError("Recovery selection requires bounded unique receipt paths")
    current = {utc(row["opened_at"]): row for row in history.frame.to_dict("records")}
    actual_overlay = _actual_receipt_overlay(current, installed=installed, directory=directory,
                                             symbol=symbol, pc=pc)
    sources, conflicts, added, revised, identical = [], [], 0, 0, 0
    receipts = []
    for entry in entries:
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory) or path.name != "receipt.json":
            raise ContractError("Native recovery receipt escapes local recovery storage")
        hashes = [entry[k] for k in ("receipt_sha256", "raw_sha256", "normalized_sha256")]
        if not all(_hash(h) for h in hashes):
            raise ContractError("Native recovery manifest lacks exact content identities")
        # Check even cached payloads: cache reuse must not hide local mutation.
        if [_sha(path), _sha(path.with_name("raw.dbn")), _sha(path.with_name("native.parquet"))] != hashes:
            raise ContractError("Native recovery immutable bytes changed")
        receipt, frame = _verified_receipt(str(path), *hashes)
        if symbol in receipt["request"]["symbols"]:
            receipts.append((receipt, frame.loc[frame.symbol.eq(symbol)].copy(), entry))
    for receipt, frame, entry in sorted(receipts, key=lambda item: item[0]["received_at"]):
        request = receipt["request"]
        sessions = equity_sessions(utc(request["start"]), utc(request["end"]))
        by_day = {}
        for session in sessions:
            for day in pd.date_range(utc(session.opened_at).normalize(), utc(session.closed_at).normalize(), freq="D"):
                by_day.setdefault(day, []).append(session)
        source = {"request_id": receipt["request_id"], "receipt_sha256": entry["receipt_sha256"],
                  "received_at": receipt["received_at"], "rows": len(frame)}
        sources.append(source)
        for opened, group in frame.groupby(frame.opened_at.dt.floor("15min"), sort=True):
            session = next((s for s in by_day.get(opened.normalize(), ()) if s.contains(opened)), None)
            if session is None or utc(request["start"]) > opened or utc(request["end"]) < opened + STEP:
                continue
            rows = _rows(group.to_dict("records"), opened=opened, closed=opened + STEP)
            prior = current.get(opened)
            if prior is not None:
                original = json.loads(prior["evidence_json"])["observations"]
                old, new = ({r["opened_at"]: r for r in values} for values in (original, rows))
                if any(key not in new or new[key] != value for key, value in old.items()):
                    conflicts.append({"opened_at": opened.isoformat(), "request_id": receipt["request_id"],
                                      "reason": "different_or_omitted_existing_minute_retained_original"})
                    continue
                if old == new:
                    identical += 1
                    continue
            coverage = {"kind": CONTRACT, "status": "success", "symbol": symbol,
                "source_dataset": "EQUS.MINI", "source_schema": "ohlcv-1m",
                "start": request["start"], "end": request["end"],
                "request_started_at": receipt["started_at"], "received_at": receipt["received_at"],
                "request_id": receipt["request_id"], "receipt_sha256": entry["receipt_sha256"],
                "raw_sha256": entry["raw_sha256"], "normalized_sha256": entry["normalized_sha256"],
                "feature_cutoff_policy": "historical_quarter_close_reconstruction"}
            current[opened] = _candle(symbol=symbol, source_dataset="EQUS.MINI", opened=opened,
                available=utc(receipt["received_at"]), kind="actual_historical_recovery", session=session,
                rows=rows, coverage=coverage)
            added += prior is None
            revised += prior is not None
    frame = validate_candles(pd.DataFrame(list(current.values())))
    if manifest_path.read_bytes() != manifest_bytes:
        raise ContractError("Installed recovery selection changed during history read")
    manifest = {**history.manifest, "native_recovery_contract": CONTRACT,
        "recovery": {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_path": str(manifest_path), "sources": sources, "new_quarters": added,
            "revised_quarters": revised, "identical_snapshots": identical, "conflicts_retained": conflicts,
            "availability_policy": "actual_receipt_no_pre_receipt_training",
            "feature_policy": "historical_quarter_close_reconstruction_without_backdated_context"},
        "actual_receipt_overlay": actual_overlay,
        "candle_rows": len(frame)}
    frame.attrs["intraday_history"] = manifest
    return History(frame, manifest)
