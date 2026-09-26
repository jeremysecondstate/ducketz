"""Configuration must keep the market universe shared and model state separate."""
from dataclasses import FrozenInstanceError, asdict, dataclass
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ml.hyperliquid_model_config import (
    DEFAULT_MARKETS_CONFIG_PATH,
    DEFAULT_MODEL_CONFIG_PATH,
    ModelConfig,
    load_config,
)


def write_config(tmp_path: Path, **values) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"version": 1, **values}), encoding="utf-8")
    return path


def test_default_file_uses_shared_market_config_and_one_hour_initial_horizon():
    settings = load_config(DEFAULT_MODEL_CONFIG_PATH)
    assert settings.markets_config == DEFAULT_MARKETS_CONFIG_PATH
    assert settings.horizons_bars == (4,)
    assert settings.retrain_seconds == 900
    assert settings.model_threads == 2
    assert settings.min_train_rows == 1000
    assert settings.calibration_rows == 192
    assert settings.assessment_rows == 288
    assert settings.max_model_age_seconds == 86400
    assert not hasattr(settings, "symbols")
    with pytest.raises(FrozenInstanceError):
        settings.poll_seconds = 30


def test_version_only_uses_project_defaults_without_reading_market_config(tmp_path):
    settings = load_config(write_config(tmp_path))
    assert settings == ModelConfig()
    assert settings.max_train_rows is None
    missing = load_config(write_config(tmp_path, markets_config="not-created-yet.json"))
    assert missing.markets_config == tmp_path / "not-created-yet.json"


def test_relative_market_path_belongs_to_model_config_directory(tmp_path, monkeypatch):
    settings_dir = tmp_path / "config"
    settings_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = write_config(settings_dir, markets_config="../markets.json")
    monkeypatch.chdir(elsewhere)
    settings = load_config(path)
    assert settings.markets_config == tmp_path / "markets.json"


def test_market_membership_is_read_fresh_and_model_root_is_under_data_root(tmp_path):
    markets_path = tmp_path / "markets.json"

    def write_markets(symbols):
        markets_path.write_text(json.dumps({
            "version": 1, "symbols": symbols, "output_root": "dataset", "interval": "15m",
        }), encoding="utf-8")

    write_markets(["BTC"])
    settings = load_config(write_config(tmp_path, markets_config="markets.json"))
    assert settings.load_markets().symbols == ("BTC",)
    assert settings.models_root == tmp_path / "dataset" / "_models"
    write_markets(["BTC", "ETH"])
    assert settings.load_markets().symbols == ("BTC", "ETH")


def test_horizons_are_sorted_and_recipe_factory_passes_only_model_settings(monkeypatch):
    @dataclass
    class Recipe:
        horizon_bars: int
        min_train_rows: int
        calibration_rows: int
        assessment_rows: int
        random_state: int
        model_threads: int
        max_train_rows: int | None

    monkeypatch.setitem(sys.modules, "ml.hyperliquid_models", SimpleNamespace(ModelSettings=Recipe))
    settings = ModelConfig(
        horizons_bars=[16, 1, 4], min_train_rows=1500, calibration_rows=200,
        assessment_rows=300, model_threads=3, max_train_rows=2000,
    )
    assert settings.horizons_bars == (1, 4, 16)
    assert settings.model_settings(4) == Recipe(4, 1500, 200, 300, 42, 3, 2000)
    with pytest.raises(ValueError, match="configuration's horizons"):
        settings.model_settings(2)
    with pytest.raises(ValueError, match="configuration's horizons"):
        settings.model_settings(True)


@pytest.mark.parametrize("cap", [None, 1000, 2000])
def test_optional_training_cap_round_trips_to_actual_model_recipe(tmp_path, cap):
    config = load_config(write_config(tmp_path, max_train_rows=cap))
    recipe = config.model_settings(4)
    assert recipe.max_train_rows == cap
    assert asdict(recipe)["max_train_rows"] == cap
    assert recipe.min_train_rows == config.min_train_rows == 1000


@pytest.mark.parametrize("cap", [True, False, 0, -1, 999, 1000.0, "1000"])
def test_training_cap_requires_integer_at_least_minimum_fit_size(tmp_path, cap):
    with pytest.raises(ValueError, match="max_train_rows"):
        ModelConfig(max_train_rows=cap)
    with pytest.raises(ValueError, match="max_train_rows"):
        load_config(write_config(tmp_path, max_train_rows=cap))
    with pytest.raises(ValueError, match="max_train_rows"):
        ModelConfig(min_train_rows=1500, max_train_rows=1000)


@pytest.mark.parametrize("horizons", [[], None, "4", [True], [4.0], [0], [-1], [1, 1]])
def test_invalid_horizon_collections_are_rejected(tmp_path, horizons):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, horizons_bars=horizons))


@pytest.mark.parametrize("name", ["retrain_seconds", "poll_seconds", "retry_seconds", "max_model_age_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, "5", None, float("nan"), float("inf")])
def test_invalid_timing_does_not_start_a_worker(tmp_path, name, value):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, **{name: value}))
    with pytest.raises(ValueError):
        ModelConfig(**{name: value})


@pytest.mark.parametrize("name", ["model_threads", "min_train_rows", "calibration_rows", "assessment_rows"])
@pytest.mark.parametrize("value", [0, -1, True, 2.5, "2", None])
def test_counts_are_positive_integers(tmp_path, name, value):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, **{name: value}))


@pytest.mark.parametrize("name", ["min_train_rows", "calibration_rows", "assessment_rows"])
def test_partition_counts_require_at_least_two_rows(tmp_path, name):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, **{name: 1}))


@pytest.mark.parametrize("value", [True, None, "", "   ", 3])
def test_market_path_is_a_nonempty_string(tmp_path, value):
    with pytest.raises(ValueError, match="markets_config"):
        load_config(write_config(tmp_path, markets_config=value))


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_configuration_version_is_exact_integer_one(tmp_path, version):
    with pytest.raises(ValueError, match="version"):
        load_config(write_config(tmp_path, version=version))


@pytest.mark.parametrize(("document", "message"), [
    ('{}', "version"),
    ('[]', "JSON object"),
    ('{"version":1,"symbols":["BTC"]}', "Unknown"),
    ('{"version":1,"output_root":"elsewhere"}', "Unknown"),
    ('{"version":1,"poll_seconds":2,"poll_seconds":5}', "Duplicate"),
    ('{"version":1,"poll_seconds":1e999}', "finite"),
])
def test_ambiguous_or_unsupported_json_is_rejected(tmp_path, document, message):
    path = tmp_path / "models.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_windows_editor_bom_is_supported(tmp_path):
    path = tmp_path / "models.json"
    path.write_text('{"version":1,"horizons_bars":[16,4]}', encoding="utf-8-sig")
    assert load_config(path).horizons_bars == (4, 16)
