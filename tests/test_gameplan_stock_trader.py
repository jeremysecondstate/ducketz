from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ml.nightly_gameplan import GameplanPublication
from ml.stock_trader.control import write_activation_intent
from ml.stock_trader.gameplan import (
    GAMEPLAN_STOCK_ENTRY_GRACE_SECONDS,
    gate_gameplan_execution_signals,
    load_current_gameplan_prediction_signals,
    read_gameplan_stock_activation_intent,
    write_gameplan_stock_activation_intent,
)


ACTION_DATE = "2026-09-04"
PACIFIC = ZoneInfo("America/Los_Angeles")


def test_gameplan_stock_activation_requires_both_switches(tmp_path: Path) -> None:
    assert read_gameplan_stock_activation_intent(tmp_path).active is False

    write_activation_intent(tmp_path, active=True)
    broker_only = read_gameplan_stock_activation_intent(tmp_path)
    assert broker_only.active is False
    assert broker_only.reason == "GAMEPLAN_STOCK_INTENT_MISSING"

    path = write_gameplan_stock_activation_intent(tmp_path, active=True)
    assert path.read_text(encoding="utf-8") == (
        "CONFIRM_GAMEPLAN_STOCK_TRADING=TRUE\n"
    )
    active = read_gameplan_stock_activation_intent(tmp_path)
    assert active.active is True
    assert active.reason == "BOTH_STOCK_TRADING_SWITCHES_TRUE"

    write_activation_intent(tmp_path, active=False)
    assert read_gameplan_stock_activation_intent(tmp_path).active is False


def test_gameplan_loader_selects_forward_hour_and_promoted_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )
    observed = pd.Timestamp("2026-09-04T16:01:00Z")

    signals, sources = load_current_gameplan_prediction_signals(
        tmp_path,
        as_of=observed,
        target_horizon="1h",
    )

    assert sorted(signals) == ["AAPL", "AMZN", "GOOG", "MU", "NVDA"]
    assert len(sources) == 6
    aapl = signals["AAPL"]
    assert aapl.prediction_id == "2026-09-04:AAPL:1h@10:00"
    assert aapl.target_window_start == "2026-09-04T16:00:00+00:00"
    assert aapl.actionable_until == "2026-09-04T16:05:00+00:00"
    assert aapl.calibrated_probability == pytest.approx(0.40)
    assert aapl.horizon_probabilities == {
        "1h": pytest.approx(0.40),
        "4h": pytest.approx(0.30),
        "1d": pytest.approx(0.60),
    }
    assert aapl.checkpoint_session == "REGULAR"
    assert "1w" not in aapl.horizon_probabilities

    available, metadata, status, target = gate_gameplan_execution_signals(
        tmp_path,
        signals,
        as_of=observed,
        maximum_target_lead_seconds=2_700,
        target_horizon="1h",
    )
    assert status == "GAMEPLAN_STOCK_ACTIONABLE_RECEIPT_VALIDATED"
    assert target == "2026-09-04T16:00:00+00:00"
    assert sorted(available) == sorted(signals)
    assert metadata["missing_symbols"] == ["SNDK"]


def test_gameplan_gate_fails_closed_after_entry_grace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )
    at_start = pd.Timestamp("2026-09-04T16:01:00Z")
    signals, _sources = load_current_gameplan_prediction_signals(
        tmp_path,
        as_of=at_start,
        target_horizon="1h",
    )
    expired = pd.Timestamp("2026-09-04T16:00:00Z") + pd.Timedelta(
        seconds=GAMEPLAN_STOCK_ENTRY_GRACE_SECONDS
    )

    available, metadata, status, target = gate_gameplan_execution_signals(
        tmp_path,
        signals,
        as_of=expired,
        maximum_target_lead_seconds=2_700,
        target_horizon="1h",
    )

    assert available == {}
    assert status == "GAMEPLAN_STOCK_ENTRY_GRACE_EXPIRED"
    assert target == "2026-09-04T16:00:00+00:00"
    assert metadata["entry_deadline"] == "2026-09-04T16:05:00+00:00"


def test_gameplan_gate_allows_schwab_pm_transition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )
    observed = pd.Timestamp("2026-09-04T20:06:00Z")
    signals, _sources = load_current_gameplan_prediction_signals(
        tmp_path,
        as_of=observed,
        target_horizon="1h",
    )

    available, metadata, status, target = gate_gameplan_execution_signals(
        tmp_path,
        signals,
        as_of=observed,
        maximum_target_lead_seconds=2_700,
        target_horizon="1h",
    )

    assert status == "GAMEPLAN_STOCK_ACTIONABLE_RECEIPT_VALIDATED"
    assert available
    assert target == "2026-09-04T20:00:00+00:00"
    assert metadata["entry_deadline"] == "2026-09-04T20:10:00+00:00"


def test_gameplan_gate_suppresses_consumed_prediction_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )
    observed = pd.Timestamp("2026-09-04T16:01:00Z")
    signals, _sources = load_current_gameplan_prediction_signals(
        tmp_path,
        as_of=observed,
        target_horizon="1h",
    )
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.consumed_live_prediction_ids",
        lambda _root: {signal.prediction_id for signal in signals.values()},
    )

    available, _metadata, status, _target = gate_gameplan_execution_signals(
        tmp_path,
        signals,
        as_of=observed,
        maximum_target_lead_seconds=2_700,
        target_horizon="1h",
    )

    assert available == {}
    assert status == "PREDICTION_GENERATION_ALREADY_CONSUMED"


