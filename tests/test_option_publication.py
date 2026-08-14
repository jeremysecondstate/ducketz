from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

import datafetching.options_runtime as options_runtime
from datafetching.decision_time import DecisionClock
from datafetching.loop_a_cycle import begin_loop_a_cycle, finish_loop_a_cycle
from datafetching.parquet_store import ParquetStore
from ml.datasets.families import OPTION_FRESHNESS
from options import OptionSnapshotOutput
from options.publication import (
    LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION,
    OptionSnapshotPublicationError,
    canonical_option_snapshots,
    committed_option_snapshots,
    option_snapshot_pointer_path,
    option_snapshot_root,
    publish_option_snapshot,
    read_committed_option_surfaces,
    read_option_snapshot,
)
from options.providers import ProviderOptionEvidence


def test_committed_option_reader_ignores_partial_generation_and_honors_cutoff(
    tmp_path: Path,
) -> None:
    first = _publish(
        tmp_path,
        snapshot_for="2026-08-05T10:00:00Z",
        available_at="2026-08-05T10:01:00Z",
        score=1.0,
    )
    partial = option_snapshot_root(tmp_path, symbol="GOOG") / "partial-generation"
    partial.mkdir(parents=True)
    _frames(
        snapshot_for="2026-08-05T10:01:00Z",
        available_at="2026-08-05T10:01:30Z",
        score=999.0,
    )[0].to_parquet(partial / "raw.parquet", index=False)
    second = _publish(
        tmp_path,
        snapshot_for="2026-08-05T10:02:00Z",
        available_at="2026-08-05T10:03:00Z",
        score=2.0,
    )

    eligible = committed_option_snapshots(
        tmp_path,
        symbol="GOOG",
        available_not_after="2026-08-05T10:02:59.999999999Z",
    )
    assert [snapshot.directory for snapshot in eligible] == [first.directory]
    surfaces, sources = read_committed_option_surfaces(
        tmp_path,
        symbols=("GOOG",),
        available_not_after="2026-08-05T10:02:59.999999999Z",
    )
    assert surfaces["score"].tolist() == [1.0]
    assert first.features_path in sources
    assert first.receipt_path in sources
    assert partial not in [snapshot.directory for snapshot in eligible]

    pointer = json.loads(
        option_snapshot_pointer_path(tmp_path, symbol="GOOG").read_text(
            encoding="utf-8"
        )
    )
    assert pointer["run_path"] == second.directory.relative_to(tmp_path).as_posix()


def test_directional_option_freshness_contract_is_unchanged() -> None:
    assert OPTION_FRESHNESS["1h"] == pd.Timedelta(hours=2)
    assert OPTION_FRESHNESS["4h"] == pd.Timedelta(hours=2)
    assert OPTION_FRESHNESS["1d"] == pd.Timedelta(days=1)
    assert OPTION_FRESHNESS["1w"] == pd.Timedelta(days=3)


