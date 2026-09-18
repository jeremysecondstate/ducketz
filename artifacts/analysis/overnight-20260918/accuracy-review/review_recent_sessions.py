"""Read-only diagnostics of immutable, verified daily actuals reviews."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.ui.gameplan_stats_data import load_gameplan_stats, prediction_metrics, review_sessions


ROOT = Path('C:/DATASTORE')
OUTPUT = Path(__file__).parent


def summarize(rows):
    metrics = prediction_metrics(tuple(rows))
    observed = [r for r in rows if r.status == 'EVALUATED']
    scored = [r for r in observed if r.correct is not None]
    result = asdict(metrics)
    result.update(accuracy=metrics.accuracy,
                  bullish_accuracy=metrics.bullish_accuracy,
                  bearish_accuracy=metrics.bearish_accuracy)
    if observed:
        result.update(
            raw_positive=sum(r.actual_return > 0 for r in observed),
            raw_negative=sum(r.actual_return < 0 for r in observed),
            flat=sum(r.actual_return == 0 for r in observed),
            positive_below_or_equal_cost=sum(0 < r.actual_return <= r.target_cost for r in observed),
            cost_adjusted_positive=sum(r.actual_return > r.target_cost for r in observed),
            mean_p=sum(r.probability for r in observed)/len(observed),
            raw_sign_brier_descriptive=sum((r.probability-int(r.actual_return>0))**2 for r in observed)/len(observed),
            always_up_accuracy_on_scored=sum(r.actual_return > 0 for r in scored)/len(scored) if scored else None,
            always_down_accuracy_on_scored=sum(r.actual_return < 0 for r in scored)/len(scored) if scored else None,
        )
    return result


def main():
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'purpose': 'Descriptive diagnostics only; no fitting, parameter search, activation or trading.', 'sessions': []}
    pooled = []
    for session in sorted(review_sessions(ROOT)):
        review = load_gameplan_stats(ROOT, session)
        pooled.extend(review.outcomes)
        result['sessions'].append({'session': session, 'run': str(review.run_directory),
            'excluded_unpromoted': review.excluded_forecasts,
            'metrics': summarize(review.outcomes),
            'by_horizon': {h: summarize(review.rows(h)) for h in ('1h','4h','1d','1w')},
            'execution_only': summarize([r for r in review.outcomes if r.role == 'EXECUTION'])})
    result['pooled'] = summarize(pooled)
    result['limitations'] = ['Seven saved session reviews have different contemporaneous universes and approved-model coverage.', 'Symbols, target clocks and horizons overlap; row counts are not independent sample counts.', 'Outcomes use each immutable review snapshot, not later backfilled maturity.', 'Raw-sign Brier is a descriptive mismatch diagnostic, not the saved cost-adjusted model score.', 'No change or challenger was selected from these results.']
    (OUTPUT/'recent-sessions.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for s in result['sessions']:
        m=s['metrics']
        print(s['session'], 'scored', m['scored'], 'accuracy', m['accuracy'], 'bull/bear', m['bullish_scored'], m['bearish_scored'], 'costBrier', m['brier'], 'subcost_up', m.get('positive_below_or_equal_cost'), 'rawBrier', m.get('raw_sign_brier_descriptive'))
    print('POOLED', json.dumps(result['pooled']))


if __name__ == '__main__':
    main()
