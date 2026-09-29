"""Machine-local production membership, with an explicit candidate-run override."""

from __future__ import annotations

import os
import re
from pathlib import Path

REPOSITORY_WATCHLIST = Path(__file__).resolve().with_name("watchlist.txt")
WATCHLIST_ENV = "DUCKETS_PRODUCTION_WATCHLIST"


def production_watchlist_path(repository_root: Path | None = None) -> Path:
    """Choose durable membership for this checkout independently of candidate runs."""

    shared = (
        Path(repository_root) / "datafetching" / "watchlist.txt"
        if repository_root is not None
        else REPOSITORY_WATCHLIST
    ).resolve()
    local = shared.with_name("watchlist.local.txt")
    return local if local.is_file() else shared


def configured_watchlist_path(repository_root: Path | None = None) -> Path:
    """Prefer a process override, then this PC's local file, then shared defaults."""

    override = os.environ.get(WATCHLIST_ENV)
    return Path(override).resolve() if override else production_watchlist_path(repository_root)


def normalize_symbol(value: str) -> str:
    symbol = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol) or symbol.endswith(".OPT"):
        raise ValueError(f"Invalid direct equity symbol: {value!r}")
    return symbol


def read_symbols(path: Path | None = None) -> tuple[str, ...]:
    selected = Path(path) if path is not None else configured_watchlist_path()
    values = []
    for line in selected.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            symbol = normalize_symbol(value)
            if symbol not in values:
                values.append(symbol)
    if not values:
        raise ValueError(f"Production watchlist is empty: {selected}")
    return tuple(values)
