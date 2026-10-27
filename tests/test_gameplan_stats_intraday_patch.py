"""Validate the portable saved-price patch without importing an installed application.

The package contains task-owned hunks, not copies of unrelated local modules.
Git application is checked using exact hunk contexts and placeholders for the
unchanged lines that deliberately are not part of this shared package. Full
before/after byte reconstruction was additionally checked on private copies of
the producer's captured source, as recorded by the package manifest.
"""
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/development/patches/gameplan-stats-intraday-evidence.json"
MEMBERS = {
    "app/ui/gameplan_stats.py": "modify",
    "app/ui/gameplan_stats_data.py": "modify",
    "datafetching/fmp_price_history_receipts.py": "modify",
    "datafetching/intraday_dbn_observations.py": "add",
    "datafetching/intraday_fmp_observations.py": "add",
    "docs/development/gameplan-stats-intraday-evidence.md": "add",
    "ml/gameplan_actuals_review.py": "modify",
    "ml/gameplan_stats_handoff.py": "modify",
    "ml/gameplan_stats_outcome_revision.py": "modify",
    "ml/stock_observation_sources.py": "modify",
    "ml/stock_target_prices.py": "modify",
    "tests/test_gameplan_stats_intraday_prices.py": "add",
    "tests/test_gameplan_stats_snapshot_context.py": "add",
    "tests/test_intraday_fmp_observations.py": "add",
    "tests/test_nightly_stats_timing_evidence.py": "modify"
}
HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n")


def load_package():
    manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
    path = ROOT / manifest["patch"]["path"]
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == manifest["patch"]["sha256"]
    assert b"\r" not in raw and raw.endswith(b"\n")
    return manifest, path, parse_patch(raw.decode("utf-8"))


def parse_patch(text):
    sections = []
    for block in text.split("diff --git ")[1:]:
        lines = block.splitlines(keepends=True)
        left, right = lines[0].strip().split(" ")
        assert left.startswith("a/") and right == "b/" + left[2:]
        path = left[2:]
        assert path in MEMBERS
        position = 1
        if lines[position] == "new file mode 100644\n":
            position += 1
        expected_old = "/dev/null" if MEMBERS[path] == "add" else left
        assert lines[position] == "--- " + expected_old + "\n"
        assert lines[position + 1] == "+++ " + right + "\n"
        position += 2
        hunks = []
        while position < len(lines):
            match = HEADER.fullmatch(lines[position])
            assert match, lines[position]
            start, count, new_start, new_count = match.groups()
            position += 1
            body = []
            while position < len(lines) and not lines[position].startswith("@@ "):
                assert lines[position][:1] in {" ", "+", "-"}
                body.append(lines[position])
                position += 1
            hunk = {"start": int(start), "count": int(count or 1),
                    "new_start": int(new_start), "new_count": int(new_count or 1), "body": body}
            assert sum(line[0] in " -" for line in body) == hunk["count"]
            assert sum(line[0] in " +" for line in body) == hunk["new_count"]
            hunks.append(hunk)
        assert hunks
        sections.append({"path": path, "hunks": hunks})
    assert len(sections) == len(MEMBERS)
    assert {section["path"] for section in sections} == set(MEMBERS)
    return sections