def test_gameplan_loader_vetoes_opposite_active_four_hour_direction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    forecasts_path = publication.run_directory / "forecasts.parquet"
    frame = pd.read_parquet(forecasts_path)
    conflict = (
        frame["symbol"].eq("AAPL")
        & frame["model_group"].eq("4h")
        & frame["route"].eq("4h@12:00")
    )
    frame.loc[conflict, "calibrated_probability"] = 0.70
    frame.loc[conflict, "direction"] = "BULLISH"
    frame.to_parquet(forecasts_path, index=False)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )

    signals, _sources = load_current_gameplan_prediction_signals(
        tmp_path,
        as_of="2026-09-04T16:01:00Z",
        target_horizon="1h",
    )

    assert "AAPL" not in signals
    assert "AMZN" in signals


def test_gameplan_loader_rejects_direction_probability_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publication = _gameplan_publication(tmp_path)
    forecasts_path = publication.run_directory / "forecasts.parquet"
    frame = pd.read_parquet(forecasts_path)
    malformed = (
        frame["symbol"].eq("AAPL")
        & frame["model_group"].eq("1h")
        & frame["route"].eq("1h@10:00")
    )
    frame.loc[malformed, "calibrated_probability"] = 0.50
    frame.loc[malformed, "direction"] = "BEARISH"
    frame.to_parquet(forecasts_path, index=False)
    monkeypatch.setattr(
        "ml.stock_trader.gameplan.read_current_gameplan",
        lambda _root: publication,
    )

    with pytest.raises(ValueError, match="direction and probability disagree"):
        load_current_gameplan_prediction_signals(
            tmp_path,
            as_of="2026-09-04T16:01:00Z",
            target_horizon="1h",
        )


def _gameplan_publication(root: Path) -> GameplanPublication:
    run = root / "ml" / "nightly-gameplan-runs" / "test-generation"
    run.mkdir(parents=True)
    forecasts = _gameplan_forecasts()
    forecasts.to_parquet(run / "forecasts.parquet", index=False)
    pd.DataFrame({"id": ["intent"]}).to_parquet(
        run / "option-strategy-intents.parquet",
        index=False,
    )
    for name in ("manifest.json", "receipt.json", "gameplan.json"):
        (run / name).write_text("{}\n", encoding="utf-8")
    pointer = root / "ml" / "nightly-gameplan-latest" / "run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{}\n", encoding="utf-8")
    return GameplanPublication(
        run_directory=run,
        manifest={},
        receipt={"action_date": ACTION_DATE},
        pointer={},
    )


def _gameplan_forecasts() -> pd.DataFrame:
    action = pd.Timestamp(f"{ACTION_DATE}T00:00:00", tz=PACIFIC)
    decision = (action - pd.Timedelta(hours=7)).tz_convert("UTC")
    frozen = (action + pd.Timedelta(hours=3, minutes=30)).tz_convert("UTC")
    rows: list[dict[str, object]] = []

    def add(
        symbol: str,
        group: str,
        route: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        probability: float,
        *,
        model_status: str = "PROMOTED",
    ) -> None:
        direction = (
            "BULLISH"
            if probability >= 0.55
            else "BEARISH"
            if probability <= 0.45
            else "NO_EDGE"
        )
        rows.append(
            {
                "id": f"{ACTION_DATE}:{symbol}:{route}",
                "symbol": symbol,
                "model_group": group,
                "route": route,
                "decision_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": start.tz_convert("UTC"),
                "target_window_end": end.tz_convert("UTC"),
                "calibrated_probability": probability,
                "model_family": f"test-{group}-model",
                "model_status": model_status,
                "model_artifact": f"models/{group}.joblib",
                "direction": direction,
                "frozen_at": frozen,
                "action_date": ACTION_DATE,
                "target_contract_version": "overnight-path-targets-v1",
                "execution_authority": "ADVISORY_PAPER_ONLY",
                "broker_orders_enabled": False,
            }
        )

    for symbol in ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK"):
        prior_close = action - pd.Timedelta(hours=7)
        add(symbol, "1h", "1h@04:00", prior_close, action + pd.Timedelta(hours=4), 0.4)
        for endpoint in range(5, 18):
            probability = 0.50 if symbol == "SNDK" and endpoint == 10 else 0.40
            add(
                symbol,
                "1h",
                f"1h@{endpoint:02d}:00",
                action + pd.Timedelta(hours=endpoint - 1),
                action + pd.Timedelta(hours=endpoint),
                probability,
            )
        add(symbol, "4h", "4h@04:00", prior_close, action + pd.Timedelta(hours=4), 0.3)
        for endpoint in (8, 12, 16):
            add(
                symbol,
                "4h",
                f"4h@{endpoint:02d}:00",
                action + pd.Timedelta(hours=endpoint - 4),
                action + pd.Timedelta(hours=endpoint),
                0.30,
            )
        for lead in range(1, 6):
            session = action + pd.Timedelta(days=lead - 1)
            add(
                symbol,
                "1d",
                f"1d@D+{lead}",
                session + pd.Timedelta(hours=6, minutes=30),
                session + pd.Timedelta(hours=13),
                0.60,
            )
        add(
            symbol,
            "1w",
            "1w@D+5",
            action + pd.Timedelta(hours=6, minutes=30),
            action + pd.Timedelta(days=4, hours=13),
            0.65,
            model_status="RESEARCH_NOT_PROMOTED",
        )
    return pd.DataFrame(rows)
