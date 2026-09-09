"""Flush fit progress and warnings so the overnight supervisor can see them."""
from __future__ import annotations

import json
import os
import time
import warnings

import numpy as np


def fit_with_progress(estimator, features, target, *, label: str, **kwargs):
    if os.environ.get("DUCKETZ_TRAINING_PROGRESS") != "1":
        return estimator.fit(features, target, **kwargs)
    started = time.monotonic()
    parameters = estimator.get_params(deep=True)
    verbose = {
        key: True if isinstance(value, (bool, np.bool_)) else 1
        for key, value in parameters.items()
        if key == "verbose" or key.endswith("__verbose")
    }
    originals = {key: parameters[key] for key in verbose}
    if verbose:
        estimator.set_params(**verbose)
    warning_count = 0

    def emit(event: str, **fields) -> None:
        print(json.dumps({"training_event": event, "fit": label, **fields}, default=str), flush=True)

    def show_warning(message, category, filename, lineno, file=None, line=None):
        nonlocal warning_count
        warning_count += 1
        emit("FIT_WARNING", category=category.__name__, message=str(message))

    emit("FIT_START", rows=len(features), columns=getattr(features, "shape", (0, 0))[1])
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.showwarning = show_warning
            result = estimator.fit(features, target, **kwargs)
        model = estimator.steps[-1][1] if hasattr(estimator, "steps") else estimator
        losses = np.asarray(getattr(model, "loss_curve_", []), dtype=float)
        if len(losses) and not np.isfinite(losses).all():
            raise RuntimeError(f"{label} produced non-finite training loss")
        emit("FIT_COMPLETE", elapsed_seconds=round(time.monotonic() - started, 2),
             iterations=getattr(model, "n_iter_", None), loss=getattr(model, "loss_", None), warnings=warning_count)
        return result
    except BaseException as exc:
        emit("FIT_FAILED", elapsed_seconds=round(time.monotonic() - started, 2), error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if originals:
            estimator.set_params(**originals)
