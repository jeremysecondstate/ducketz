from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.option_pricing.publication import (
    OPTION_PRICING_PUBLICATION_VERSION,
    OptionPricingPublicationError,
    authoritative_option_pricing_runs,
    pricing_pointer_path,
    publish_option_pricing_run,
    read_current_option_pricing_publication,
    read_option_pricing_publication_at,
)
from ml.option_pricing_runtime import resolve_pricing_symbols, run_option_pricing_once
from ml.parquet_contracts import (
    LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    write_parquet_with_schema,
)


_OUTPUTS = {
    "pricing-samples.parquet": OPTION_PRICING_SAMPLE_SCHEMA,
    "pricing-predictions.parquet": OPTION_PRICING_PREDICTION_SCHEMA,
    "pricing-evaluations.parquet": OPTION_PRICING_EVALUATION_SCHEMA,
    "pricing-surfaces.parquet": OPTION_PRICING_SURFACE_SCHEMA,
    "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
}


def _prepared_run(root: Path, name: str, timestamp: str) -> Path:
    return _prepared_versioned_run(
        root,
        name,
        timestamp,
        publication_version=OPTION_PRICING_PUBLICATION_VERSION,
        surface_schema=OPTION_PRICING_SURFACE_SCHEMA,
    )


def _prepared_versioned_run(
    root: Path,
    name: str,
    timestamp: str,
    *,
    publication_version: str,
    surface_schema: pa.Schema,
) -> Path:
    run = root / "ml" / "option-pricing-runs" / name
    run.mkdir(parents=True)
    outputs = {
        **_OUTPUTS,
        "pricing-surfaces.parquet": surface_schema,
    }
    for output_name, schema in outputs.items():
        write_parquet_with_schema(empty_frame(schema), run / output_name, schema)
    report_name = "option-pricing-model-reports.json"
    (run / report_name).write_text(
        json.dumps({"automated_action_allowed": False}) + "\n",
        encoding="utf-8",
    )
    write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=(),
        output_files=(*outputs, report_name),
        configuration={
            "publication_contract": {
                "version": publication_version,
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            }
        },
        datastore_root=root,
    )
    return run


