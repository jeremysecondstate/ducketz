from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching.symbol_onboarding import (
    OPRA_SCHEMAS, US_EQUITIES_SCHEMAS, _checksum, _request,
    enforce_budget, load_plan, queue_history, reference_requests, screen_secondary_history,
)
from datafetching.symbol_universe import WATCHLIST_ENV, read_symbols
from ml.nightly_gameplan import _configured_symbols
from ml.overnight_runtime import _production_watchlist
from ml.stock_trader.model import ENRICHMENT_FEATURE_NAMES
from ml.stock_trader.training import _training_row


def test_reference_windows_preserve_archives_and_extend_production(tmp_path: Path) -> None:
    base = tmp_path / "market-data/databento"
    for schema in OPRA_SCHEMAS:
        for day in ("2026-05-07", "2026-08-18"):
            receipt = base / "opra/OPRA.PILLAR" / schema / "AAPL.OPT/dates" / day / "segments/full-day/receipt.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}")
    for schema in US_EQUITIES_SCHEMAS:
        directory = base / "us-equities/XNAS.ITCH" / schema / "AAPL/windows/baseline"
        directory.mkdir(parents=True)
        (directory / "receipt.json").write_text("{}")
        (directory / "manifest.json").write_text(json.dumps({"request": {
            "dataset": "XNAS.ITCH", "start": "2026-05-07", "end": "2026-08-15"}}))
    requests = reference_requests(tmp_path, "COST", "AAPL", {"end": "2026-09-04T00:00:00Z"})
    assert len(requests) == 25
    indexed = {(r["dataset"], r["schema"]): r for r in requests}
    assert indexed["OPRA.PILLAR", "definition"]["end"] == "2026-09-04"
    assert indexed["OPRA.PILLAR", "ohlcv-1d"]["end"] == "2026-08-19"
    assert indexed["XNAS.ITCH", "ohlcv-1d"]["end"] == "2026-08-15"
    assert all(r["start"] == "2026-05-07" for r in requests)
    assert all(Path(r["storage_path"]).is_relative_to(tmp_path) for r in requests)


@pytest.mark.parametrize("bytes_limit,cost_limit,free,rows", [
    (9, 1.0, 100 * 1024**3, 1), (10, 0.0, 100 * 1024**3, 1),
    (10, 1.0, 5 * 1024**3, 1), (10, 1.0, 100 * 1024**3, 0),
])
def test_budget_rejects_excess_or_missing_history(bytes_limit, cost_limit, free, rows) -> None:
    with pytest.raises(ValueError):
        enforce_budget([{"estimated_download_size_bytes": 10, "cost_usd": 0.01, "record_count": rows}],
            max_billable_bytes=bytes_limit, max_cost_usd=cost_limit, free_bytes=free)


def test_plan_rejects_tampering_and_noncanonical_paths(tmp_path: Path) -> None:
    payload = {"schema_version": "symbol-onboarding-v1", "datastore_root": str(tmp_path), "symbol": "COST",
               "requests": [_request(tmp_path, "COST", "XNAS.ITCH", "ohlcv-1d", "2026-08-01", "2026-09-01")]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({**payload, "plan_id": _checksum(payload)}))
    assert load_plan(path)["symbol"] == "COST"
    payload["requests"][0]["storage_path"] = str(tmp_path.parent / "elsewhere")
    path.write_text(json.dumps({**payload, "plan_id": _checksum(payload)}))
    with pytest.raises(ValueError, match="canonical"):
        load_plan(path)
    payload["symbol"] = "NVDA"
    path.write_text(json.dumps({**payload, "plan_id": "old-checksum"}))
    with pytest.raises(ValueError, match="checksum"):
        load_plan(path)


