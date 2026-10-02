from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from datafetching import symbol_universe


@pytest.fixture
def watchlists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    shared = tmp_path / "datafetching" / "watchlist.txt"
    shared.parent.mkdir()
    shared.write_text("AAPL\nAMZN\n", encoding="utf-8")
    monkeypatch.setattr(symbol_universe, "REPOSITORY_WATCHLIST", shared)
    monkeypatch.delenv(symbol_universe.WATCHLIST_ENV, raising=False)
    return shared, shared.with_name("watchlist.local.txt")


def test_local_membership_replaces_shared_defaults_without_editing_them(watchlists) -> None:
    shared, local = watchlists
    assert symbol_universe.production_watchlist_path() == shared
    assert symbol_universe.read_symbols() == ("AAPL", "AMZN")

    local.write_text("# This PC\nmsft\nTSLA\nMSFT\n", encoding="utf-8")
    assert symbol_universe.production_watchlist_path() == local
    assert symbol_universe.configured_watchlist_path() == local
    assert symbol_universe.read_symbols() == ("MSFT", "TSLA")
    assert shared.read_text(encoding="utf-8") == "AAPL\nAMZN\n"


def test_candidate_override_does_not_redirect_persistent_membership(
    watchlists, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, local = watchlists
    local.write_text("MSFT\n", encoding="utf-8")
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("MSFT\nTSLA\n", encoding="utf-8")
    monkeypatch.setenv(symbol_universe.WATCHLIST_ENV, str(candidate))

    assert symbol_universe.configured_watchlist_path() == candidate
    assert symbol_universe.read_symbols() == ("MSFT", "TSLA")
    assert symbol_universe.production_watchlist_path() == local
    local.unlink()
    assert symbol_universe.production_watchlist_path() == shared


def test_local_membership_is_independent_of_working_directory(
    watchlists, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, local = watchlists
    local.write_text("MSFT\n", encoding="utf-8")
    unrelated = tmp_path / "another-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    assert symbol_universe.configured_watchlist_path() == local
    assert symbol_universe.configured_watchlist_path(tmp_path) == local


@pytest.mark.parametrize("contents", ["# no selected symbols\n", "MSFT\nINVALID SYMBOL\n"])
def test_invalid_local_membership_fails_instead_of_using_shared_symbols(watchlists, contents) -> None:
    _, local = watchlists
    local.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        symbol_universe.read_symbols()


@pytest.mark.parametrize("candidate_override", [False, True])
def test_fresh_runtimes_share_local_or_candidate_membership(
    watchlists, tmp_path: Path, candidate_override: bool,
) -> None:
    shared, local = watchlists
    local.write_text("MSFT\nTSLA\n", encoding="utf-8")
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("MSFT\nTSLA\nF\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop(symbol_universe.WATCHLIST_ENV, None)
    if candidate_override:
        environment[symbol_universe.WATCHLIST_ENV] = str(candidate)
    # A fresh process must select the same scope before module-level contracts,
    # CLI defaults and the guardian's closed launch allowlist are constructed.
    code = """
import json
from pathlib import Path
import sys
from datafetching import symbol_universe
symbol_universe.REPOSITORY_WATCHLIST = Path(sys.argv[1])
from datafetching.orchestrate import DEFAULT_WATCHLIST as fetch_watchlist
from datafetching.options_runtime import DEFAULT_WATCHLIST as options_watchlist
from datafetching.databento_cold_start import DEFAULT_WATCHLIST as history_watchlist
from ml.option_pricing_runtime import DEFAULT_WATCHLIST as pricing_watchlist
from ml.strategy_pricing_canary import DEFAULT_WATCHLIST as canary_watchlist
from ml.overnight_runtime import _production_watchlist
from ml.system_guardian import GUARDIAN_LAUNCHES
from ml.universe import PRODUCTION_LOOPS_SYMBOLS
print(json.dumps({
    'defaults': [str(value) for value in (
        fetch_watchlist, options_watchlist, history_watchlist, pricing_watchlist,
        canary_watchlist, _production_watchlist(Path(sys.argv[1]).parents[1]),
    )],
    'guardian': {launch.runtime: launch.arguments[launch.arguments.index('--watchlist') + 1]
                 for launch in GUARDIAN_LAUNCHES if '--watchlist' in launch.arguments},
    'symbols': PRODUCTION_LOOPS_SYMBOLS,
    'production': str(symbol_universe.production_watchlist_path()),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(shared)],
        env=environment, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    selected = candidate if candidate_override else local
    assert payload["defaults"] == [str(selected)] * 6
    assert payload["guardian"] == {
        runtime: str(selected) for runtime in ("loop_a", "loop_b", "pricing", "options")
    }
    assert payload["symbols"] == (["MSFT", "TSLA", "F"] if candidate_override else ["MSFT", "TSLA"])
    assert payload["production"] == str(local)
    assert shared.read_text(encoding="utf-8") == "AAPL\nAMZN\n"


def test_guardian_keeps_relative_commands_for_the_repository_watchlist(monkeypatch) -> None:
    from ml import system_guardian

    repository = Path(system_guardian.__file__).resolve().parents[1]
    selected = repository / "datafetching" / "watchlist.txt"
    monkeypatch.setattr(system_guardian, "configured_watchlist_path", lambda: selected)
    assert system_guardian._watchlist_argument() == str(Path("datafetching") / "watchlist.txt")