def _install_legacy_current(root: Path) -> Path:
    timestamp = "2026-07-06T14:01:00+00:00"
    published_at = "2026-07-06T14:01:01+00:00"
    run = _prepared_versioned_run(
        root,
        "20260706T140100.000000Z",
        timestamp,
        publication_version="option-pricing-publication-v1",
        surface_schema=LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    )
    receipt = {
        "schema_version": "option-pricing-publication-v1",
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": timestamp,
        "published_at": published_at,
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "previous_publication": None,
    }
    (run / "publication.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pointer = pricing_pointer_path(root)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "schema_version": "option-pricing-pointer-v1",
                "current": {
                    "run_path": receipt["run_path"],
                    "run_timestamp": timestamp,
                    "published_at": published_at,
                    "manifest_checksum_sha256": receipt[
                        "manifest_checksum_sha256"
                    ],
                    "receipt_checksum_sha256": file_checksum(
                        run / "publication.json"
                    ),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run


def test_pricing_publication_is_atomic_and_receipt_chained(tmp_path: Path) -> None:
    first = _prepared_run(tmp_path, "20260706T140100.000000Z", "2026-07-06T14:01:00Z")
    published_first = publish_option_pricing_run(
        tmp_path,
        run_directory=first,
        published_at="2026-07-06T14:01:01Z",
    )
    second = _prepared_run(tmp_path, "20260706T141600.000000Z", "2026-07-06T14:16:00Z")
    # A complete manifest without a receipt/pointer is an invisible orphan.
    assert read_current_option_pricing_publication(tmp_path).run_directory == first
    assert not (second / "publication.json").exists()

    published_second = publish_option_pricing_run(
        tmp_path,
        run_directory=second,
        published_at="2026-07-06T14:16:01Z",
    )
    assert published_second.run_directory == second
    assert published_second.receipt["previous_publication"] == published_first.pointer["current"]
    reachable = authoritative_option_pricing_runs(tmp_path)
    assert set(reachable) == {first.resolve(), second.resolve()}


def test_pricing_runtime_advances_a_legacy_v1_current_publication(
    tmp_path: Path,
) -> None:
    legacy = _install_legacy_current(tmp_path)
    assert read_current_option_pricing_publication(tmp_path).run_directory == legacy

    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA",),
        run_timestamp="2026-07-06T14:16:00Z",
        runtime_clock=lambda: "2026-07-06T14:16:01Z",
    )

    current = read_current_option_pricing_publication(tmp_path)
    assert current.run_directory == result.run_directory
    assert current.receipt["schema_version"] == OPTION_PRICING_PUBLICATION_VERSION
    assert current.pointer["schema_version"] == "option-pricing-pointer-v2"
    assert current.receipt["previous_publication"]["run_path"] == legacy.relative_to(
        tmp_path
    ).as_posix()


def test_pricing_publication_recovers_receipt_after_interrupted_pointer_write(
    tmp_path: Path,
) -> None:
    first = _prepared_run(tmp_path, "20260706T140100.000000Z", "2026-07-06T14:01:00Z")
    current = publish_option_pricing_run(
        tmp_path,
        run_directory=first,
        published_at="2026-07-06T14:01:01Z",
    )
    second = _prepared_run(tmp_path, "20260706T141600.000000Z", "2026-07-06T14:16:00Z")
    orphan_receipt = {
        "schema_version": OPTION_PRICING_PUBLICATION_VERSION,
        "run_path": second.relative_to(tmp_path).as_posix(),
        "run_timestamp": "2026-07-06T14:16:00+00:00",
        "published_at": "2026-07-06T14:16:01+00:00",
        "manifest_checksum_sha256": file_checksum(second / "manifest.json"),
        "previous_publication": current.pointer["current"],
    }
    (second / "publication.json").write_text(
        json.dumps(orphan_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    recovered = publish_option_pricing_run(
        tmp_path,
        run_directory=second,
        # A restart has a new wall clock, but preserves the first receipt time.
        published_at="2026-07-06T14:17:00Z",
    )
    assert recovered.run_directory == second
    assert recovered.receipt["published_at"] == "2026-07-06T14:16:01+00:00"


def test_pricing_publication_detects_output_tampering(tmp_path: Path) -> None:
    run = _prepared_run(tmp_path, "20260706T140100.000000Z", "2026-07-06T14:01:00Z")
    publish_option_pricing_run(
        tmp_path,
        run_directory=run,
        published_at="2026-07-06T14:01:01Z",
    )
    with (run / "pricing-predictions.parquet").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(OptionPricingPublicationError, match="manifest is invalid"):
        read_current_option_pricing_publication(tmp_path)


def test_causal_publication_selection_never_hides_newer_corruption(
    tmp_path: Path,
) -> None:
    first = _prepared_run(
        tmp_path,
        "20260706T140100.000000Z",
        "2026-07-06T14:01:00Z",
    )
    publish_option_pricing_run(
        tmp_path,
        run_directory=first,
        published_at="2026-07-06T14:01:01Z",
    )
    second = _prepared_run(
        tmp_path,
        "20260706T141600.000000Z",
        "2026-07-06T14:16:00Z",
    )
    publish_option_pricing_run(
        tmp_path,
        run_directory=second,
        published_at="2026-07-06T14:16:01Z",
    )
    with (second / "pricing-surfaces.parquet").open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(OptionPricingPublicationError, match="manifest is invalid"):
        read_option_pricing_publication_at(
            tmp_path,
            available_not_after="2026-07-06T14:05:00Z",
        )


def test_pricing_publication_rejects_path_escape(tmp_path: Path) -> None:
    pointer = pricing_pointer_path(tmp_path)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "schema_version": "option-pricing-pointer-v1",
                "current": {
                    "run_path": "../outside",
                    "run_timestamp": "2026-07-06T14:01:00Z",
                    "published_at": "2026-07-06T14:01:01Z",
                    "manifest_checksum_sha256": "x",
                    "receipt_checksum_sha256": "y",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OptionPricingPublicationError, match="escapes"):
        read_current_option_pricing_publication(tmp_path)


def test_empty_runtime_is_route_isolated_and_writes_only_pricing_authority(
    tmp_path: Path,
) -> None:
    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "AAPL"),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    assert result.run_directory.is_dir()
    assert set(result.route_errors) == {"NVDA/live", "GOOG/live", "AAPL/live"}
    assert read_current_option_pricing_publication(tmp_path).run_directory == result.run_directory
    assert pricing_pointer_path(tmp_path).is_file()
    assert not (tmp_path / "ml" / "latest" / "run.json").exists()
    assert not (tmp_path / "ml" / "strategy-latest" / "run.json").exists()
    report = json.loads(
        (result.run_directory / "option-pricing-model-reports.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["gate"]["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"
    assert report["automated_action_allowed"] is False
    assert report["cycle"]["status"] == "TARGET_INPUT_UNAVAILABLE"
    assert report["black_scholes_baseline"] == {
        "new_predictions_created": 0,
        "requires_fitted_residual_model": False,
        "status": "READY_WHEN_CAUSAL_INPUTS_AVAILABLE",
    }
    assert report["runtime_scope"] == {
        "black_scholes_baseline_symbols": ["NVDA", "GOOG", "AAPL"],
        "bsgp_eligibility_pilot_symbols": ["NVDA", "GOOG", "MU"],
        "live_symbol_count": 3,
        "live_symbols": ["NVDA", "GOOG", "AAPL"],
        "source": "configured-watchlist-or-explicit-symbols",
    }
    assert len(report["gate"]["gates"]) == 10
    monitoring = pd.read_parquet(result.run_directory / "pricing-monitoring.parquet")
    live_rows = monitoring.loc[monitoring["category"].eq("live_route")]
    assert set(live_rows["scope_value"]) == {"NVDA", "GOOG", "AAPL"}
    assert live_rows["status"].eq("TARGET_BAR_NOT_READY").all()
    public_lockbox = report["closed_lockbox_inventory"]
    assert public_lockbox["target_snapshot_fors_redacted"] is True
    assert public_lockbox["target_output_paths_redacted"] is True
    assert "target_snapshot_fors" not in public_lockbox
    assert "outputs" not in public_lockbox
    assert (result.run_directory / "closed-lockbox-inventory.json").is_file()


def test_pricing_cli_scope_defaults_to_watchlist_and_symbols_override(
    tmp_path: Path,
) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text(
        "# active Pricing universe\nNVDA\nGOOG\nMU\nAAPL\nMSFT\n",
        encoding="utf-8",
    )

    assert resolve_pricing_symbols(symbols=None, watchlist=watchlist) == (
        "NVDA",
        "GOOG",
        "MU",
        "AAPL",
        "MSFT",
    )
    assert resolve_pricing_symbols(
        symbols=("MU", "NVDA", "GOOG", "TSLA"),
        watchlist=tmp_path / "not-read.txt",
    ) == ("MU", "NVDA", "GOOG", "TSLA")
    with pytest.raises(ValueError, match="pilot symbols: MU"):
        resolve_pricing_symbols(
            symbols=("NVDA", "GOOG", "AAPL"),
            watchlist=watchlist,
        )


def test_all_pricing_parquets_have_exact_schema_and_one_readable_id(tmp_path: Path) -> None:
    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA",),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    for name, expected in _OUTPUTS.items():
        observed = pd.read_parquet(result.run_directory / name)
        assert observed.columns.tolist() == expected.names
        assert observed.columns.tolist().count("id") == 1
        assert observed.columns[0] == "id"