def test_candidate_scope_is_local_to_the_child_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(WATCHLIST_ENV, raising=False)
    original = read_symbols()
    candidate = tmp_path / "candidate.txt"
    symbols = tuple(dict.fromkeys((*original, "COST")))
    candidate.write_text("\n".join(symbols))
    environment = dict(os.environ, **{WATCHLIST_ENV: str(candidate)})
    result = subprocess.run([sys.executable, "-c",
        "import json; from ml.universe import PRODUCTION_LOOPS_SYMBOLS; print(json.dumps(PRODUCTION_LOOPS_SYMBOLS))"],
        env=environment, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == list(symbols)
    assert read_symbols() == original
    monkeypatch.setenv(WATCHLIST_ENV, str(candidate))
    assert _production_watchlist(tmp_path) == candidate


@pytest.mark.parametrize("symbols", [("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK"),
    ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")])
def test_gameplan_accepts_saved_six_and_candidate_seven(symbols) -> None:
    frame = pd.DataFrame({"symbol": list(symbols) * 2})
    assert _configured_symbols({"configuration": {"symbols": symbols}}, frame) == symbols
    with pytest.raises(RuntimeError, match="disagree"):
        _configured_symbols({"configuration": {"symbols": symbols}}, frame[frame.symbol != symbols[-1]])


def test_legacy_audit_can_supply_a_later_symbol_indicator_without_losing_other_checks() -> None:
    features = {name: 0.5 for name in ENRICHMENT_FEATURE_NAMES}
    features.pop("symbol_AAPL")
    pair = {"symbol": "AMZN", "model": {"feature_values": features},
            "market_reality": {"status": "EVALUATED", "direction_aligned_net_return": 0.01,
                               "direction_aligned_raw_return": 0.011}}
    row = _training_row(pair)
    assert row["features"][ENRICHMENT_FEATURE_NAMES.index("symbol_AAPL")] == 0.0
    features.pop("calibrated_probability")
    assert _training_row(pair) is None


def test_batch_preparation_reuses_jobs_without_publishing_data(tmp_path: Path) -> None:
    requests = [_request(tmp_path, "COST", "OPRA.PILLAR", schema, "2026-05-01", "2026-07-01")
                for schema in ("definition", "cbbo-1s")]
    estimates = [{"record_count": 10, "estimated_download_size_bytes": 100, "cost_usd": 0.0} for _ in requests]
    payload = {"schema_version": "symbol-onboarding-v1", "datastore_root": str(tmp_path),
               "symbol": "COST", "requests": requests, "estimates": estimates,
               "budget": {"max_billable_bytes": 1000, "max_cost_usd": 0.0}}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({**payload, "plan_id": _checksum(payload)}))
    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return {"id": "OPRA-20260501-AAAAAAAAAA"}

    client = SimpleNamespace(batch=SimpleNamespace(submit_job=submit, get_job_details=lambda **_: None,
        list_files=lambda **_: [], download=lambda **_: None), metadata=SimpleNamespace(
        get_dataset_condition=lambda **_: [{"date": day.date().isoformat(), "condition": "available"}
                                           for day in pd.bdate_range("2026-05-01", "2026-06-30")]))
    first = queue_history(path, client)
    second = queue_history(path, client)
    assert len(submitted) == 1
    assert submitted[0]["schema"] == "definition"
    assert first["jobs"][0]["job_id"] == second["jobs"][0]["job_id"]
    assert second["jobs"][0]["status"] == "REUSED"
    assert not list(tmp_path.rglob("normalized.parquet"))
    assert not list(tmp_path.rglob("symbol-history"))


def test_secondary_price_screen_preserves_evidence_and_valid_primary_bars(tmp_path: Path) -> None:
    source = tmp_path / "stocks/COST/bars/1d/schwab/normalized/COST_daily.parquet"
    raw = source.parent.parent / "raw/provider.parquet"
    primary = tmp_path / "stocks/COST/bars/1d/databento/normalized/COST_daily.parquet"
    for path in (source, raw, primary):
        path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2009-03-09", tz="UTC")],
                          "open": [-0.56], "high": [0.47], "low": [-0.8199], "close": [-0.56]})
    frame.to_parquet(source)
    raw.write_bytes(b"original provider evidence")
    primary.write_bytes(b"primary series unchanged")
    original = source.read_bytes()
    plan = {"datastore_root": str(tmp_path), "symbol": "COST", "plan_id": "test-plan"}
    output = tmp_path / "secondary-quality.json"
    receipt = screen_secondary_history(plan, output)
    assert len(receipt["excluded_series"]) == 1
    quarantined = Path(receipt["excluded_series"][0]["quarantine_path"])
    assert quarantined.read_bytes() == original
    assert not source.exists()
    assert raw.read_bytes() == b"original provider evidence"
    assert primary.read_bytes() == b"primary series unchanged"
    assert screen_secondary_history(plan, output)["excluded_series"] == receipt["excluded_series"]


