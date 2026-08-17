from __future__ import annotations

import gc
import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import datafetching.databento_cold_start as cold_start


AS_OF = date(2026, 8, 15)
CME_DATASET = "GLBX.MDP3"
EQUITIES_DATASET = "XNAS.ITCH"
WATCHLIST = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK")
CME_SCOPES = (cold_start.CmeScope("NQ.c.0", "continuous", "test"),)


def _catalog(schemas: tuple[str, ...]) -> dict[str, dict[str, str]]:
    return {
        schema: {"start": "2000-01-01", "end": AS_OF.isoformat()}
        for schema in schemas
    }


def _manifest(tmp_path: Path) -> dict[str, object]:
    return cold_start.build_manifest(
        datastore_root=tmp_path,
        equities_symbols=WATCHLIST,
        cme_dataset=CME_DATASET,
        cme_scopes=CME_SCOPES,
        equities_dataset=EQUITIES_DATASET,
        as_of=AS_OF,
        catalogs={
            cold_start.OPRA_DATASET: _catalog(cold_start.OPRA_SCHEMAS),
            CME_DATASET: _catalog(cold_start.CME_SCHEMAS),
            EQUITIES_DATASET: _catalog(cold_start.US_EQUITIES_SCHEMAS),
        },
    )


def _generic_request(
    tmp_path: Path,
    *,
    request_id: str = "generic-request",
    symbol: str = "NQ.v.0",
) -> dict[str, object]:
    destination = (
        tmp_path
        / "market-data"
        / "databento"
        / "cme"
        / CME_DATASET
        / "trades"
        / symbol.upper()
        / "windows"
        / "2026-07-15_to_2026-08-15"
    )
    return {
        "request_id": request_id,
        "dataset": CME_DATASET,
        "standard_plan_dataset": cold_start.PLAN_DATASET_CME,
        "schema": "trades",
        "symbol_scope": [symbol],
        "stype_in": "continuous",
        "start": "2026-07-15",
        "end": "2026-08-15",
        "storage_path": str(destination),
        "storage_contract": "isolated-cold-start",
        "window": {"unit": "calendar_months", "value": 1},
        "fetch_mode": "initial-baseline",
        "baseline_start": "2026-07-15",
        "previous_completed_through": None,
        "status": "PENDING",
    }


class _ParquetStore:
    def to_parquet(self, path: Path, **_kwargs: object) -> None:
        import pandas as pd

        pd.DataFrame(
            {"ts_event": pd.to_datetime(["2026-08-14T12:00:00Z"])}
        ).to_parquet(path, index=False)


