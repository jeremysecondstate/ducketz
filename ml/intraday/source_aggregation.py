"""Project an already validated, time-sorted native minute frame once.

Validation, source coverage, session selection and numeric reductions stay with
the caller. Rows retain their original pandas scalar types; converting them to
floats here would change reductions for lower precision and integer columns.
"""
from __future__ import annotations

import numpy as np


def source_minute_groups(frame):
    """Yield quarter, typed rows and positional bounds without per-group frames.

    The caller supplies the stable time order from its native-input validator.
    Bounds index that exact frame, allowing the caller to retain its original
    pandas numeric reductions and their precision/overflow behavior.
    """
    fields = ("symbol", "opened_at", "available_at", "availability_kind",
              "open", "high", "low", "close", "volume", "source_id", "source_dataset")
    native = list(frame[list(fields)].itertuples(index=False, name="NativeMinute"))
    slots = frame.opened_at.dt.floor("15min")
    boundaries = [*np.flatnonzero(slots.ne(slots.shift()).to_numpy()), len(frame)]
    for begin, end in zip(boundaries, boundaries[1:]):
        yield slots.iloc[begin], native[begin:end], begin, end
