"""Optional real-price catch-up inside the existing nightly acquisition owner."""
from datetime import timedelta
from zoneinfo import ZoneInfo


def capture_optional_fmp_history(root, symbols, *, now):
    # Seven inclusive NY dates provide overlap across weekends and holidays.
    # Native timestamps/receipt clocks determine eligibility, not request success.
    end = now.astimezone(ZoneInfo('America/New_York')).date()
    start = end - timedelta(days=6)
    try:
        from .fmp_price_history_receipts import capture_native_five_minute_history
        report = capture_native_five_minute_history(root, symbols, start_date=start, end_date=end)
        return {'scope':'optional_observed_fmp_5m_history','start_date':str(start),
                'end_date':str(end),'result':report,'core_work_continues':True}
    except Exception as exc:
        # Provider exception strings can contain authenticated URLs. Actual
        # per-symbol errors are retained in the acquisition report/nightly log;
        # successful and empty responses have immutable raw receipt objects.
        return {'scope':'optional_observed_fmp_5m_history','start_date':str(start),
                'end_date':str(end),'status':'error','error_type':type(exc).__name__,
                'core_work_continues':True}
