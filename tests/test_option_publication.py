from __future__ import annotations

import json
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
    OptionSnapshotPublicationError,
    committed_option_snapshots,
    option_snapshot_pointer_path,
    option_snapshot_root,
    publish_option_snapshot,
    read_committed_option_surfaces,
    read_option_snapshot,
)


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
        "latest_completed_bar_clock",
        lambda *_args, **_kwargs: DecisionClock(
            decision_timestamp=pd.Timestamp("2026-08-05T10:15:00Z"),
            bar_timestamp=pd.Timestamp("2026-08-05T10:14:00Z"),
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
        clock=lambda: pd.Timestamp("2026-08-05T10:16:00Z").to_pydatetime(),
        reporter=None,
    )

    assert result.published == 1
    assert captured["regime_available_not_after"] == committed.finished_at


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
