"""Offline Markdown hygiene check for explicitly selected documentation files.

Checks UTF-8 readability, whitespace, fenced blocks, and repository-relative
link syntax. It neither imports application code nor evaluates trading behavior.
"""
from pathlib import Path
import re
import sys


def main(paths):
    if not paths:
        raise SystemExit("Provide repository-relative Markdown paths")
    root = Path.cwd().resolve()
    errors = []
    for name in paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.suffix != ".md":
            errors.append(f"{name}: expected a Markdown path within the repository")
            continue
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        if content.startswith("\ufeff"):
            errors.append(f"{name}: unexpected UTF-8 BOM")
        if not content.endswith("\n"):
            errors.append(f"{name}: missing final newline")
        fence = None
        for number, line in enumerate(content.splitlines(), 1):
            if line.rstrip() != line:
                errors.append(f"{name}:{number}: trailing whitespace")
            if "\t" in line:
                errors.append(f"{name}:{number}: tab character")
            match = re.match(r"^\s*(`{3,}|~{3,})", line)
            if match:
                marker = match.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
        if fence is not None:
            errors.append(f"{name}: unclosed fenced block")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            target = target.strip("<>")
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                if not target.startswith(("https://", "http://", "mailto:")):
                    errors.append(f"{name}: nonportable link {target}")
                continue
            local = target.split("#", 1)[0]
            if local and not (path.parent / local).resolve().is_relative_to(root):
                errors.append(f"{name}: link escapes repository: {target}")
        print(f"Checked {name}: {len(content.splitlines())} lines")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Markdown hygiene checks passed; linked content and architecture require review.")


if __name__ == "__main__":
    main(sys.argv[1:])
