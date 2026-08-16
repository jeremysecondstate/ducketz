from __future__ import annotations

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
        "ohlcv-1s": "2026-08-10",
        "bbo-1s": "2026-08-10",
        "cbbo-1s": "2026-08-10",
        "ohlcv-1m": "2026-05-07",
        "bbo-1m": "2026-05-07",
        "cbbo-1m": "2026-05-07",
        "ohlcv-1h": "2021-08-16",
        "ohlcv-1d": "2019-08-17",
        "statistics": "2026-07-15",
        "status": "2026-07-15",
        "mbp-10": "2026-07-15",
    }
    for request in requests:
        assert request["end"] == AS_OF.isoformat()
        expected = expected_common_starts.get(request["schema"], "2026-07-15")
        if request["schema"] == "definition":
            expected = {
                cold_start.OPRA_DATASET: "2013-08-15",
                CME_DATASET: "2012-12-06",
                EQUITIES_DATASET: "2018-08-15",
            }[request["dataset"]]
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
        (cold_start.PLAN_DATASET_OPRA, "definition"): {"unit": "years", "value": 13},
        (cold_start.PLAN_DATASET_CME, "definition"): {"unit": "days", "value": 5_000},
        (cold_start.PLAN_DATASET_US_EQUITIES, "definition"): {"unit": "years", "value": 8},
    }
    assert manifest["derived_views"] == []


def test_every_interval_schema_uses_the_shared_cap() -> None:
    expected = {
        "1s": {"unit": "days", "value": 5},
        "1m": {"unit": "days", "value": 100},
        "1h": {"unit": "days", "value": 1_825},
        "1d": {"unit": "days", "value": 2_555},
    }
    for role, schemas in (
        (cold_start.PLAN_DATASET_OPRA, cold_start.OPRA_SCHEMAS),
        (cold_start.PLAN_DATASET_CME, cold_start.CME_SCHEMAS),
        (cold_start.PLAN_DATASET_US_EQUITIES, cold_start.US_EQUITIES_SCHEMAS),
    ):
        for schema in schemas:
            interval = schema.rsplit("-", maxsplit=1)[-1]
            if interval in expected:
                assert cold_start.schema_window(role, schema) == expected[interval]


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

    with pytest.raises(cold_start.ColdStartError, match="configured bootstrap scope"):
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

    def download(_client: object, *, datastore_root: Path, request: dict[str, object]) -> None:
        assert datastore_root == tmp_path
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
    assert (tmp_path / "state" / "databento-cold-start" / "cursors" / "test-request.json").is_file()


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

    monkeypatch.setattr(
        cold_start,
        "synchronize_opra",
        lambda *_args, **_kwargs: SimpleNamespace(errors={}, completed_rows=9),
    )
    monkeypatch.setattr(
        cold_start,
        "publish_opra_symbol_history_cursor",
        lambda *_args, **kwargs: seen.update(kwargs),
    )

    cold_start._execute_opra_entry(
        object(), datastore_root=tmp_path, request=request, manifest_id="manifest", reporter=None
    )
    assert seen["symbol"] == "AAPL"
    assert seen["lookback_policy"] == {"unit": "months", "value": 1}
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
