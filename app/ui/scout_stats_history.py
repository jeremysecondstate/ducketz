"""Read privately imported Scout history without replacing local publications."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import re

from app.ui.scout_stats_peer import (_day, _fields, _hash, _ID, _json, _link,
    _read, _require, _SHA, _stamp, validate_payload)

_MANIFEST = {"authorization", "completion_id", "consumer_interfaces", "files", "gameplan_horizon_dates",
    "gameplan_horizon_package_count", "generated_at", "horizon_source_files", "limitations", "machine",
    "previous_october27_delivery", "producer", "recipient", "root_index", "runtime_implications",
    "schema_version", "scope", "scout_15m_dates", "scout_15m_source_files", "validation"}
_INDEX = {"completion_id", "gameplan_horizons_index", "generated_at", "limitations", "machine",
    "previous_october27_delivery", "producer", "recipient", "schema_version", "scout_15m_calendar_gaps",
    "scout_15m_sessions", "timezone"}
_HORIZON_INDEX = {"combined_publications_excluded", "limitations", "package_count", "producer", "recipient",
    "rejected_source_count", "schema_version", "scope", "session_count", "sessions", "source_files", "source_session_before"}
_SESSION = {"coverage_status", "metrics", "outcomes_through", "reviewed_at", "revisions", "selected_file", "selected_sha256", "session"}
_REVISION = {"bytes", "coverage_status", "file", "metrics", "outcomes_through", "probability_target_contract",
    "reviewed_at", "rows", "session", "sha256", "source_kind", "source_receipt_sha256"}
_FIFTEEN = {"execution_status", "execution_summary", "path", "prediction_status", "prediction_summary",
    "report", "session", "sha256", "status", "warnings"}
_FILE = re.compile(r"(?:index\.json|report\.html|gameplan-horizons/index\.json|"
    r"sessions/\d{4}-\d{2}-\d{2}/(?:snapshot\.json|report\.html)|"
    r"gameplan-horizons/packages/\d{4}-\d{2}-\d{2}/[a-f0-9]{16}\.json)\Z")


@dataclass(frozen=True)
class HorizonHistorySnapshot:
    session: str | None = None
    review: object | None = None
    exported_at: str | None = None
    completion_id: str | None = None
    package_sha256: str | None = None
    revision_count: int = 0
    coverage_status: str | None = None
    status: str = "missing"
    message: str = "No saved Scout Gameplan forecast Stats are loaded."


def _path(directory, name):
    _require(isinstance(name, str) and _FILE.fullmatch(name), "unexpected history file path")
    relative = PurePosixPath(name)
    _require(not relative.is_absolute() and all(part not in {".", ".."} for part in relative.parts), "unsafe history path")
    path = directory
    for part in relative.parts:
        path = path / part
        _require(not _link(path) and path.resolve().is_relative_to(directory.resolve()), "unsafe history path")
    return path


def _source_hashes(value):
    _require(isinstance(value, dict) and value, "missing source provenance")
    for name, digest in value.items():
        _require(isinstance(name, str) and name.endswith(".py") and not PurePosixPath(name).is_absolute()
                 and ".." not in PurePosixPath(name).parts and isinstance(digest, str) and _SHA.fullmatch(digest), "invalid source provenance")


def _dates(values):
    _require(isinstance(values, list) and len(values) == len(set(values)) and values == sorted(values), "invalid history date inventory")
    for value in values:
        _day(value)
    return values


def _envelope(directory, expected_manifest_sha256):
    directory = Path(directory)
    _require(directory.is_dir() and not _link(directory), "unsafe history directory")
    raw = _read(directory, "manifest.json", 262144)
    _require(isinstance(expected_manifest_sha256, str) and _SHA.fullmatch(expected_manifest_sha256)
             and _hash(raw) == expected_manifest_sha256, "history manifest hash differs")
    manifest = _json(raw)
    _fields(manifest, _MANIFEST, {"schema_version", "producer", "recipient", "machine", "completion_id",
        "generated_at", "files", "root_index", "scout_15m_dates", "gameplan_horizon_dates",
        "gameplan_horizon_package_count", "scout_15m_source_files", "horizon_source_files"})
    _require(manifest["schema_version"] == "scout-stats-history-delivery-v1" and manifest["producer"] == "Scout"
             and manifest["recipient"] == "Atlas" and manifest["machine"] == "pc-new", "history identity differs")
    _require(isinstance(manifest["completion_id"], str) and _ID.fullmatch(manifest["completion_id"]), "invalid history completion")
    _stamp(manifest["generated_at"])
    _require(manifest["root_index"] == "index.json", "unsupported history index")
    _dates(manifest["scout_15m_dates"])
    _dates(manifest["gameplan_horizon_dates"])
    _source_hashes(manifest["scout_15m_source_files"])
    _source_hashes(manifest["horizon_source_files"])
    _require(isinstance(manifest["files"], dict) and len(manifest["files"]) <= 10000, "invalid history files")
    files = {}
    total = 0
    for name, item in manifest["files"].items():
        _fields(item, {"bytes", "sha256"}, {"bytes", "sha256"})
        _require(type(item["bytes"]) is int and 0 <= item["bytes"] <= 32 * 1024 * 1024
                 and isinstance(item["sha256"], str) and _SHA.fullmatch(item["sha256"]), "invalid history file record")
        total += item["bytes"]
        _require(total <= 256 * 1024 * 1024, "history export exceeds limit")
        path = _path(directory, name)
        raw = _read(path.parent, path.name, 32 * 1024 * 1024)
        _require(len(raw) == item["bytes"] and _hash(raw) == item["sha256"], "history file size or hash differs")
        files[name] = raw
    _require({"index.json", "report.html", "gameplan-horizons/index.json"} <= files.keys(), "history indexes missing")
    index = _json(files["index.json"])
    _fields(index, _INDEX, _INDEX - {"previous_october27_delivery"})
    _require(index["schema_version"] == "scout-stats-history-export-v1" and index["timezone"] == "America/Los_Angeles"
             and index["gameplan_horizons_index"] == "gameplan-horizons/index.json", "history index schema differs")
    for name in ("producer", "recipient", "machine", "completion_id", "generated_at"):
        _require(index[name] == manifest[name], "history index identity differs")
    horizon = _json(files["gameplan-horizons/index.json"])
    _fields(horizon, _HORIZON_INDEX, _HORIZON_INDEX)
    _require(horizon["schema_version"] == "scout-gameplan-horizon-history-index-v1" and horizon["producer"] == "Scout"
             and horizon["recipient"] == "Atlas" and horizon["source_files"] == manifest["horizon_source_files"], "horizon index identity differs")
    _day(horizon["source_session_before"])
    _require(isinstance(index["scout_15m_sessions"], list) and isinstance(horizon["sessions"], list), "invalid history sessions")
    expected = {"index.json", "report.html", "gameplan-horizons/index.json"}
    dates = []
    for item in index["scout_15m_sessions"]:
        _fields(item, _FIFTEEN, _FIFTEEN)
        day = _day(item["session"])
        _require(item["path"] == f"sessions/{day}/snapshot.json" and item["report"] == f"sessions/{day}/report.html", "15-minute session path differs")
        _require(item["path"] in files and item["report"] in files
                 and item["sha256"] == manifest["files"][item["path"]]["sha256"], "15-minute session hash differs")
        expected.update((item["path"], item["report"]))
        dates.append(day)
    _require(dates == manifest["scout_15m_dates"], "15-minute session inventory differs")
    dates, packages = [], []
    for item in horizon["sessions"]:
        _fields(item, _SESSION, _SESSION)
        day = _day(item["session"])
        _require(day < horizon["source_session_before"] and isinstance(item["revisions"], list) and item["revisions"], "invalid horizon revision inventory")
        revision_files = []
        for revision in item["revisions"]:
            _fields(revision, _REVISION, _REVISION)
            _require(revision["session"] == day and isinstance(revision["file"], str)
                     and re.fullmatch(r"packages/" + day + r"/[a-f0-9]{16}\.json", revision["file"]), "revision session path differs")
            name = "gameplan-horizons/" + revision["file"]
            _require(name in files and revision["sha256"] == manifest["files"][name]["sha256"]
                     and revision["bytes"] == manifest["files"][name]["bytes"], "revision file hash differs")
            _stamp(revision["reviewed_at"])
            _stamp(revision["outcomes_through"])
            revision_files.append(revision["file"])
            packages.append(name)
            expected.add(name)
        _require(len(set(revision_files)) == len(revision_files), "duplicate horizon revision")
        chosen = next((r for r in item["revisions"] if r["file"] == item["selected_file"]), None)
        _require(chosen is not None, "selected horizon revision missing")
        for key in ("sha256", "coverage_status", "metrics", "outcomes_through", "reviewed_at"):
            _require(item["selected_sha256"] == chosen[key] if key == "sha256" else item[key] == chosen[key], "selected horizon metadata differs")
        dates.append(day)
    _require(dates == manifest["gameplan_horizon_dates"] and len(set(packages)) == len(packages), "horizon session inventory differs")
    _require(horizon["session_count"] == len(dates) and horizon["package_count"] == len(packages)
             and manifest["gameplan_horizon_package_count"] == len(packages), "horizon package count differs")
    _require(expected == set(files), "unindexed history file")
    return manifest, index, horizon, files


def _fifteen_payload(manifest, item, files):
    day = item["session"]
    payload = validate_payload(_json(files[item["path"]]), expected_identity={
        "session": day, "producer": "Scout", "recipient": "Atlas", "generated_at": manifest["generated_at"],
        "completion_id": manifest["completion_id"] + "-" + day.replace("-", ""),
        "source_files": manifest["scout_15m_source_files"]})
    for name in ("prediction_summary", "execution_summary"):
        _require(item[name] == payload[name], "15-minute index summary differs")
    for name in ("status", "prediction_status", "execution_status", "warnings"):
        _require(item[name] == payload["snapshot"][name], "15-minute index status differs")
    return payload


def _horizon_review(directory, manifest, item, *, all_revisions):
    from app.ui.gameplan_stats_data import GameplanStatsReview, _outcome
    from ml.gameplan_stats_handoff import read_stats_package

    selected = None
    for revision in item["revisions"]:
        if not all_revisions and revision["file"] != item["selected_file"]:
            continue
        path = _path(directory, "gameplan-horizons/" + revision["file"])
        payload, frame = read_stats_package(path, expected_sha256=revision["sha256"])
        _require(payload["producer"] == "Scout" and payload["action_date"] == item["session"], "horizon producer or session differs")
        for name in ("coverage_status", "outcomes_through", "reviewed_at", "source_receipt_sha256", "probability_target_contract"):
            _require(payload[name] == revision[name], "horizon revision provenance differs")
        _require(path.stem == payload["source_receipt_sha256"][:16], "horizon revision filename differs from provenance")
        _require(revision["rows"] == len(frame), "horizon row count differs")
        approved = frame.loc[frame.model_status.eq("PROMOTED")] if not frame.empty else frame
        outcomes = tuple(_outcome(row) for row in approved.to_dict("records"))
        review = GameplanStatsReview(item["session"], _stamp(payload["reviewed_at"]), _stamp(payload["outcomes_through"]),
            path.parent, directory / "report.html", outcomes, len(frame) - len(approved), payload["probability_target_contract"],
            payload["coverage_status"], payload["source_receipt_sha256"])
        metrics = asdict(review.metrics())
        metrics["accuracy"] = review.metrics().accuracy
        _require(metrics == revision["metrics"], "horizon recomputed metrics differ")
        if revision["file"] == item["selected_file"]:
            selected = HorizonHistorySnapshot(item["session"], review, manifest["generated_at"], manifest["completion_id"],
                revision["sha256"], len(item["revisions"]), payload["coverage_status"], "read",
                "No saved independent Scout Gameplan forecast for this session." if not payload["rows"] else
                "Saved Scout Gameplan horizon review; selected revision only.")
    _require(selected is not None, "selected horizon review unavailable")
    return selected


def verify_history(directory, expected_manifest_sha256):
    """Fully verify the immutable delivery and every historical revision."""
    directory = Path(directory)
    manifest, index, horizon, files = _envelope(directory, expected_manifest_sha256)
    return {"manifest": manifest, "index": index, "horizon_index": horizon,
        "scout_15m": {item["session"]: _fifteen_payload(manifest, item, files) for item in index["scout_15m_sessions"]},
        "horizons": {item["session"]: _horizon_review(directory, manifest, item, all_revisions=True) for item in horizon["sessions"]}}


def _archives(datastore_root):
    from app.ui.scout_stats_data import _root
    base = _root(datastore_root)
    root = base / "intraday/scout/display-history/v1"
    if not root.is_dir():
        return
    path = base
    for part in ("intraday", "scout", "display-history", "v1"):
        path = path / part
        _require(not _link(path) and path.resolve().is_relative_to(base), "unsafe history archive root")
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        _require(not _link(directory), "unsafe history archive directory")
        if not (directory / "receipt.json").is_file():
            continue
        receipt = _json(_read(directory, "receipt.json", 65536))
        _require(isinstance(receipt, dict) and receipt.get("completion_id") == directory.name, "history receipt identity differs")
        _stamp(receipt.get("verified_at"))
        bundle = _envelope(directory, receipt.get("manifest_sha256"))
        _require(bundle[0]["completion_id"] == receipt["completion_id"], "history receipt identity differs")
        yield directory, bundle


def history_15m_sessions(datastore_root=None):
    return tuple(sorted({item["session"] for _, (_, index, _, _) in _archives(datastore_root)
                         for item in index["scout_15m_sessions"]}, reverse=True))


def horizon_history_sessions(datastore_root=None):
    return tuple(sorted({item["session"] for _, (_, _, horizon, _) in _archives(datastore_root)
                         for item in horizon["sessions"]}, reverse=True))


def load_history_15m(datastore_root=None, session=None):
    _day(session)
    candidates = []
    for _, (manifest, index, _, files) in _archives(datastore_root):
        for item in index["scout_15m_sessions"]:
            if item["session"] == session:
                candidates.append(_fifteen_payload(manifest, item, files))
    return max(candidates, key=lambda p: (_stamp(p["generated_at"]), p["completion_id"])) if candidates else None


def load_horizon_history(datastore_root=None, session=None):
    _day(session)
    candidates = []
    for directory, (manifest, _, horizon, _) in _archives(datastore_root):
        for item in horizon["sessions"]:
            if item["session"] == session:
                candidates.append(_horizon_review(directory, manifest, item, all_revisions=False))
    return max(candidates, key=lambda p: (p.review.reviewed_at, _stamp(p.exported_at), p.completion_id)) if candidates else None