@pytest.mark.parametrize("incomplete", ["progress.json", "operational-history.json", "secondary-history-quality.json"])
def test_activation_preserves_production_membership_until_every_source_gate_passes(tmp_path: Path, monkeypatch, incomplete) -> None:
    import datafetching.symbol_onboarding as onboarding

    watchlist = tmp_path / "watchlist.txt"
    original = "# Keep the operator's watchlist comments.\nAAPL\nAMZN\n"
    watchlist.write_text(original)
    plan = {"plan_id": "candidate", "symbol": "COST", "datastore_root": str(tmp_path),
            "previous_symbols": ["AAPL", "AMZN"], "candidate_symbols": ["AAPL", "AMZN", "COST"]}
    monkeypatch.setattr(onboarding, "load_plan", lambda _: plan)
    monkeypatch.setattr(onboarding, "validate_candidate", lambda _: {"plan_id": "candidate"})
    monkeypatch.setattr(onboarding, "REPOSITORY_WATCHLIST", watchlist)
    for filename in ("progress.json", "operational-history.json", "secondary-history-quality.json"):
        status = "HISTORY_FETCHED" if filename == "progress.json" else "COMPLETE"
        (tmp_path / filename).write_text(json.dumps({"plan_id": "candidate",
            "status": "IN_PROGRESS" if filename == incomplete else status}))
    with pytest.raises(ValueError, match="not complete"):
        onboarding.activate_plan(tmp_path / "plan.json")
    assert watchlist.read_text() == original
    assert not (tmp_path / "activation.json").exists()

    repaired_status = "HISTORY_FETCHED" if incomplete == "progress.json" else "COMPLETE"
    (tmp_path / incomplete).write_text(json.dumps({"plan_id": "candidate", "status": repaired_status}))
    watchlist.write_text("AAPL\nNVDA\n")
    with pytest.raises(ValueError, match="changed independently"):
        onboarding.activate_plan(tmp_path / "plan.json")
    assert watchlist.read_text() == "AAPL\nNVDA\n"
    watchlist.write_text(original)
    assert onboarding.activate_plan(tmp_path / "plan.json")["status"] == "ACTIVE"
    assert watchlist.read_text() == original + "COST\n"


def test_candidate_stock_model_can_be_trained_without_replacing_production(tmp_path: Path, monkeypatch) -> None:
    from ml.stock_trader import training
    from ml.stock_trader.model import load_current_enrichment_model

    pairs = [{"decision_id": str(i), "symbol": "AAPL",
              "model": {"feature_values": {name: 0.01 * (i + 1) for name in ENRICHMENT_FEATURE_NAMES}},
              "market_reality": {"status": "EVALUATED", "direction_aligned_net_return": 0.01 if i % 2 else -0.01,
                                 "direction_aligned_raw_return": 0.011 if i % 2 else -0.009}}
             for i in range(48)]
    monkeypatch.setattr(training, "load_verified_audit_pairs", lambda _: (pairs, ()))
    monkeypatch.setattr(training, "load_verified_loop_b_bootstrap_pairs", lambda _: ([], ()))
    pointer = tmp_path / "ml/stock-trader-model-latest/run.json"
    pointer.parent.mkdir(parents=True)
    original = b'{"prior_production_pointer":"preserved exactly"}'
    pointer.write_bytes(original)
    staged = training.train_and_publish_enrichment_model(tmp_path, publish_current=False)
    assert (staged / "receipt.json").is_file()
    assert pointer.read_bytes() == original
    manifest = json.loads((staged / "manifest.json").read_text())
    assert manifest["configuration"]["automatic_activation_allowed"] is False
    published = training.train_and_publish_enrichment_model(tmp_path)
    assert published != staged
    assert load_current_enrichment_model(tmp_path).feature_names == ENRICHMENT_FEATURE_NAMES
