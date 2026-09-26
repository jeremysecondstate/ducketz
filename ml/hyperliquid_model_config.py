"""Independent model settings for the shared Hyperliquid market universe.

Reading this file never opens an account credential file or imports an execution
runtime. Market membership and the datastore location come from the existing
data coordinator configuration instead of a second symbol list.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ml.hyperliquid_coordinator_config import CoordinatorConfig
    from ml.hyperliquid_models import ModelSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKETS_CONFIG_PATH = PROJECT_ROOT / "configs" / "hyperliquid-markets.json"
DEFAULT_MODEL_CONFIG_PATH = PROJECT_ROOT / "configs" / "hyperliquid-models.json"
CONFIG_VERSION = 1


@dataclass(frozen=True)
class ModelConfig:
    """Shared model recipe; fitted models and predictions remain per market."""

    markets_config: Path = DEFAULT_MARKETS_CONFIG_PATH
    horizons_bars: tuple[int, ...] = (4,)
    retrain_seconds: float = 3600.0
    poll_seconds: float = 5.0
    retry_seconds: float = 60.0
    model_threads: int = 2
    min_train_rows: int = 1000
    calibration_rows: int = 192
    assessment_rows: int = 288
    max_model_age_seconds: float = 86400.0

    def __post_init__(self):
        if not isinstance(self.markets_config, (str, Path)) or not str(self.markets_config).strip():
            raise ValueError("markets_config must be a nonempty filesystem path.")
        if not isinstance(self.horizons_bars, (tuple, list)) or not self.horizons_bars:
            raise ValueError("horizons_bars must be a nonempty array of positive integers.")
        if any(type(value) is not int or value < 1 for value in self.horizons_bars):
            raise ValueError("Every horizon must be a positive integer candle count.")
        if len(set(self.horizons_bars)) != len(self.horizons_bars):
            raise ValueError("horizons_bars must contain distinct candle counts.")
        for name in ("retrain_seconds", "poll_seconds", "retry_seconds", "max_model_age_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number.")
        for name in ("model_threads", "min_train_rows", "calibration_rows", "assessment_rows"):
            value = getattr(self, name)
            minimum = 1 if name == "model_threads" else 2
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}.")
        object.__setattr__(self, "markets_config", Path(self.markets_config).resolve())
        object.__setattr__(self, "horizons_bars", tuple(sorted(self.horizons_bars)))

    def load_markets(self) -> CoordinatorConfig:
        """Read the current data config so symbol additions need only one edit."""
        from ml.hyperliquid_coordinator_config import load_config as load_markets_config

        return load_markets_config(self.markets_config)

    @property
    def models_root(self) -> Path:
        """Derive the isolated model namespace from the current data root."""
        return self.load_markets().output_root / "_models"

    def model_settings(self, horizon_bars: int) -> ModelSettings:
        """Construct the model recipe lazily without loading sklearn for config IO."""
        if type(horizon_bars) is not int or horizon_bars not in self.horizons_bars:
            raise ValueError("horizon_bars must be one of this configuration's horizons.")
        from ml.hyperliquid_models import ModelSettings

        return ModelSettings(
            horizon_bars=horizon_bars,
            min_train_rows=self.min_train_rows,
            calibration_rows=self.calibration_rows,
            assessment_rows=self.assessment_rows,
            random_state=42,
            model_threads=self.model_threads,
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"Duplicate model configuration key: {key}")
        values[key] = value
    return values


def _invalid_constant(value: str):
    raise ValueError(f"Non-finite JSON numbers are unsupported: {value}")


def load_config(path: str | Path = DEFAULT_MODEL_CONFIG_PATH) -> ModelConfig:
    """Read strict version-one JSON, resolving relative paths beside that file."""
    config_path = Path(path).resolve()
    values = json.loads(
        config_path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(values, dict):
        raise ValueError("Model configuration must be a JSON object.")
    known_keys = {field.name for field in fields(ModelConfig)} | {"version"}
    unknown_keys = values.keys() - known_keys
    if unknown_keys:
        raise ValueError(f"Unknown model configuration keys: {', '.join(sorted(unknown_keys))}")
    version = values.pop("version", None)
    if type(version) is not int or version != CONFIG_VERSION:
        raise ValueError(f"Model configuration requires version: {CONFIG_VERSION}.")
    if "markets_config" in values:
        raw_path = values["markets_config"]
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("markets_config must be a nonempty filesystem path string.")
        markets_path = Path(raw_path)
        if not markets_path.is_absolute():
            markets_path = config_path.parent / markets_path
        values["markets_config"] = markets_path
    return ModelConfig(**values)
