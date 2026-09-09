from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import ml.strategy_profit_training as training
from datafetching.databento_opra_history import OpraSyncError, canonical_root


def _replay(
    root: Path,
    *,
    session: str = "2026-09-04",
    segment: str = "live-session",
    start: str = "2026-09-04T12:30:00Z",
    end: str = "2026-09-05T00:00:00Z",
) -> tuple[Path, dict[str, object]]:
    directory = (
        canonical_root(root) / "cbbo-1m" / "AAPL.OPT" / "dates" / session
        / "segments" / segment
    )
    directory.mkdir(parents=True)
    manifest = {
        "provider": "databento-opra", "dataset": "OPRA.PILLAR",
        "schema": "cbbo-1m", "partition_date": session,
        "symbol_scope": "AAPL.OPT", "time_segment": segment,
        "partition_start": start, "partition_end": end,
        "request": {
            "dataset": "OPRA.PILLAR", "schema": "cbbo-1m",
            "symbols": ["AAPL.OPT"], "stype_in": "parent",
            "start": start, "end": end,
        },
        "provider_delivery": {"mode": "live-intraday-replay"},
        "raw": {"path": "provider.dbn"},
        "normalized": {"path": "normalized.parquet"},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "receipt.json").write_text("{}", encoding="utf-8")
    (directory / "provider.dbn").write_bytes(b"native-fixture")
    (directory / "normalized.parquet").write_bytes(b"normalized-fixture")
    return directory, manifest


@pytest.fixture
def verifier(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    # The archive's own tests validate native DBN, receipts, and delivery proof.
    # Isolate the resolver here while asserting it must call that strict reader.
    calls: list[Path] = []

    def verified(directory: Path, *, datastore_root: Path) -> dict[str, object]:
        assert datastore_root in directory.parents
        calls.append(directory)
        return {
            "manifest": json.loads((directory / "manifest.json").read_text()),
            "directory": directory,
        }

    monkeypatch.setattr(training, "verify_partition", verified)
    return calls


def _resolve(root: Path, session: str = "2026-09-04") -> Path:
    return training._opra_partition_path(root, "cbbo-1m", "AAPL", session)


def test_historical_full_day_remains_preferred(tmp_path: Path, verifier: list[Path]) -> None:
    directory, _ = _replay(tmp_path)
    historical = directory.parent / "full-day" / "normalized.parquet"
    historical.parent.mkdir()
    historical.write_bytes(b"historical")

    assert _resolve(tmp_path) == historical
    assert verifier == []


def test_missing_partition_retains_expected_historical_path(tmp_path: Path) -> None:
    path = _resolve(tmp_path)
    assert path.parts[-2:] == ("full-day", "normalized.parquet")
    assert not path.exists()


def test_complete_replay_is_verified_once_until_evidence_changes(
    tmp_path: Path, verifier: list[Path],
) -> None:
    directory, _ = _replay(tmp_path)
    assert _resolve(tmp_path) == directory / "normalized.parquet"
    assert _resolve(tmp_path) == directory / "normalized.parquet"
    assert verifier == [directory]

    (directory / "normalized.parquet").write_bytes(b"changed-normalized-fixture")
    assert _resolve(tmp_path) == directory / "normalized.parquet"
    assert verifier == [directory, directory]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-04T13:30:00.000000001Z", "2026-09-05T00:00:00Z"),
        ("2026-09-04T12:30:00Z", "2026-09-04T20:00:00Z"),
        ("2026-09-04T12:30:00Z", "2026-09-04T19:59:59Z"),
    ],
)
def test_replay_must_cover_open_and_include_close(
    tmp_path: Path, verifier: list[Path], start: str, end: str,
) -> None:
    _replay(tmp_path, start=start, end=end)
    with pytest.raises(RuntimeError, match="does not cover the complete session"):
        _resolve(tmp_path)


def test_exact_open_and_exclusive_end_after_close_are_valid(
    tmp_path: Path, verifier: list[Path],
) -> None:
    directory, _ = _replay(
        tmp_path, start="2026-09-04T13:30:00Z", end="2026-09-04T20:00:00.000000001Z",
    )
    assert _resolve(tmp_path) == directory / "normalized.parquet"


def test_early_close_uses_exchange_calendar(tmp_path: Path, verifier: list[Path]) -> None:
    directory, _ = _replay(
        tmp_path, session="2026-11-27", start="2026-11-27T14:30:00Z",
        end="2026-11-27T18:00:00.000000001Z",
    )
    assert _resolve(tmp_path, "2026-11-27") == directory / "normalized.parquet"


def test_non_session_cannot_supply_a_session_replay(tmp_path: Path, verifier: list[Path]) -> None:
    _replay(
        tmp_path, session="2026-09-05", start="2026-09-05T00:00:00Z",
        end="2026-09-06T00:00:00Z",
    )
    with pytest.raises(RuntimeError, match="coverage is invalid"):
        _resolve(tmp_path, "2026-09-05")


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "provider", "other-provider"),
        (None, "dataset", "OTHER.DATASET"),
        (None, "schema", "ohlcv-1h"),
        (None, "partition_date", "2026-09-03"),
        (None, "symbol_scope", "NVDA.OPT"),
        (None, "time_segment", "full-day"),
        ("provider_delivery", "mode", "timeseries-stream"),
        ("request", "dataset", "OTHER.DATASET"),
        ("request", "schema", "definition"),
        ("request", "symbols", ["NVDA.OPT"]),
        ("request", "stype_in", "instrument_id"),
        ("normalized", "path", "../normalized.parquet"),
        ("raw", "path", "../provider.dbn"),
    ],
)
def test_replay_identity_must_match_the_requested_source(
    tmp_path: Path, verifier: list[Path], section: str | None, key: str, value: object,
) -> None:
    directory, manifest = _replay(tmp_path)
    target = manifest if section is None else manifest[section]
    target[key] = value
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="identity is invalid"):
        _resolve(tmp_path)


def test_request_and_partition_bounds_must_match(tmp_path: Path, verifier: list[Path]) -> None:
    directory, manifest = _replay(tmp_path)
    manifest["request"]["start"] = "2026-09-04T13:00:00Z"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="does not cover the complete session"):
        _resolve(tmp_path)


def test_unverified_replay_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _replay(tmp_path)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise OpraSyncError("OPRA partition raw checksum verification failed")

    monkeypatch.setattr(training, "verify_partition", failed)
    with pytest.raises(OpraSyncError, match="checksum verification failed"):
        _resolve(tmp_path)


def test_partial_publication_is_rejected(tmp_path: Path, verifier: list[Path]) -> None:
    directory, _ = _replay(tmp_path)
    (directory / "receipt.json").unlink()
    with pytest.raises(RuntimeError, match="Incomplete OPRA live-session evidence"):
        _resolve(tmp_path)
    assert verifier == []


def test_multiple_replay_segments_are_ambiguous(tmp_path: Path, verifier: list[Path]) -> None:
    _replay(tmp_path)
    _replay(tmp_path, segment="live-session-second")
    with pytest.raises(RuntimeError, match="Ambiguous OPRA live-session partitions"):
        _resolve(tmp_path)
    assert verifier == []
