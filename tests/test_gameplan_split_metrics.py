"""Direction cards retain the saved prediction population and scoring rules."""
from __future__ import annotations

import pytest

from app.ui.gameplan_stats import percent
from app.ui.gameplan_stats_data import load_gameplan_stats
from gameplan_stats_fixture import forecast, write_review


def test_direction_subgroups_reconcile_for_each_company_and_horizon(tmp_path):
    rows = []
    # More companies than the design mockup; none may be omitted from metrics.
    symbols = tuple(f"CO{index:02d}" for index in range(22))
    for index, symbol in enumerate(symbols):
        rows.extend([
            forecast(symbol, 4, direction="BULLISH", change=.02 if index % 2 else -.02),
            forecast(symbol, 5, direction="BEARISH", change=-.02 if index % 3 else .02),
            forecast(symbol, 6, direction="NO_EDGE", change=.01),
            forecast(symbol, 7, direction="BULLISH", status="MATURE_AWAITING_DATA"),
            forecast(symbol, 8, direction="BEARISH", status="PENDING_MATURITY"),
            forecast(symbol, 4, horizon="4h", direction="BULLISH", change=0.0),
        ])
    write_review(tmp_path, rows)
    review = load_gameplan_stats(tmp_path)
    assert review.symbols == symbols
    for horizon in ("all", "1h", "4h", "1d", "1w"):
        for symbol in (None, *symbols):
            metric = review.metrics(horizon, symbol)
            assert metric.correct == metric.bullish_correct + metric.bearish_correct
            assert metric.scored == metric.bullish_scored + metric.bearish_scored
            if metric.scored:
                weighted = (metric.bullish_correct + metric.bearish_correct) / metric.scored
                assert metric.accuracy == pytest.approx(weighted)
                assert percent(metric.accuracy) == percent(weighted)
            else:
                assert percent(metric.accuracy) == "—"
    overall = review.metrics()
    assert overall.scored == 66  # Includes evaluated flat bullish calls as incorrect.
    assert overall.neutral == overall.pending == overall.awaiting_data == 22
    assert overall.evaluated == 88  # Evaluated neutral calls still contribute to Brier.
    expected_brier = sum(row.brier for row in review.outcomes if row.brier is not None) / 88
    assert overall.brier == pytest.approx(expected_brier)


@pytest.mark.parametrize("correct,scored,shown", [(0, 0, "—"), (0, 2, "0.0%"),
                                                    (2, 3, "66.7%"), (1, 16, "6.3%")])
def test_card_percentage_is_count_ratio_with_existing_rounding(correct, scored, shown):
    assert percent(correct / scored if scored else None) == shown