def test_failed_multifile_write_never_replaces_committed_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _publish(
        tmp_path,
        snapshot_for="2026-08-05T10:00:00Z",
        available_at="2026-08-05T10:01:00Z",
        score=1.0,
    )
    pointer_path = option_snapshot_pointer_path(tmp_path, symbol="GOOG")
    original_pointer = pointer_path.read_bytes()
    original_to_parquet = pd.DataFrame.to_parquet
    writes = 0

    def fail_second_write(self: pd.DataFrame, path: object, *args: object, **kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("deliberate normalized-contract write failure")
        original_to_parquet(self, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_second_write)
    raw, contracts, features = _frames(
        snapshot_for="2026-08-05T10:02:00Z",
        available_at="2026-08-05T10:03:00Z",
        score=2.0,
    )
    with pytest.raises(OSError, match="deliberate"):
        publish_option_snapshot(
            tmp_path,
            symbol="GOOG",
            raw=raw,
            contracts=contracts,
            features=features,
        )

    assert pointer_path.read_bytes() == original_pointer
    assert [snapshot.directory for snapshot in committed_option_snapshots(
        tmp_path, symbol="GOOG"
    )] == [first.directory]
    assert not tuple(option_snapshot_root(tmp_path, symbol="GOOG").glob(".*.tmp-*"))


def test_corrupt_committed_option_file_fails_closed(tmp_path: Path) -> None:
    snapshot = _publish(
        tmp_path,
        snapshot_for="2026-08-05T10:00:00Z",
        available_at="2026-08-05T10:01:00Z",
        score=1.0,
    )
    snapshot.features_path.write_bytes(b"not parquet")

    with pytest.raises(OptionSnapshotPublicationError, match="checksum mismatch"):
        read_option_snapshot(snapshot.directory)


def test_options_runtime_uses_prior_committed_regime_cutoff_during_active_loop_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = begin_loop_a_cycle(
        tmp_path,
        symbols=("GOOG",),
        providers=("databento",),
        now="2026-08-05T10:00:00Z",
    )
    committed = finish_loop_a_cycle(
        tmp_path,
        first,
        failure_count=0,
        now="2026-08-05T10:01:00Z",
    )
    begin_loop_a_cycle(
        tmp_path,
        symbols=("GOOG",),
        providers=("databento",),
        now="2026-08-05T10:15:00Z",
    )
    captured: dict[str, object] = {}

    class Session:
        @staticmethod
        def get_option_chain_snapshot(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"chain": "payload"}

    monkeypatch.setattr(
        options_runtime,
        "completed_bar_clock_for_target",
        lambda *_args, **_kwargs: DecisionClock(
            decision_timestamp=pd.Timestamp("2026-08-05T17:15:00Z"),
            bar_timestamp=pd.Timestamp("2026-08-05T17:14:00Z"),
            provider="databento",
            timeframe="1m",
            source_file=tmp_path / "bars.parquet",
        ),
    )

    def persist(*_args: object, **kwargs: object) -> OptionSnapshotOutput:
        captured.update(kwargs)
        return OptionSnapshotOutput(
            tmp_path / "contracts.parquet",
            tmp_path / "features.parquet",
            tmp_path / "raw.parquet",
            2,
            tmp_path / "receipt.json",
            tmp_path / "snapshot",
        )

    monkeypatch.setattr(options_runtime, "persist_schwab_option_snapshot", persist)
    result = options_runtime.run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG",),
        session=Session(),  # type: ignore[arg-type]
        clock=lambda: pd.Timestamp("2026-08-05T17:16:00Z").to_pydatetime(),
        bar_readiness_mode="exact",
        reporter=None,
    )

    assert result.published == 1
    assert captured["regime_available_not_after"] == committed.finished_at


def test_provider_neutral_paths_precedence_idempotency_and_divergence(
    tmp_path: Path,
) -> None:
    target = "2026-08-05T17:15:00Z"
    schwab = _publish(
        tmp_path,
        snapshot_for=target,
        available_at="2026-08-05T17:16:00Z",
        score=1.0,
    )
    raw, contracts, features = _frames(
        snapshot_for=target,
        available_at="2026-08-05T17:15:30Z",
        score=2.0,
    )
    opra = publish_option_snapshot(
        tmp_path,
        provider="databento-opra",
        dataset="OPRA.PILLAR",
        symbol="GOOG",
        raw=raw,
        contracts=contracts,
        features=features,
    )
    assert opra.provider == "databento-opra"
    assert opra.directory.parent == option_snapshot_root(
        tmp_path, symbol="GOOG", provider="databento-opra"
    )
    assert schwab.directory.parent == option_snapshot_root(
        tmp_path, symbol="GOOG", provider="schwab"
    )

    retry_raw, retry_contracts, retry_features = _frames(
        snapshot_for=target,
        available_at="2026-08-05T17:16:30Z",
        score=2.0,
    )
    retry = publish_option_snapshot(
        tmp_path,
        provider="databento-opra",
        dataset="OPRA.PILLAR",
        symbol="GOOG",
        raw=retry_raw,
        contracts=retry_contracts,
        features=retry_features,
    )
    assert retry.directory == opra.directory
    assert retry.available_at == pd.Timestamp("2026-08-05T17:15:30Z")

    divergent = retry_features.copy()
    divergent["score"] = 3.0
    with pytest.raises(OptionSnapshotPublicationError, match="Divergent duplicate"):
        publish_option_snapshot(
            tmp_path,
            provider="databento-opra",
            dataset="OPRA.PILLAR",
            symbol="GOOG",
            raw=retry_raw,
            contracts=retry_contracts,
            features=divergent,
        )

    surfaces, _sources = read_committed_option_surfaces(
        tmp_path,
        symbols=("GOOG",),
        available_not_after="2026-08-05T17:17:00Z",
    )
    assert surfaces["score"].tolist() == [2.0]
    assert surfaces["provider"].eq("databento-opra").all()
    assert surfaces["fallback_used"].eq(False).all()


