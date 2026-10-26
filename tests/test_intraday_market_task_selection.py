"""Task selection preserves provider progress and old recovery without rescans."""
from copy import deepcopy

import pytest

from ml.intraday.market_collection import _select_tasks


def task(key, quarter, provider="schwab", kind="stock_quote", attempts=0, endpoint="quotes"):
    return (key, {"quarter_start": quarter, "attempts": attempts,
                  "request": {"provider": provider, "source_kind": kind, "endpoint": endpoint}})


@pytest.mark.parametrize("maximum,expected", [
    (1, ["k9"]),
    (2, ["k9", "old"]),
    (3, ["k9", "k8", "old"]),
    (4, ["k9", "k8", "background", "old"]),
    (8, ["k9", "k8", "k7", "k6", "k5", "background", "old"]),
])
def test_task_selection_preserves_current_lanes_background_and_oldest(maximum, expected):
    due = [task("old", "2026-10-19"), task("k5", "2026-10-26"),
           task("background", "2026-10-26", "fmp", "macro"),
           task("k6", "2026-10-26", "fmp"), task("k7", "2026-10-26", "databento"),
           task("k8", "2026-10-26", endpoint="stream"), task("k9", "2026-10-26")]
    before = deepcopy(due)
    actual = _select_tasks(due, maximum)
    assert [item[0] for item in actual] == expected
    assert due == before
    assert all(any(item is original for original in due) for item in actual)


def test_task_selection_preserves_low_attempt_priority_and_key_ties():
    due = [task("a", "2026-10-26", attempts=1), task("b", "2026-10-26", attempts=0),
           task("c", "2026-10-26", attempts=0)]
    assert [item[0] for item in _select_tasks(due, 3)] == ["c", "b", "a"]


def test_task_selection_preserves_duplicate_equality_effects():
    first = task("same", "2026-10-26")
    equal_copy = deepcopy(first)
    old = task("old", "2026-10-19")
    due = [first, equal_copy, old]
    actual = _select_tasks(due, 2)
    assert actual[0] is first
    assert actual[1] is old


def test_task_selection_empty_and_complete_prefix():
    assert _select_tasks([], 8) == []
    due = [task("a", "2026-10-25", "fmp", "macro"), task("b", "2026-10-26"),
           task("c", "2026-10-25")]
    actual = _select_tasks(due, 8)
    assert [item[0] for item in actual] == ["b", "c", "a"]


def test_task_selection_groups_many_quarters_once():
    # Count source accesses instead of timing. A collector with a real backlog
    # must not rescan every task once for each quarter merely to choose eight.
    accesses = [0]
    class CountedTask(dict):
        def __getitem__(self, key):
            if key == "quarter_start":
                accesses[0] += 1
            return super().__getitem__(key)
    due = []
    for quarter in range(200):
        for index in range(10):
            key, value = task(f"k{quarter:03}-{index:02}", f"quarter-{quarter:03}")
            due.append((key, CountedTask(value)))
    selected = _select_tasks(due, 8)
    assert len(selected) == 8
    assert selected[-1][0] == "k000-00"
    assert accesses[0] < 12 * len(due)

@pytest.mark.parametrize("maximum,expected", [
    (0, []), (-1, ["new", "background"]), (-2, ["new"]), (-3, []), (-10, []),
])
def test_task_selection_preserves_direct_slice_semantics(maximum, expected):
    due = [task("old", "2026-10-19"), task("background", "2026-10-26", "fmp", "macro"),
           task("new", "2026-10-26")]
    actual = _select_tasks(due, maximum)
    assert [item[0] for item in actual] == expected
    assert all(any(item is original for original in due) for item in actual)