def test_manifest_has_exact_schema_coverage_and_requested_windows(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    requests = manifest["requests"]
    assert isinstance(requests, list)

    by_dataset = {}
    for request in requests:
        by_dataset.setdefault(request["dataset"], set()).add(request["schema"])
    assert by_dataset[cold_start.OPRA_DATASET] == set(cold_start.OPRA_SCHEMAS)
    assert by_dataset[CME_DATASET] == set(cold_start.CME_SCHEMAS)
    assert by_dataset[EQUITIES_DATASET] == set(cold_start.US_EQUITIES_SCHEMAS)

    opra = [item for item in requests if item["dataset"] == cold_start.OPRA_DATASET]
    assert {item["symbol_scope"][0] for item in opra} == {
        f"{symbol}.OPT" for symbol in WATCHLIST
    }
    assert all(item["stype_in"] == "parent" for item in opra)
    assert {item["symbol_scope"][0] for item in requests if item["dataset"] == EQUITIES_DATASET} == set(WATCHLIST)

    expected_common_starts = {
        "ohlcv-1s": "2026-08-05",
        "bbo-1s": "2026-08-12",
        "cbbo-1s": "2026-08-14",
        "ohlcv-1m": "2026-05-07",
        "bbo-1m": "2026-05-07",
        "cbbo-1m": "2026-07-26",
        "ohlcv-1h": "2021-08-16",
        "ohlcv-1d": "2019-08-17",
        "statistics": "2026-07-15",
        "status": "2026-07-15",
        "mbp-10": "2026-08-14",
        "mbo": "2026-08-14",
    }
    for request in requests:
        assert request["end"] == AS_OF.isoformat()
        expected = expected_common_starts.get(request["schema"], "2026-07-15")
        if request["schema"] == "definition":
            expected = "2026-05-07"
        assert request["start"] == expected

    assert {
        (request["standard_plan_dataset"], request["schema"]): request["window"]
        for request in requests
        if request["schema"] in {"ohlcv-1d", "definition"}
    } == {
        (role, "ohlcv-1d"): {"unit": "days", "value": 2_555}
        for role in (
            cold_start.PLAN_DATASET_OPRA,
            cold_start.PLAN_DATASET_CME,
            cold_start.PLAN_DATASET_US_EQUITIES,
        )
    } | {
        (cold_start.PLAN_DATASET_OPRA, "definition"): {"unit": "days", "value": 100},
        (cold_start.PLAN_DATASET_CME, "definition"): {"unit": "days", "value": 100},
        (cold_start.PLAN_DATASET_US_EQUITIES, "definition"): {"unit": "days", "value": 100},
    }
    assert manifest["derived_views"] == []


def test_datastore_paths_are_readable_and_grouped_by_market_scope(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    requests = manifest["requests"]
    cme = next(
        item
        for item in requests
        if item["dataset"] == CME_DATASET
        and item["schema"] == "mbp-10"
        and item["symbol_scope"] == ["NQ.c.0"]
    )
    equity = next(
        item
        for item in requests
        if item["dataset"] == EQUITIES_DATASET
        and item["schema"] == "ohlcv-1s"
        and item["symbol_scope"] == ["AAPL"]
    )
    opra = next(
        item
        for item in requests
        if item["dataset"] == cold_start.OPRA_DATASET
        and item["schema"] == "ohlcv-1s"
        and item["symbol_scope"] == ["AAPL.OPT"]
    )

    assert Path(cme["storage_path"]) == (
        tmp_path
        / "market-data"
        / "databento"
        / "cme"
        / "GLBX.MDP3"
        / "mbp-10"
        / "NQ.C.0"
        / "windows"
        / "2026-08-14_to_2026-08-15"
    )
    assert Path(equity["storage_path"]) == (
        tmp_path
        / "market-data"
        / "databento"
        / "us-equities"
        / "XNAS.ITCH"
        / "ohlcv-1s"
        / "AAPL"
        / "windows"
        / "2026-08-05_to_2026-08-15"
    )
    assert Path(opra["storage_path"]) == (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "ohlcv-1s"
        / "AAPL.OPT"
    )
    assert all(str(item["request_id"]) not in str(item["storage_path"]) for item in requests)


def test_manifest_preflight_and_overlap_cursor_use_readable_stable_names(
    tmp_path: Path,
) -> None:
    first = _manifest(tmp_path)
    manifest_path = cold_start.write_manifest(tmp_path, first)
    preflight_path = cold_start.write_preflight(
        tmp_path,
        {
            "schema_version": cold_start.PREFLIGHT_VERSION,
            "manifest_id": first["manifest_id"],
            "as_of": first["as_of"],
            "capacity_pass": True,
            "estimates": [],
        },
    )
    run = (
        tmp_path
        / "state"
        / "databento"
        / "history"
        / "prediction-focused-baseline"
        / "as-of"
        / "2026-08-15"
    )
    assert manifest_path == run / "manifest.json"
    assert preflight_path == run / "preflight.json"

    request = next(
        item
        for item in first["requests"]
        if item["dataset"] == EQUITIES_DATASET
        and item["schema"] == "ohlcv-1m"
        and item["symbol_scope"] == ["AAPL"]
    )
    cursor_path = cold_start._write_request_cursor(
        tmp_path,
        manifest_id=str(first["manifest_id"]),
        request=request,
        status="PUBLISHED",
    )
    assert cursor_path == (
        tmp_path
        / "state"
        / "databento"
        / "history-cursors"
        / "us-equities"
        / "XNAS.ITCH"
        / "ohlcv-1m"
        / "AAPL"
        / "cursor.json"
    )

    follow_up = cold_start.build_manifest(
        datastore_root=tmp_path,
        equities_symbols=WATCHLIST,
        cme_dataset=CME_DATASET,
        cme_scopes=CME_SCOPES,
        equities_dataset=EQUITIES_DATASET,
        as_of=date(2026, 8, 16),
    )
    overlap = next(
        item
        for item in follow_up["requests"]
        if item["dataset"] == EQUITIES_DATASET
        and item["schema"] == "ohlcv-1m"
        and item["symbol_scope"] == ["AAPL"]
    )
    assert overlap["fetch_mode"] == "overlap-fill"
    assert overlap["previous_completed_through"] == "2026-08-15"
    assert overlap["start"] == "2026-08-13"
    assert Path(overlap["storage_path"]).parent.parent == Path(request["storage_path"]).parent.parent
    assert Path(overlap["storage_path"]).name == "2026-08-13_to_2026-08-16"


def test_interval_schemas_use_the_configured_caps() -> None:
    expected = {
        "ohlcv-1s": 10,
        "bbo-1s": 3,
        "cbbo-1s": 1,
        "ohlcv-1m": 100,
        "bbo-1m": 100,
        "cbbo-1m": 20,
        "ohlcv-1h": 1_825,
        "ohlcv-1d": 2_555,
    }
    for role, schemas in (
        (cold_start.PLAN_DATASET_OPRA, cold_start.OPRA_SCHEMAS),
        (cold_start.PLAN_DATASET_CME, cold_start.CME_SCHEMAS),
        (cold_start.PLAN_DATASET_US_EQUITIES, cold_start.US_EQUITIES_SCHEMAS),
    ):
        for schema in schemas:
            if schema in expected:
                assert cold_start.schema_window(role, schema) == {
                    "unit": "days",
                    "value": expected[schema],
                }


def test_research_only_and_redundant_books_are_not_default_baselines() -> None:
    assert "cmbp-1" not in cold_start.OPRA_SCHEMAS
    assert "mbp-1" not in cold_start.CME_SCHEMAS
    assert "mbp-1" not in cold_start.US_EQUITIES_SCHEMAS


def test_dense_book_schemas_use_one_day_initial_baseline() -> None:
    for role, schema in (
        (cold_start.PLAN_DATASET_OPRA, "cmbp-1"),
        (cold_start.PLAN_DATASET_CME, "mbp-10"),
        (cold_start.PLAN_DATASET_CME, "mbo"),
        (cold_start.PLAN_DATASET_US_EQUITIES, "mbp-10"),
        (cold_start.PLAN_DATASET_US_EQUITIES, "mbo"),
    ):
        assert cold_start.schema_window(role, schema) == {
            "unit": "days",
            "value": 1,
        }

    assert cold_start.schema_window(
        cold_start.PLAN_DATASET_US_EQUITIES,
        "imbalance",
    ) == {"unit": "calendar_months", "value": 1}


def test_watchlist_and_opra_parent_scope_reject_ambiguous_input(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("# scope\nAAPL\n nvda # note\n", encoding="utf-8")
    assert cold_start.parse_watchlist(watchlist) == ("AAPL", "NVDA")
    assert cold_start.opra_parent_symbols(("AAPL", "NVDA")) == ("AAPL.OPT", "NVDA.OPT")

    watchlist.write_text("AAPL\naapl\n", encoding="utf-8")
    with pytest.raises(cold_start.ColdStartError, match="duplicate"):
        cold_start.parse_watchlist(watchlist)
    with pytest.raises(cold_start.ColdStartError, match="Cannot construct"):
        cold_start.opra_parent_symbols(("AAPL.OPT",))


def test_cold_start_equities_dataset_does_not_inherit_live_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABENTO_EQUITIES_DATASET", "EQUS.MINI")
    monkeypatch.delenv("DATABENTO_COLD_START_EQUITIES_DATASET", raising=False)
    assert cold_start.resolve_equities_dataset() == "XNAS.ITCH"

    monkeypatch.setenv("DATABENTO_COLD_START_EQUITIES_DATASET", "XNYS.PILLAR")
    assert cold_start.resolve_equities_dataset() == "XNYS.PILLAR"
    assert cold_start.resolve_equities_dataset("XNAS.ITCH") == "XNAS.ITCH"


def test_default_as_of_uses_latest_date_available_to_every_schema() -> None:
    catalogs = {
        "OPRA.PILLAR": {
            "ohlcv-1s": {"start": "2013-01-01", "end": "2026-08-15"},
            "ohlcv-1d": {"start": "2013-01-01", "end": "2026-08-16"},
        },
        "GLBX.MDP3": {
            "ohlcv-1s": {"start": "2010-01-01", "end": "2026-08-16"},
        },
    }

    assert cold_start.latest_common_available_date(catalogs) == date(2026, 8, 15)


def test_cme_scope_is_required_and_never_invented_from_equities() -> None:
    with pytest.raises(cold_start.ColdStartError, match="requires explicit CME scope"):
        cold_start.resolve_cme_scopes({})

    scopes = cold_start.resolve_cme_scopes(
        {
            "DATABENTO_CME_CONTEXT_SYMBOLS": '["NQ.c.0", "ES.c.0"]',
            "DATABENTO_CME_CONTEXT_STYPE_IN": "continuous",
        }
    )
    assert [(scope.symbol, scope.stype_in) for scope in scopes] == [
        ("ES.c.0", "continuous"),
        ("NQ.c.0", "continuous"),
    ]
    with pytest.raises(cold_start.ColdStartError, match="Ambiguous CME"):
        cold_start.resolve_cme_scopes(
            {
                "DATABENTO_CME_CONTEXT_SYMBOLS": "NQ.c.0",
                "DATABENTO_CME_CONTRACT_SYMBOLS": "NQ.c.0",
            }
        )


def test_manifest_is_deterministic_and_storage_preflight_uses_exact_arithmetic(
    tmp_path: Path,
) -> None:
    first = _manifest(tmp_path)
    second = _manifest(tmp_path)
    assert first == second

    class Metadata:
        def get_billable_size(self, **kwargs: object) -> int:
            return len(str(kwargs["schema"])) * 100

        def get_record_count(self, **_kwargs: object) -> int:
            return 7

    preflight = cold_start.preflight_manifest(
        SimpleNamespace(metadata=Metadata()),
        datastore_root=tmp_path,
        manifest=first,
        disk_usage=lambda _path: SimpleNamespace(free=10**15),
    )
    expected_download_size = sum(
        len(str(request["schema"])) * 100 for request in first["requests"]
    )
    assert (
        preflight["total_estimated_download_size_bytes"]
        == expected_download_size
    )
    assert preflight["total_record_count"] == 7 * len(first["requests"])
    assert preflight["required_free_bytes"] == (
        5 * 1024**3 + 2 * expected_download_size
    )
    assert preflight["capacity_pass"] is True
    assert all(
        item["estimated_download_size_bytes"] > 0
        for item in preflight["estimates"]
    )


def test_metadata_call_retries_only_transient_server_failures() -> None:
    attempts = 0
    delays: list[float] = []

    def transient_then_success(**_kwargs: object) -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("504 The remote gateway timed out")
        return 17

    assert cold_start._metadata_call(
        transient_then_success,
        _retry_sleeper=delays.append,
    ) == 17
    assert attempts == 3
    assert delays == [1.0, 2.0]

    with pytest.raises(cold_start.ColdStartError, match=r"after 1 attempt\(s\)"):
        cold_start._metadata_call(
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("401 Unauthorized")),
            _retry_sleeper=delays.append,
        )


def test_preflight_rejects_manifest_outside_configured_included_scope(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    request = next(
        item
        for item in manifest["requests"]
        if item["dataset"] == EQUITIES_DATASET and item["schema"] == "ohlcv-1d"
    )
    request["start"] = "2012-12-06"

    with pytest.raises(cold_start.ColdStartError, match="configured baseline/overlap scope"):
        cold_start.preflight_manifest(
            SimpleNamespace(metadata=object()),
            datastore_root=tmp_path,
            manifest=manifest,
        )


def test_cold_start_cli_requires_neutral_download_confirmation() -> None:
    with pytest.raises(SystemExit):
        cold_start.main(["--execute"])
    with pytest.raises(SystemExit):
        cold_start.main(["--preflight", "--confirm-download"])
    with pytest.raises(SystemExit):
        cold_start.main(["--execute", "--confirm-billable-download"])
    with pytest.raises(SystemExit):
        cold_start.main(["--preflight", "--refresh-preflight"])


def test_execute_reuses_saved_receipts_without_provider_metadata_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\n", encoding="utf-8")
    scopes = (cold_start.CmeScope("NQ.c.0", "continuous", "test"),)
    manifest = cold_start.build_manifest(
        datastore_root=tmp_path,
        equities_symbols=("AAPL",),
        cme_dataset=CME_DATASET,
        cme_scopes=scopes,
        equities_dataset=EQUITIES_DATASET,
        as_of=AS_OF,
        catalogs={
            cold_start.OPRA_DATASET: _catalog(cold_start.OPRA_SCHEMAS),
            CME_DATASET: _catalog(cold_start.CME_SCHEMAS),
            EQUITIES_DATASET: _catalog(cold_start.US_EQUITIES_SCHEMAS),
        },
    )
    cold_start.write_manifest(tmp_path, manifest)

    class Metadata:
        def get_billable_size(self, **_kwargs: object) -> int:
            return 1

        def get_record_count(self, **_kwargs: object) -> int:
            return 1

    preflight = cold_start.preflight_manifest(
        SimpleNamespace(metadata=Metadata()),
        datastore_root=tmp_path,
        manifest=manifest,
        disk_usage=lambda _path: SimpleNamespace(free=10**15),
    )
    cold_start.write_preflight(tmp_path, preflight)

    monkeypatch.setattr(cold_start, "load_repository_environment", lambda: None)
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    monkeypatch.setattr(
        cold_start,
        "discover_dataset_catalog",
        lambda *_args, **_kwargs: pytest.fail("catalog metadata was repeated"),
    )
    monkeypatch.setattr(
        cold_start,
        "preflight_manifest",
        lambda *_args, **_kwargs: pytest.fail("size/count preflight was repeated"),
    )
    monkeypatch.setattr(
        cold_start,
        "execute_manifest",
        lambda *_args, **_kwargs: {
            "verified": 0,
            "downloaded": len(manifest["requests"]),
            "no_data": 0,
            "failed": 0,
        },
    )

    import databento

    monkeypatch.setattr(databento, "Historical", lambda _api_key: object())
    result = cold_start.main(
        [
            "--datastore",
            str(tmp_path),
            "--watchlist",
            str(watchlist),
            "--equities-dataset",
            EQUITIES_DATASET,
            "--cme-dataset",
            CME_DATASET,
            "--cme-symbol",
            "NQ.c.0",
            "--cme-stype-in",
            "continuous",
            "--as-of",
            AS_OF.isoformat(),
            "--execute",
            "--confirm-download",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "provider estimate calls were skipped" in output
    assert "Cold-start completed" in output


def test_saved_preflight_is_checksum_verified_and_disk_space_is_rechecked(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    cold_start.write_manifest(tmp_path, manifest)

    class Metadata:
        def get_billable_size(self, **_kwargs: object) -> int:
            return 100

        def get_record_count(self, **_kwargs: object) -> int:
            return 2

    cold_start.write_preflight(
        tmp_path,
        cold_start.preflight_manifest(
            SimpleNamespace(metadata=Metadata()),
            datastore_root=tmp_path,
            manifest=manifest,
            disk_usage=lambda _path: SimpleNamespace(free=10**15),
        ),
    )
    loaded, _path = cold_start.load_execution_preflight(
        tmp_path,
        manifest=manifest,
        disk_usage=lambda _path: SimpleNamespace(free=123),
    )
    assert loaded["available_free_bytes"] == 123
    assert loaded["capacity_pass"] is False

    preflight_path = (
        tmp_path
        / "state"
        / "databento"
        / "history"
        / "prediction-focused-baseline"
        / "as-of"
        / AS_OF.isoformat()
        / "preflight.json"
    )
    tampered = json.loads(preflight_path.read_text(encoding="utf-8"))
    tampered["total_record_count"] = int(tampered["total_record_count"]) + 1
    preflight_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(cold_start.ColdStartError, match="checksum verification failed"):
        cold_start.load_execution_preflight(tmp_path, manifest=manifest)


def test_execution_resumes_verified_generic_entries_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = {
        "request_id": "test-request",
        "dataset": EQUITIES_DATASET,
        "standard_plan_dataset": cold_start.PLAN_DATASET_US_EQUITIES,
        "schema": "trades",
        "symbol_scope": ["AAPL"],
        "stype_in": "raw_symbol",
        "start": "2026-07-15",
        "end": "2026-08-15",
        "storage_path": str(tmp_path / "partition"),
        "storage_contract": "isolated-cold-start",
        "window": {"unit": "calendar_months", "value": 1},
        "status": "PENDING",
    }
    manifest = {
        "manifest_id": "resume-manifest",
        "as_of": "2026-08-15",
        "entitlement_authority": cold_start.STANDARD_PLAN_AUTHORITY,
        "requests": [request],
    }
    preflight = {
        "manifest_id": "resume-manifest",
        "capacity_pass": True,
        "estimates": [{"request_id": "test-request", "record_count": 4}],
    }
    completed: set[str] = set()
    downloads: list[str] = []

    monkeypatch.setattr(
        cold_start,
        "_entry_is_verified",
        lambda _root, raw: str(raw["request_id"]) in completed,
    )

    def download(
        _client: object,
        *,
        datastore_root: Path,
        request: dict[str, object],
        reporter: object,
    ) -> None:
        assert datastore_root == tmp_path
        assert reporter is None
        downloads.append(str(request["request_id"]))
        completed.add(str(request["request_id"]))

    monkeypatch.setattr(cold_start, "_download_generic_entry", download)

    first = cold_start.execute_manifest(
        object(), datastore_root=tmp_path, manifest=manifest, preflight=preflight, reporter=None
    )
    second = cold_start.execute_manifest(
        object(), datastore_root=tmp_path, manifest=manifest, preflight=preflight, reporter=None
    )
    assert downloads == ["test-request"]
    assert first == {"verified": 0, "downloaded": 1, "no_data": 0, "failed": 0}
    assert second == {"verified": 1, "downloaded": 0, "no_data": 0, "failed": 0}
    assert (
        tmp_path
        / "state"
        / "databento"
        / "history-cursors"
        / "us-equities"
        / "XNAS.ITCH"
        / "trades"
        / "AAPL"
        / "cursor.json"
    ).is_file()


def test_generic_download_releases_windows_handle_and_recovers_complete_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = (
        tmp_path
        / "market-data"
        / "databento"
        / "cme"
        / "GLBX.MDP3"
        / "trades"
        / "NQ.V.0"
        / "windows"
        / "2026-08-14_to_2026-08-15"
    )
    request = {
        "request_id": "windows-handle-request",
        "dataset": CME_DATASET,
        "standard_plan_dataset": cold_start.PLAN_DATASET_CME,
        "schema": "trades",
        "symbol_scope": ["NQ.v.0"],
        "stype_in": "continuous",
        "start": "2026-08-14",
        "end": "2026-08-15",
        "storage_path": str(destination),
        "storage_contract": "isolated-cold-start",
        "window": {"unit": "calendar_months", "value": 1},
        "fetch_mode": "initial-baseline",
        "baseline_start": "2026-07-15",
        "previous_completed_through": None,
        "status": "PENDING",
    }
    released: list[bool] = []

    class Store:
        def to_parquet(self, path: Path, **_kwargs: object) -> None:
            import pandas as pd

            pd.DataFrame(
                {"ts_event": pd.to_datetime(["2026-08-14T12:00:00Z"])}
            ).to_parquet(path, index=False)

        def __del__(self) -> None:
            released.append(True)

    class TimeSeries:
        def get_range(self, **kwargs: object) -> Store:
            Path(str(kwargs["path"])).write_bytes(b"provider data")
            return Store()

    original_replace = Path.replace

    def blocked_publish(source: Path, target: Path) -> Path:
        if source.name == "attempt-001":
            gc.collect()
            assert released
            raise PermissionError(5, "simulated open Windows handle", str(source))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", blocked_publish)
    with pytest.raises(
        cold_start.ColdStartPublicationError,
        match="simulated open Windows handle",
    ):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
        )

    monkeypatch.setattr(Path, "replace", original_replace)

    class NoNetwork:
        def get_range(self, **_kwargs: object) -> object:
            pytest.fail("complete retained staging was downloaded again")

    cold_start._download_generic_entry(
        SimpleNamespace(timeseries=NoNetwork()),
        datastore_root=tmp_path,
        request=request,
    )
    assert destination.is_dir()
    cold_start._verify_generic_partition(destination, request)
    cold_start._download_generic_entry(
        SimpleNamespace(timeseries=NoNetwork()),
        datastore_root=tmp_path,
        request=request,
    )


def test_incomplete_and_corrupt_generic_staging_are_never_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from databento.common.error import BentoClientError

    request = _generic_request(tmp_path)
    destination, staging_base = cold_start._generic_entry_paths(tmp_path, request)
    incomplete = staging_base / "attempt-001"
    incomplete.mkdir(parents=True)
    (incomplete / "provider.dbn.zst").write_bytes(b"partial")

    class TimeSeries:
        def get_range(self, **kwargs: object) -> _ParquetStore:
            Path(str(kwargs["path"])).write_bytes(b"provider data")
            return _ParquetStore()

    original_replace = Path.replace

    def block_publish(source: Path, target: Path) -> Path:
        if source.name == "attempt-002":
            raise PermissionError(5, "retain complete attempt", str(source))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", block_publish)
    with pytest.raises(cold_start.ColdStartPublicationError):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
        )
    corrupt = staging_base / "attempt-002"
    (corrupt / "provider.dbn.zst").write_bytes(b"corrupt")
    monkeypatch.setattr(Path, "replace", original_replace)

    calls = 0

    class NoNetwork:
        def get_range(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise BentoClientError(http_status=422, message="invalid symbol")

    with pytest.raises(BentoClientError, match="invalid symbol"):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=NoNetwork()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
        )
    assert calls == 1
    assert incomplete.is_dir()
    assert corrupt.is_dir()
    assert not destination.exists()


def test_transient_truncated_stream_retries_in_a_fresh_attempt_then_succeeds(
    tmp_path: Path,
) -> None:
    from databento.common.error import BentoError

    request = _generic_request(tmp_path)
    paths: list[Path] = []

    class TimeSeries:
        def get_range(self, **kwargs: object) -> _ParquetStore:
            path = Path(str(kwargs["path"]))
            paths.append(path)
            path.write_bytes(b"partial" if len(paths) == 1 else b"provider data")
            if len(paths) == 1:
                raise BentoError("Error streaming response: Response ended prematurely")
            return _ParquetStore()

    sleeps: list[float] = []
    cold_start._download_generic_entry(
        SimpleNamespace(timeseries=TimeSeries()),
        datastore_root=tmp_path,
        request=request,
        reporter=None,
        _retry_sleeper=sleeps.append,
    )
    destination = Path(str(request["storage_path"]))
    assert [path.parent.name for path in paths] == ["attempt-001", "attempt-002"]
    assert paths[0].is_file()
    assert sleeps == [1.0]
    cold_start._verify_generic_partition(destination, request)


def test_matching_truncated_streams_submit_and_resume_exact_batch_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from databento.common.error import BentoError

    request = _generic_request(tmp_path)
    partial_paths: list[Path] = []
    batch_payload = b"verified batch provider data"
    batch_filename = "glbx-mdp3.definition.dbn.zst"
    batch_hash = hashlib.sha256(batch_payload).hexdigest()

    class TimeSeries:
        def get_range(self, **kwargs: object) -> object:
            path = Path(str(kwargs["path"]))
            partial_paths.append(path)
            path.write_bytes(b"matching truncated header")
            raise BentoError("Error streaming response: Response ended prematurely")

    class Batch:
        ready = False
        submit_calls = 0

        def submit_job(self, **kwargs: object) -> dict[str, object]:
            self.submit_calls += 1
            assert kwargs["dataset"] == request["dataset"]
            assert kwargs["schema"] == request["schema"]
            assert kwargs["symbols"] == request["symbol_scope"]
            assert kwargs["stype_in"] == request["stype_in"]
            assert kwargs["start"] == request["start"]
            assert kwargs["end"] == request["end"]
            assert kwargs["split_duration"] == "none"
            return {"id": "GLBX-TEST-BATCH"}

        def get_job_details(self, job_id: str) -> dict[str, object]:
            assert job_id == "GLBX-TEST-BATCH"
            if self.ready:
                return {"id": job_id, "state": "done", "progress": 100}
            return {"id": job_id, "state": "processing", "progress": 25}

        def list_files(self, job_id: str) -> list[dict[str, object]]:
            assert job_id == "GLBX-TEST-BATCH"
            return [
                {
                    "filename": "metadata.json",
                    "size": 2,
                    "hash": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
                },
                {
                    "filename": batch_filename,
                    "size": len(batch_payload),
                    "hash": f"sha256:{batch_hash}",
                },
            ]

        def download(
            self,
            *,
            job_id: str,
            output_dir: Path,
            filename_to_download: str,
        ) -> list[Path]:
            assert job_id == "GLBX-TEST-BATCH"
            assert filename_to_download == batch_filename
            path = Path(output_dir) / job_id / filename_to_download
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(batch_payload)
            return [path]

    batch = Batch()
    client = SimpleNamespace(timeseries=TimeSeries(), batch=batch)
    monkeypatch.setattr(
        cold_start,
        "_dbn_store_from_file",
        lambda _path: _ParquetStore(),
    )
    sleeps: list[float] = []

    def interrupt_while_batch_is_processing(delay: float) -> None:
        sleeps.append(delay)
        if delay == cold_start.GENERIC_BATCH_FALLBACK_POLL_SECONDS:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cold_start._download_generic_entry(
            client,
            datastore_root=tmp_path,
            request=request,
            reporter=None,
            _retry_sleeper=interrupt_while_batch_is_processing,
        )

    _destination, staging_base = cold_start._generic_entry_paths(tmp_path, request)
    state_path = cold_start._batch_fallback_state_path(staging_base)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["job_id"] == "GLBX-TEST-BATCH"
    assert batch.submit_calls == 1
    assert len(partial_paths) == cold_start.GENERIC_BATCH_FALLBACK_REPEAT_THRESHOLD
    assert sleeps == [1.0, 2.0, 5.0]

    batch.ready = True
    cold_start._download_generic_entry(
        client,
        datastore_root=tmp_path,
        request=request,
        reporter=None,
        _retry_sleeper=lambda _delay: pytest.fail("completed batch job should not sleep"),
    )

    destination = Path(str(request["storage_path"]))
    assert batch.submit_calls == 1
    assert len(partial_paths) == cold_start.GENERIC_BATCH_FALLBACK_REPEAT_THRESHOLD
    cold_start._verify_generic_partition(destination, request)
    partition_manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert partition_manifest["provider_delivery"] == {
        "mode": "batch",
        "job_id": "GLBX-TEST-BATCH",
        "filename": batch_filename,
        "provider_hash": f"sha256:{batch_hash}",
    }


def test_exhausted_transient_generic_retries_stop_with_all_attempts_retained(
    tmp_path: Path,
) -> None:
    from databento.common.error import BentoError

    request = _generic_request(tmp_path)
    paths: list[Path] = []

    class TimeSeries:
        def get_range(self, **kwargs: object) -> object:
            path = Path(str(kwargs["path"]))
            paths.append(path)
            path.write_bytes(b"partial")
            raise BentoError("Error streaming response: Response ended prematurely")

    sleeps: list[float] = []
    with pytest.raises(
        cold_start.ColdStartInfrastructureError,
        match="exhausted transient retries",
    ):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
            _retry_sleeper=sleeps.append,
        )
    assert [path.parent.name for path in paths] == [
        f"attempt-{attempt:03d}" for attempt in range(1, 12)
    ]
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0, 29.0]
    assert sum(sleeps) == 180.0
    assert all(path.is_file() for path in paths)
    assert not Path(str(request["storage_path"])).exists()


def test_nontransient_provider_error_is_not_retried(tmp_path: Path) -> None:
    from databento.common.error import BentoClientError

    request = _generic_request(tmp_path)
    calls = 0

    class TimeSeries:
        def get_range(self, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise BentoClientError(http_status=401, message="authentication failed")

    sleeps: list[float] = []
    with pytest.raises(BentoClientError, match="authentication failed"):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
            _retry_sleeper=sleeps.append,
        )
    assert calls == 1
    assert sleeps == []


def test_reduced_quality_warning_remains_visible_and_is_recorded(
    tmp_path: Path,
) -> None:
    import warnings

    from databento.common.error import BentoWarning

    request = _generic_request(tmp_path)

    class TimeSeries:
        def get_range(self, **kwargs: object) -> _ParquetStore:
            warnings.warn(
                "The streaming request contained a degraded day",
                BentoWarning,
                stacklevel=2,
            )
            Path(str(kwargs["path"])).write_bytes(b"provider data")
            return _ParquetStore()

    with pytest.warns(BentoWarning, match="degraded day"):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
        )
    manifest = json.loads(
        (Path(str(request["storage_path"])) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["provider_warnings"] == [
        {
            "category": "databento.common.error.BentoWarning",
            "message": "The streaming request contained a degraded day",
        }
    ]


def test_filesystem_publication_failure_stops_before_next_manifest_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _generic_request(tmp_path, request_id="first", symbol="NQ.v.0")
    second = _generic_request(tmp_path, request_id="second", symbol="ES.v.0")
    manifest = {
        "manifest_id": "fail-fast-manifest",
        "as_of": AS_OF.isoformat(),
        "entitlement_authority": cold_start.STANDARD_PLAN_AUTHORITY,
        "requests": [first, second],
    }
    preflight = {
        "manifest_id": manifest["manifest_id"],
        "capacity_pass": True,
        "estimates": [
            {"request_id": "first", "record_count": 1},
            {"request_id": "second", "record_count": 1},
        ],
    }
    calls: list[str] = []
    monkeypatch.setattr(cold_start, "_entry_is_verified", lambda *_args: False)

    def fail_publish(
        _client: object,
        *,
        datastore_root: Path,
        request: dict[str, object],
        reporter: object,
    ) -> None:
        assert datastore_root == tmp_path
        assert reporter is None
        calls.append(str(request["request_id"]))
        raise PermissionError(5, "publication denied")

    monkeypatch.setattr(cold_start, "_download_generic_entry", fail_publish)
    with pytest.raises(
        cold_start.ColdStartInfrastructureError,
        match="stopped after the first failed request",
    ):
        cold_start.execute_manifest(
            object(),
            datastore_root=tmp_path,
            manifest=manifest,
            preflight=preflight,
            reporter=None,
        )
    assert calls == ["first"]
    progress = json.loads(
        cold_start._progress_path(tmp_path, manifest).read_text(encoding="utf-8")
    )
    assert progress["entries"]["first"]["status"] == "FAILED"
    assert "second" not in progress["entries"]


def test_keyboard_interrupt_is_not_recorded_as_an_ordinary_request_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _generic_request(tmp_path, request_id="first", symbol="NQ.v.0")
    second = _generic_request(tmp_path, request_id="second", symbol="ES.v.0")
    manifest = {
        "manifest_id": "interrupt-manifest",
        "as_of": AS_OF.isoformat(),
        "entitlement_authority": cold_start.STANDARD_PLAN_AUTHORITY,
        "requests": [first, second],
    }
    preflight = {
        "manifest_id": manifest["manifest_id"],
        "capacity_pass": True,
        "estimates": [
            {"request_id": "first", "record_count": 1},
            {"request_id": "second", "record_count": 1},
        ],
    }
    calls: list[str] = []
    monkeypatch.setattr(cold_start, "_entry_is_verified", lambda *_args: False)

    def interrupt(
        _client: object,
        *,
        datastore_root: Path,
        request: dict[str, object],
        reporter: object,
    ) -> None:
        assert datastore_root == tmp_path
        assert reporter is None
        calls.append(str(request["request_id"]))
        raise KeyboardInterrupt

    monkeypatch.setattr(cold_start, "_download_generic_entry", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cold_start.execute_manifest(
            object(),
            datastore_root=tmp_path,
            manifest=manifest,
            preflight=preflight,
            reporter=None,
        )
    assert calls == ["first"]
    assert not cold_start._progress_path(tmp_path, manifest).exists()


def test_keyboard_interrupt_releases_store_and_retains_partial_staging(
    tmp_path: Path,
) -> None:
    request = _generic_request(tmp_path)
    released: list[bool] = []

    class InterruptingStore:
        def to_parquet(self, _path: Path, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        def __del__(self) -> None:
            released.append(True)

    class TimeSeries:
        def get_range(self, **kwargs: object) -> InterruptingStore:
            Path(str(kwargs["path"])).write_bytes(b"partial")
            return InterruptingStore()

    with pytest.raises(KeyboardInterrupt):
        cold_start._download_generic_entry(
            SimpleNamespace(timeseries=TimeSeries()),
            datastore_root=tmp_path,
            request=request,
            reporter=None,
        )
    _destination, staging_base = cold_start._generic_entry_paths(tmp_path, request)
    assert released
    assert (staging_base / "attempt-001" / "provider.dbn.zst").is_file()


def test_opra_cursor_handoff_keeps_history_lock_and_normalizes_calendar_month_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = {
        "request_id": "opra-request",
        "dataset": cold_start.OPRA_DATASET,
        "standard_plan_dataset": cold_start.PLAN_DATASET_OPRA,
        "schema": "trades",
        "symbol_scope": ["AAPL.OPT"],
        "stype_in": "parent",
        "start": "2026-07-15",
        "end": "2026-08-15",
        "storage_path": str(tmp_path / "opra"),
        "storage_contract": "canonical-opra",
        "window": {"unit": "calendar_months", "value": 1},
        "status": "PENDING",
    }
    seen: dict[str, object] = {}

    def synchronize(*_args: object, **kwargs: object) -> object:
        seen["storage_preflight_receipt"] = kwargs["storage_preflight_receipt"]
        seen["fail_fast"] = kwargs["fail_fast"]
        seen["batch_download"] = kwargs["batch_download"]
        seen["refresh_health"] = kwargs["refresh_health"]
        return SimpleNamespace(errors={}, completed_rows=9)

    monkeypatch.setattr(cold_start, "synchronize_opra", synchronize)
    monkeypatch.setattr(
        cold_start,
        "publish_opra_symbol_history_cursor",
        lambda *_args, **kwargs: seen.update(kwargs),
    )

    cold_start._execute_opra_entry(
        object(),
        datastore_root=tmp_path,
        request=request,
        manifest_id="manifest",
        reporter=None,
        preflight_estimate={
            "estimated_download_size_bytes": 1_024,
            "record_count": 9,
        },
        available_free_bytes=10 * 1024**3,
    )
    assert seen["symbol"] == "AAPL"
    assert seen["lookback_policy"] == {"unit": "months", "value": 1}
    storage_preflight_receipt = seen["storage_preflight_receipt"]
    assert storage_preflight_receipt["estimates"]["trades"]["record_count"] == 9
    assert storage_preflight_receipt["source"].startswith("checksum-verified")
    assert seen["fail_fast"] is True
    assert seen["batch_download"] is True
    assert seen["refresh_health"] is False
    assert not (tmp_path / ".ducketz-options-writer.lock").exists()
    assert not (tmp_path / ".ducketz-cme-writer.lock").exists()


def test_opra_cursor_handoff_publishes_current_v5_contract(tmp_path: Path) -> None:
    path = cold_start.publish_opra_symbol_history_cursor(
        tmp_path,
        symbol="AAPL",
        schema="trades",
        requested_start="2026-07-15",
        completed_through="2026-08-15",
        lookback_policy={"unit": "months", "value": 1},
        bootstrap_manifest_id="cold-start-manifest",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "options-opra-symbol-history-v5"
    assert payload["provider_symbol"] == "AAPL.OPT"
    assert payload["requested_start"] == "2026-07-15"
    assert payload["completed_through"] == "2026-08-15"
    assert payload["lookback_policy"] == {"unit": "months", "value": 1}
    assert payload["bootstrap_manifest_id"] == "cold-start-manifest"


def test_opra_cursor_handoff_accepts_calendar_year_policy(tmp_path: Path) -> None:
    path = cold_start.publish_opra_symbol_history_cursor(
        tmp_path,
        symbol="AAPL",
        schema="definition",
        requested_start="2013-08-15",
        completed_through="2026-08-15",
        lookback_policy={"unit": "years", "value": 13},
        bootstrap_manifest_id="cold-start-manifest",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lookback_policy"] == {"unit": "years", "value": 13}


def test_active_startup_material_has_no_stale_paid_download_wording() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "docs" / "datafetch-ml" / "current_start_command",
        root / "docs" / "datafetch-ml" / "databento-cold-start.md",
        root / "docs" / "datafetch-ml" / "options-opra-history.md",
        root / "docs" / "datafetch-ml" / "start_all_loops.ps1",
        root / "options" / "README.md",
        *sorted((root / "docs" / "loops-system-analysis").rglob("*.md")),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()
    assert "confirm-billable" not in combined
    assert "billable" not in combined
    assert "nonzero-cost" not in combined


def test_coordinator_preserves_loop_locks_and_publication_authority() -> None:
    source = Path(cold_start.__file__).read_text(encoding="utf-8")
    assert ".ducketz-databento-cold-start.lock" in source
    for forbidden in (
        ".ducketz-cme-writer.lock",
        ".ducketz-orchestration.lock",
        ".ducketz-options-writer.lock",
        "loop-a/bar-readiness",
        "ml/latest/run.json",
        "ml/strategy-latest/run.json",
    ):
        assert forbidden not in source
    assert "publish_opra_symbol_history_cursor" in source
    assert "option_writer_lock_path" not in source
    assert ' / "state" / "sync.lock"' in source