def test_legacy_schwab_v1_snapshot_remains_readable(tmp_path: Path) -> None:
    snapshot = _publish(
        tmp_path,
        snapshot_for="2026-08-05T17:15:00Z",
        available_at="2026-08-05T17:16:00Z",
        score=1.0,
    )
    manifest_path = snapshot.directory / "manifest.json"
    receipt_path = snapshot.directory / "receipt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt["schema_version"] = LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION
    from ml.artifacts import file_checksum

    receipt["manifest_checksum_sha256"] = file_checksum(manifest_path)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    legacy = read_option_snapshot(snapshot.directory)
    assert legacy.provider == "schwab"
    assert legacy.schema_version == LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION


def test_legacy_v1_later_capture_divergence_is_diagnostic_only(
    tmp_path: Path,
) -> None:
    target = "2026-08-05T17:15:00Z"
    first = _publish(
        tmp_path,
        snapshot_for=target,
        available_at="2026-08-05T17:16:00Z",
        score=1.0,
    )
    _rewrite_snapshot_capture(
        first.directory,
        available_at="2026-08-05T17:16:00Z",
        score=1.0,
        schema_version=LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION,
    )
    first = read_option_snapshot(first.directory)
    later_directory = first.directory.with_name(f"{first.directory.name}-later-v1")
    shutil.copytree(first.directory, later_directory)
    _rewrite_snapshot_capture(
        later_directory,
        available_at="2026-08-05T17:31:00Z",
        score=2.0,
        schema_version=LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION,
    )

    selected, report = canonical_option_snapshots(tmp_path, symbol="GOOG")

    assert [snapshot.directory for snapshot in selected] == [first.directory]
    assert report["duplicate_publication_count"] == 1
    assert report["legacy_divergent_publication_count"] == 1
    surfaces, _sources = read_committed_option_surfaces(
        tmp_path,
        symbols=("GOOG",),
        available_not_after="2026-08-05T17:32:00Z",
    )
    assert surfaces["score"].tolist() == [1.0]

    raw, contracts, features = _frames(
        snapshot_for=target,
        available_at="2026-08-05T17:46:00Z",
        score=1.0,
    )
    retry = publish_option_snapshot(
        tmp_path,
        symbol="GOOG",
        raw=raw,
        contracts=contracts,
        features=features,
    )
    assert retry.directory == first.directory


def test_v2_committed_divergence_still_fails_closed(tmp_path: Path) -> None:
    first = _publish(
        tmp_path,
        snapshot_for="2026-08-05T17:15:00Z",
        available_at="2026-08-05T17:16:00Z",
        score=1.0,
    )
    duplicate_directory = first.directory.with_name(
        f"{first.directory.name}-divergent-v2"
    )
    shutil.copytree(first.directory, duplicate_directory)
    _rewrite_snapshot_capture(
        duplicate_directory,
        available_at="2026-08-05T17:31:00Z",
        score=2.0,
        schema_version=first.schema_version,
    )

    with pytest.raises(
        OptionSnapshotPublicationError,
        match="Conflicting duplicate option evidence",
    ):
        canonical_option_snapshots(tmp_path, symbol="GOOG")


