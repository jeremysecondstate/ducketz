"""Verify the portable historical-UNKNOWN patch without importing the application."""
from __future__ import annotations

import ast
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import textwrap
from types import SimpleNamespace


PACKAGE = "docs/development/patches/intraday-historical-unknown-liveness"
SOURCE = "ml/intraday/account_execution.py"
TEST = "tests/test_intraday_historical_unknown_liveness.py"
METHOD = "_requires_prior_intention_reconciliation"
HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n?$")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def parse_patch(raw):
    require(b"\r" not in raw, "The portable patch must use LF newlines")
    lines = raw.decode("utf-8").splitlines(keepends=True)
    members = {}
    index = 0
    while index < len(lines):
        require(lines[index].startswith("--- "), "Missing old-file patch header")
        old = lines[index][4:].strip()
        index += 1
        require(index < len(lines) and lines[index].startswith("+++ b/"),
                "Missing new-file patch header")
        path = lines[index][6:].strip()
        require(path not in members, "Duplicate patch member")
        require(old in {"a/" + path, "/dev/null"}, "Unexpected old-file path")
        index += 1
        hunks = []
        while index < len(lines) and not lines[index].startswith("--- "):
            match = HEADER.fullmatch(lines[index])
            require(match is not None, "Malformed unified hunk")
            old_start, old_count, new_start, new_count = match.groups()
            index += 1
            body = []
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ")):
                require(lines[index][:1] in {" ", "+", "-"}, "Invalid hunk line")
                body.append(lines[index])
                index += 1
            require(sum(line[0] != "+" for line in body) == int(old_count or 1),
                    "Old hunk line count differs")
            require(sum(line[0] != "-" for line in body) == int(new_count or 1),
                    "New hunk line count differs")
            hunks.append((int(old_start), int(new_start), body))
        require(hunks, "Empty patch member")
        members[path] = (old == "/dev/null", hunks)
    return members


def transform(text, hunks, *, reverse=False):
    """Strict line-position/context reconstruction; no fuzzy patch application."""
    original = text.splitlines(keepends=True)
    result = []
    cursor = 0
    for old_start, new_start, body in hunks:
        start = (new_start if reverse else old_start) - 1
        start = max(0, start)
        require(start >= cursor, "Overlapping or out-of-order patch hunks")
        result.extend(original[cursor:start])
        removed = "+" if reverse else "-"
        inserted = "-" if reverse else "+"
        consumed = [line[1:] for line in body if line[0] != inserted]
        replacement = [line[1:] for line in body if line[0] != removed]
        require(original[start:start + len(consumed)] == consumed,
                "Exact hunk context differs from supplied source")
        result.extend(replacement)
        cursor = start + len(consumed)
    result.extend(original[cursor:])
    return "".join(result)


def raw_with_newlines(text, encoding):
    require(encoding in {"LF", "CRLF"}, "Unsupported member newline convention")
    return text.replace("\n", "\r\n").encode() if encoding == "CRLF" else text.encode()


