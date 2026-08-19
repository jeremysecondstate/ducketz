from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import datafetching.orchestrate as orchestration
from datafetching import schwab_fetch
from datafetching import FetchResult
from datafetching.parquet_store import ParquetStore
from datafetching.runtime_lock import exclusive_runtime_lock
from options.publication import option_writer_lock_path


def test_loop_a_external_modes_never_enter_slow_failing_cme_or_options_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, bool, bool]] = []

    def provider_batch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        *,
        include_cme: bool,
        include_options: bool,
        include_schwab_price_history: bool,
        **_kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        observed.append(
            (include_cme, include_options, include_schwab_price_history)
        )
        if include_cme or include_options:
            time.sleep(0.5)
            raise TimeoutError("deliberately slow external provider")
        return {
            symbol: (
                FetchResult("databento", 0, 0, 0),
                FetchResult("schwab", 0, 0, 0),
            )
            for symbol in symbols
        }

    monkeypatch.setattr(orchestration, "run_symbols_fetch", provider_batch)
    started = time.perf_counter()
    failures = orchestration.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento", "schwab"),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
    )
    elapsed = time.perf_counter() - started

    assert failures == 0
    assert observed == [(False, False, False)]
    assert elapsed < 0.25


def test_writer_lock_rejects_double_writer_and_recovers_dead_owner(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".writer.lock"
    with exclusive_runtime_lock(lock, process_name="test writer"):
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            with exclusive_runtime_lock(lock, process_name="second writer"):
                raise AssertionError("unreachable")
    assert not lock.exists()

    lock.write_text(
        "process=dead test writer\n"
        "pid=2147483647\n"
        "started_at=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    with exclusive_runtime_lock(lock, process_name="restarted writer"):
        assert f"pid={os.getpid()}" in lock.read_text(encoding="utf-8")
    assert not lock.exists()


def test_inline_options_conflict_fails_before_slow_chain_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowSession:
        calls = 0

        def get_option_chain_snapshot(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            time.sleep(0.5)
            raise TimeoutError("deliberate slow option provider")

    class EmptyProvider:
        def fetch_quote(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("quote unavailable")

    session = SlowSession()
    monkeypatch.setattr(schwab_fetch, "_specs_for_profile", lambda _profile: ())
    with exclusive_runtime_lock(
        option_writer_lock_path(tmp_path),
        process_name="external Options runtime",
    ):
        started = time.perf_counter()
        result = schwab_fetch.fetch(
            "GOOG",
            ParquetStore(tmp_path),
            session=session,  # type: ignore[arg-type]
            provider=EmptyProvider(),  # type: ignore[arg-type]
            include_options=True,
        )
        elapsed = time.perf_counter() - started

    assert session.calls == 0
    assert result.error_files >= 1
    assert elapsed < 0.25


def test_loop_a_schwab_quote_only_mode_never_builds_price_history_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuoteFailureProvider:
        def fetch_quote(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("quote unavailable")

    monkeypatch.setattr(
        schwab_fetch,
        "_specs_for_profile",
        lambda _profile: pytest.fail("quote-only mode requested Schwab bars"),
    )

    result = schwab_fetch.fetch(
        "GOOG",
        ParquetStore(tmp_path),
        provider=QuoteFailureProvider(),  # type: ignore[arg-type]
        include_options=False,
        include_price_history=False,
    )

    assert result.provider == "schwab"
    assert result.error_files == 1
