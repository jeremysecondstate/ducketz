"""Configuration failures should be clear before the coordinator starts work."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from datafetching.hyperliquid_candles import DEFAULT_INFO_URL
from ml.hyperliquid_coordinator_config import (
    CoordinatorConfig,
    DEFAULT_CONFIG_PATH,
    load_config,
)
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT


def write_config(tmp_path: Path, **values) -> Path:
    path = tmp_path / "markets.json"
    path.write_text(json.dumps({"version": 1, "symbols": ["BTC"], **values}), encoding="utf-8")
    return path


def test_default_config_is_versioned_and_contains_the_requested_four_markets():
    settings = load_config(DEFAULT_CONFIG_PATH)
    assert DEFAULT_CONFIG_PATH.is_absolute()
    assert settings.symbols == ("BTC", "ETH", "HYPE", "ZEC")
    assert settings.interval == "15m"
    assert settings.max_parallel_updates == 2
    assert settings.output_root == DEFAULT_OUTPUT_ROOT.resolve()
    assert settings.control_dir == settings.output_root / "_coordinator"


def test_symbols_normalize_into_frozen_config_and_per_market_settings(tmp_path):
    settings = CoordinatorConfig(
        symbols=[" btc ", "eth"], interval="1h", output_root=tmp_path,
        info_url="https://example.test/info", max_parallel_updates=3,
        close_delay_seconds=8, retry_seconds=12, max_retry_seconds=50,
        repair_every_cycles=0,
    )
    assert settings.symbols == ("BTC", "ETH")
    workers = settings.loop_configs()
    assert list(workers) == ["BTC/1h", "ETH/1h"]
    assert workers["BTC/1h"] is not workers["ETH/1h"]
    for symbol, worker in zip(settings.symbols, workers.values()):
        assert worker.coin == symbol
        assert worker.dataset_dir == tmp_path / symbol / "1h"
        assert worker.info_url == settings.info_url
        assert worker.close_delay_seconds == 8
        assert worker.retry_seconds == 12
        assert worker.max_retry_seconds == 50
        assert worker.repair_every_cycles == 0
    with pytest.raises(FrozenInstanceError):
        settings.interval = "15m"


def test_relative_output_path_uses_config_directory_independently_of_working_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "settings"
    config_dir.mkdir()
    working_dir = tmp_path / "other"
    working_dir.mkdir()
    path = write_config(config_dir, output_root="../dataset")
    monkeypatch.chdir(working_dir)
    settings = load_config(path)
    assert settings.output_root == tmp_path / "dataset"


def test_missing_optional_values_use_defaults_and_endpoint_fallback(tmp_path):
    settings = load_config(write_config(tmp_path), default_info_url="https://example.test/info")
    assert settings.info_url == "https://example.test/info"
    assert settings.interval == "15m"
    assert settings.output_root == DEFAULT_OUTPUT_ROOT.resolve()
    assert settings.close_delay_seconds == 5
    assert settings.retry_seconds == 15
    assert settings.max_retry_seconds == 120
    assert settings.repair_every_cycles == 96


def test_explicit_endpoint_overrides_app_default(tmp_path):
    settings = load_config(
        write_config(tmp_path, info_url="https://explicit.test/info"),
        default_info_url=DEFAULT_INFO_URL,
    )
    assert settings.info_url == "https://explicit.test/info"


@pytest.mark.parametrize("symbols", [[], "BTC", None, ["BTC", 5], ["BTC", " btc "], [""], ["../BTC"], ["BTC-PERP"]])
def test_invalid_symbol_collections_are_rejected(tmp_path, symbols):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, symbols=symbols))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_parallel_updates", True), ("max_parallel_updates", 2.0),
        ("max_parallel_updates", 0), ("max_parallel_updates", "2"),
        ("repair_every_cycles", False), ("repair_every_cycles", 2.5),
        ("repair_every_cycles", -1), ("close_delay_seconds", True),
        ("close_delay_seconds", "5"), ("close_delay_seconds", -1),
        ("close_delay_seconds", 900), ("retry_seconds", 0),
        ("retry_seconds", None), ("max_retry_seconds", 10),
        ("interval", 15), ("interval", "unknown"),
        ("info_url", None), ("info_url", ""),
        ("info_url", "ftp://example.test/info"),
        ("output_root", True), ("output_root", " "),
    ],
)
def test_invalid_setting_types_and_ranges_fail_before_workers_are_created(tmp_path, name, value):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, **{name: value}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_timing_rejected_for_both_python_and_json_config(tmp_path, value):
    with pytest.raises(ValueError):
        CoordinatorConfig(retry_seconds=value)
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, retry_seconds=value))


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_schema_version_must_be_integer_one(tmp_path, version):
    with pytest.raises(ValueError, match="version"):
        load_config(write_config(tmp_path, version=version))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ('{"symbols": ["BTC"]}', "version"),
        ('{"version": 1}', "symbols"),
        ('[]', "JSON object"),
        ('{"version": 1, "symbols": ["BTC"], "symbol": "ETH"}', "Unknown"),
        ('{"version": 1, "symbols": ["BTC"], "symbols": ["ETH"]}', "Duplicate"),
    ],
)
def test_missing_required_fields_unknown_fields_and_ambiguous_json_are_rejected(tmp_path, document, message):
    path = tmp_path / "markets.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_utf8_bom_from_windows_editors_is_supported(tmp_path):
    path = tmp_path / "markets.json"
    path.write_text('{"version": 1, "symbols": ["ZEC"]}', encoding="utf-8-sig")
    assert load_config(path).symbols == ("ZEC",)