def predicate_checks(hunks):
    added = "".join(line[1:] for _, _, body in hunks for line in body if line[0] == "+")
    method_hunks = [body for _, _, body in hunks
                    if any(line.startswith("+    def " + METHOD + "(") for line in body)]
    require(len(method_hunks) == 1, "Expected one complete added predicate hunk")
    method = "".join(line[1:] for line in method_hunks[0] if line[0] == "+")
    tree = ast.parse(textwrap.dedent(method))
    require(len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
            and tree.body[0].name == METHOD, "Patch does not contain the owned predicate")
    require(added.count("and self." + METHOD + "(r)") == 1,
            "Pending selection must call the new predicate exactly once")
    pacific = timezone(timedelta(hours=-7), "Pacific daylight fixture")

    def instant(value):
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError("Synthetic instant requires an aware datetime")
        return result.astimezone(timezone.utc)

    namespace = {"instant": instant, "PACIFIC": pacific}
    exec(compile(tree, "<reviewed-patch-predicate>", "exec"), namespace)
    predicate = namespace[METHOD]
    session = SimpleNamespace(session_started_at=instant("2025-06-05T08:00:00+00:00"))
    base = dict(status="RESERVED", broker_order_id=None, filled_quantity=0,
                target_end="2025-06-04T23:45:00+00:00")
    cases = [
        ({}, False),
        ({"status": "UNKNOWN"}, False),
        ({"target_end": "2025-06-05T06:59:59+00:00"}, False),
        ({"target_end": "2025-06-05T07:00:00+00:00"}, True),
        ({"target_end": "2025-06-05T08:00:00+00:00"}, True),
        ({"target_end": "2025-06-06T08:00:00+00:00"}, True),
        ({"target_end": None}, True),
        ({"target_end": "invalid"}, True),
        ({"target_end": "2025-06-04T23:45:00"}, True),
        ({"target_end": 17}, True),
        ({"broker_order_id": "synthetic-order"}, True),
        ({"filled_quantity": 1}, True),
        *[({"status": status}, True) for status in
          ("SUBMITTED", "WORKING", "PARTIAL", "FILLED", "REJECTED", "CANCELLED")],
    ]
    for overrides, expected in cases:
        native = SimpleNamespace(**(base | overrides))
        before = vars(native).copy()
        require(predicate(session, native) is expected,
                "Historical uncertainty classification differs: " + repr(overrides))
        require(vars(native) == before, "Predicate mutated the original reservation")
    # Compare Pacific days even when the session uses another timezone offset.
    equivalent = SimpleNamespace(session_started_at=instant("2025-06-05T01:00:00-07:00"))
    require(predicate(equivalent, SimpleNamespace(**base)) is False,
            "Equivalent session instants classify differently")
    return len(cases) + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-source-match", action="store_true",
                        help="Require exact reviewed final consumer and integration-test bytes")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    metadata = json.loads((root / (PACKAGE + ".json")).read_text(encoding="utf-8"))
    raw = (root / (PACKAGE + ".patch")).read_bytes()
    require(sha(raw) == metadata["patch_sha256"], "Patch bytes differ from reviewed metadata")
    members = parse_patch(raw)
    require(set(members) == {SOURCE, TEST}, "Unexpected application or test patch member")
    inventory = {member["path"]: member for member in metadata["members"]}
    require(set(inventory) == set(members), "Patch inventory differs")
    reconstructed = []
    unmatched = []
    for path, (added, hunks) in members.items():
        member = inventory[path]
        require(member["operation"] == ("add" if added else "modify"),
                "Declared operation differs from patch")
        if added:
            after = transform("", hunks)
            require(sha(raw_with_newlines(after, member["final_newline_encoding"]))
                    == member["after_sha256"], "Added test bytes differ")
            ast.parse(after)
        installed = root / path
        if installed.is_file():
            current = installed.read_bytes()
            if sha(current) != member["after_sha256"]:
                require(not args.require_source_match,
                        "Present source differs from reviewed final bytes: " + path)
                unmatched.append(path)
                continue
            after = current.decode("utf-8").replace("\r\n", "\n")
            before = transform(after, hunks, reverse=True)
            require(transform(before, hunks) == after, "Patch round trip differs")
            expected = member.get("before_sha256")
            require((sha(raw_with_newlines(before, member["before_newline_encoding"]))
                     if expected else before) == (expected if expected else ""),
                    "Reconstructed exact preimage differs: " + path)
            reconstructed.append(path)
        elif args.require_source_match:
            raise AssertionError("Required reviewed source is absent: " + path)
    cases = predicate_checks(members[SOURCE][1])
    print("HISTORICAL_UNKNOWN_PACKAGE_CHECK=" + json.dumps({
        "patch_sha256": sha(raw), "predicate_cases": cases,
        "reconstructed_members": reconstructed,
        "different_consumer_composition": unmatched,
        "integration": "not run; full synthetic executor tests require the adapted application",
        "external_operations": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
