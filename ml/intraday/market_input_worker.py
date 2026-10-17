"""Market inputs and durable retries owned by an already-started manual trader."""


def run_market_input_worker(collector, *, clock, stopped, pause, reference_for, event):
    """No scheduler/start authority; retry work never blocks the model worker."""
    if collector is None:
        return
    last_reference = None
    while not stopped():
        reference = reference_for(clock())
        try:
            if reference != last_reference:
                last_reference = reference
                event("market_inputs_collected", collector.collect(reference))
            else:
                recovered = collector.recover_pending()
                if recovered:
                    event("market_inputs_retry_completed", recovered)
        except Exception as exc:
            event("market_inputs_error", {"error_type": type(exc).__name__,
                "reference_at": reference.isoformat(), "core_forecasts_continue": True})
        pause(1)