def test_injected_opra_adapter_is_primary_and_same_target_skips_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-05T17:15:00Z")
    monkeypatch.setattr(
        options_runtime,
        "completed_bar_clock_for_target",
        lambda *_args, **_kwargs: DecisionClock(
            decision_timestamp=target,
            bar_timestamp=target - pd.Timedelta(minutes=1),
            provider="databento",
            timeframe="1m",
            source_file=tmp_path / "bars.parquet",
        ),
    )

    class Adapter:
        provider = "databento-opra"
        dataset = "OPRA.PILLAR"
        schema = "cbbo-1s"
        calls = 0

        @classmethod
        def fetch_snapshot(cls, **_kwargs: object) -> ProviderOptionEvidence:
            cls.calls += 1
            contract = "GOOG  260821C00100000"
            return ProviderOptionEvidence(
                provider=cls.provider,
                dataset=cls.dataset,
                schema=cls.schema,
                symbol="GOOG",
                target_snapshot_for=target,
                received_at=target + pd.Timedelta(minutes=1),
                quotes=pd.DataFrame(
                    [
                        {
                            "contract_symbol": contract,
                            "quote_timestamp": target - pd.Timedelta(seconds=1),
                            "bid": 1.0,
                            "ask": 1.1,
                            "bid_size": 10,
                            "ask_size": 12,
                            "publisher_id": 30,
                        }
                    ]
                ),
                definitions=pd.DataFrame(
                    [
                        {
                            "contract_symbol": contract,
                            "symbol": "GOOG",
                            "expiration_date": "2026-08-21T00:00:00Z",
                            "call_put": "CALL",
                            "strike": 100.0,
                            "multiplier": 100.0,
                            "standard_contract": True,
                            "definition_effective_at": target - pd.Timedelta(days=1),
                            "exercise_style": "AMERICAN",
                            "settlement_type": "PHYSICAL",
                        }
                    ]
                ),
            )

    runtime_clock = lambda: (target + pd.Timedelta(minutes=1)).to_pydatetime()
    first = options_runtime.run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG",),
        clock=runtime_clock,
        bar_readiness_mode="exact",
        pricing_barrier_timeout_seconds=0,
        canonical_market_adapter=Adapter(),  # type: ignore[arg-type]
        reporter=None,
    )
    second = options_runtime.run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG",),
        clock=runtime_clock,
        bar_readiness_mode="exact",
        pricing_barrier_timeout_seconds=0,
        canonical_market_adapter=Adapter(),  # type: ignore[arg-type]
        reporter=None,
    )
    assert first.opra_requests == 1
    assert first.schwab_requests == 0
    assert first.schwab_fallbacks == 0
    assert second.opra_requests == 0
    assert Adapter.calls == 1


def _publish(
    root: Path,
    *,
    snapshot_for: str,
    available_at: str,
    score: float,
):
    raw, contracts, features = _frames(
        snapshot_for=snapshot_for,
        available_at=available_at,
        score=score,
    )
    return publish_option_snapshot(
        root,
        symbol="GOOG",
        raw=raw,
        contracts=contracts,
        features=features,
    )


def _frames(
    *,
    snapshot_for: str,
    available_at: str,
    score: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    key = {
        "symbol": "GOOG",
        "snapshot_for": pd.Timestamp(snapshot_for),
        "available_at": pd.Timestamp(available_at),
    }
    raw = pd.DataFrame([{**key, "payload_json": "{}"}])
    contracts = pd.DataFrame(
        [
            {**key, "contract_symbol": "GOOG  260821C00100000", "bid": 1.0, "ask": 1.1},
            {**key, "contract_symbol": "GOOG  260821P00100000", "bid": 0.9, "ask": 1.0},
        ]
    )
    features = pd.DataFrame([{**key, "score": score}])
    return raw, contracts, features


def _rewrite_snapshot_capture(
    directory: Path,
    *,
    available_at: str,
    score: float,
    schema_version: str,
) -> None:
    from ml.artifacts import file_checksum

    available = pd.Timestamp(available_at)
    for name in ("raw.parquet", "contracts.parquet", "option-quality.parquet"):
        path = directory / name
        frame = pd.read_parquet(path)
        for column in ("available_at", "first_available_at"):
            if column in frame.columns:
                frame[column] = available
        if name == "option-quality.parquet":
            frame["score"] = score
        frame.to_parquet(path, index=False)
    outputs = {
        name: {
            "rows": len(pd.read_parquet(directory / name)),
            "size": (directory / name).stat().st_size,
            "checksum_sha256": file_checksum(directory / name),
        }
        for name in ("raw.parquet", "contracts.parquet", "option-quality.parquet")
    }
    manifest_path = directory / "manifest.json"
    receipt_path = directory / "receipt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for payload in (manifest, receipt):
        payload["schema_version"] = schema_version
        payload["available_at"] = available.isoformat()
        payload["outputs"] = outputs
        if schema_version == LEGACY_OPTION_SNAPSHOT_PUBLICATION_VERSION:
            for key in (
                "provider",
                "dataset",
                "normalized_schema_version",
                "target_snapshot_for",
                "first_available_at",
                "receipt_published_at",
            ):
                payload.pop(key, None)
        else:
            payload["first_available_at"] = available.isoformat()
            payload["receipt_published_at"] = available.isoformat()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt["manifest_checksum_sha256"] = file_checksum(manifest_path)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
