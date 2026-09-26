"""Configuration for shared, multi-symbol public Hyperliquid data updates."""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from datafetching.hyperliquid_candles import DEFAULT_INFO_URL
from ml.hyperliquid_data_loop import LoopConfig
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "hyperliquid-markets.json"
CONFIG_VERSION = 1


@dataclass(frozen=True)
class CoordinatorConfig:
    """One calculation implementation, with separate data and state per symbol."""

    symbols: tuple[str, ...] = ("BTC", "ETH", "HYPE", "ZEC")
    interval: str = "15m"
    output_root: Path = DEFAULT_OUTPUT_ROOT
    info_url: str = DEFAULT_INFO_URL
    max_parallel_updates: int = 2
    close_delay_seconds: float = 5.0
    retry_seconds: float = 15.0
    max_retry_seconds: float = 120.0
    repair_every_cycles: int = 96

    def __post_init__(self):
        if not isinstance(self.symbols, (tuple, list)) or not self.symbols:
            raise ValueError("symbols must be a nonempty array of perpetual coin symbols.")
        if any(not isinstance(symbol, str) for symbol in self.symbols):
            raise ValueError("Every symbol must be a string.")
        symbols = tuple(symbol.strip().upper() for symbol in self.symbols)
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique after removing whitespace and normalizing case.")
        if not isinstance(self.interval, str):
            raise ValueError("interval must be a supported fixed candle interval string.")
        if not isinstance(self.output_root, (str, Path)) or not str(self.output_root).strip():
            raise ValueError("output_root must be a nonempty filesystem path.")
        if not isinstance(self.info_url, str) or not self.info_url.strip():
            raise ValueError("info_url must be an absolute HTTP or HTTPS URL.")
        info_url = self.info_url.strip()
        parsed_url = urlsplit(info_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("info_url must be an absolute HTTP or HTTPS URL.")
        if type(self.max_parallel_updates) is not int or self.max_parallel_updates < 1:
            raise ValueError("max_parallel_updates must be a positive integer.")
        if type(self.repair_every_cycles) is not int or self.repair_every_cycles < 0:
            raise ValueError("repair_every_cycles must be a nonnegative integer.")
        for name in ("close_delay_seconds", "retry_seconds", "max_retry_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number.")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "output_root", Path(self.output_root).resolve())
        object.__setattr__(self, "info_url", info_url)
        # Use exactly the same symbol, interval, and timing rules as the existing
        # single-market worker, rather than maintaining competing definitions.
        self.loop_configs()

    @property
    def control_dir(self) -> Path:
        return self.output_root / "_coordinator"

    def loop_configs(self) -> dict[str, LoopConfig]:
        return {
            f"{symbol}/{self.interval}": LoopConfig(
                coin=symbol,
                interval=self.interval,
                output_root=self.output_root,
                info_url=self.info_url,
                close_delay_seconds=self.close_delay_seconds,
                retry_seconds=self.retry_seconds,
                max_retry_seconds=self.max_retry_seconds,
                repair_every_cycles=self.repair_every_cycles,
            )
            for symbol in self.symbols
        }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError(f"Non-finite JSON numbers are unsupported: {value}")


def load_config(
    path: str | Path, *, default_info_url: str = DEFAULT_INFO_URL,
) -> CoordinatorConfig:
    """Read versioned JSON; relative data paths belong to its directory.

    ``default_info_url`` allows the caller to use the application's existing
    public API endpoint setting without this module opening any credential file.
    A URL explicitly provided in this config takes precedence.
    """
    config_path = Path(path).resolve()
    values = json.loads(
        config_path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(values, dict):
        raise ValueError("Coordinator configuration must be a JSON object.")
    known_keys = {field.name for field in fields(CoordinatorConfig)} | {"version"}
    unknown_keys = values.keys() - known_keys
    if unknown_keys:
        raise ValueError(f"Unknown coordinator configuration keys: {', '.join(sorted(unknown_keys))}")
    version = values.pop("version", None)
    if type(version) is not int or version != CONFIG_VERSION:
        raise ValueError(f"Coordinator configuration requires version: {CONFIG_VERSION}.")
    if "symbols" not in values:
        raise ValueError("Coordinator configuration requires a nonempty symbols array.")
    if "output_root" in values:
        raw_path = values["output_root"]
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("output_root must be a nonempty filesystem path string.")
        output_root = Path(raw_path)
        if not output_root.is_absolute():
            output_root = config_path.parent / output_root
        values["output_root"] = output_root
    values.setdefault("info_url", default_info_url)
    return CoordinatorConfig(**values)