def test_manifest_binds_exact_owned_members_and_operations():
    manifest, _, sections = load_package()
    assert manifest["schema_version"] == "gameplan-stats-intraday-evidence-reviewed-patch-v1"
    assert manifest["scope"] == "shared"
    assert {member["path"]: member["operation"] for member in manifest["members"]} == MEMBERS
    assert len(manifest["members"]) == len(MEMBERS)
    assert manifest["behavioral_validation"]["source_sha256"] == {
        member["path"]: member["after_sha256"] for member in manifest["members"]}
    assert manifest["behavioral_validation"]["status"] == "PASS", "Record final behavior checks before publication"
    assert manifest["behavioral_validation"]["checks"], "A placeholder is not behavioral evidence"
    assert manifest["local_exact_byte_validation"]["result"] == "PASS"
    assert manifest["local_exact_byte_validation"]["members_verified"] == len(MEMBERS)
    assert manifest["boundaries"] == {
        "timezone": "America/Los_Angeles", "source_session_close": "17:00",
        "stats_only_start": "17:30", "evening_minimum": "21:00", "dispatcher_kickoff": "21:05",
    }
    indexed = {section["path"]: section for section in sections}
    for member in manifest["members"]:
        for field in ("after_sha256", "after_normalized_lf_sha256"):
            assert re.fullmatch("[a-f0-9]{64}", member[field])
        assert member["final_newline_encoding"] in {"LF", "CRLF", "MIXED"}
        if member["final_newline_encoding"] == "MIXED":
            numbers = member["final_crlf_line_numbers"]
            assert numbers and numbers == sorted(set(numbers))
            assert all(1 <= number <= member["after_line_count"] for number in numbers)
        assert member["hunks"] == len(indexed[member["path"]]["hunks"])
        if member["operation"] == "modify":
            for field in ("before_sha256", "before_normalized_lf_sha256"):
                assert re.fullmatch("[a-f0-9]{64}", member[field])
            assert member["before_line_count"] > 0
        else:
            assert member["before_sha256"] is None and member["before_line_count"] == 0


def test_git_applies_exact_hunks_without_unrelated_source(tmp_path):
    manifest, patch, sections = load_package()
    indexed = {member["path"]: member for member in manifest["members"]}
    expected = {}
    for section in sections:
        path = section["path"]
        member = indexed[path]
        old = [f"# Unchanged line {index + 1} deliberately omitted from shared patch\n"
               for index in range(member["before_line_count"])]
        prior_end = 0
        for hunk in section["hunks"]:
            start = max(0, hunk["start"] - 1)
            assert start >= prior_end
            original = [line[1:] for line in hunk["body"] if line[0] in " -"]
            assert len(original) == hunk["count"]
            old[start:start + hunk["count"]] = original
            prior_end = start + hunk["count"]
        position = 0
        new = []
        for hunk in section["hunks"]:
            start = max(0, hunk["start"] - 1)
            new.extend(old[position:start])
            assert len(new) == max(0, hunk["new_start"] - 1)
            new.extend(line[1:] for line in hunk["body"] if line[0] in " +")
            position = start + hunk["count"]
        new.extend(old[position:])
        expected[path] = "".join(new).encode("utf-8")
        assert len(new) == member["after_line_count"]
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if member["operation"] == "modify":
            target.write_bytes("".join(old).encode("utf-8"))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    def git(*arguments):
        subprocess.run(["git", "-c", "core.autocrlf=false", *arguments], cwd=tmp_path,
                       capture_output=True, text=True, check=True, timeout=30, creationflags=flags)
    git("init", "--quiet")
    git("apply", "--check", "--whitespace=nowarn", str(patch))
    git("apply", "--whitespace=nowarn", str(patch))
    for path, raw in expected.items():
        assert (tmp_path / path).read_bytes() == raw
        if indexed[path]["operation"] == "add":
            assert sha256(raw).hexdigest() == indexed[path]["after_normalized_lf_sha256"]
            final_raw = raw.replace(b"\n", b"\r\n") if indexed[path]["final_newline_encoding"] == "CRLF" else raw
            if indexed[path]["final_newline_encoding"] == "MIXED":
                crlf_lines = set(indexed[path]["final_crlf_line_numbers"])
                final_raw = b"".join(line[:-1] + b"\r\n" if index in crlf_lines else line
                                     for index, line in enumerate(raw.splitlines(keepends=True), 1))
            assert sha256(final_raw).hexdigest() == indexed[path]["after_sha256"]


def main():
    """Run in an isolated publication candidate without pytest or conftest."""
    check_root = ROOT / "scratch/stats-intraday-patch-package"
    check_root.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="apply-", dir=check_root)).resolve()
    if not fixture.is_relative_to(check_root.resolve()):
        raise ValueError("Patch fixture must remain in the task-owned scratch directory")
    test_manifest_binds_exact_owned_members_and_operations()
    test_git_applies_exact_hunks_without_unrelated_source(fixture)
    print("Gameplan Stats intraday patch: digest, scope and Git application checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
