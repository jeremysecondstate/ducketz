"""Resolve an installed Codex CLI without changing frozen local configuration."""
from __future__ import annotations

from os import PathLike
from pathlib import Path
import re
import shutil
from typing import Callable


def _installed_sibling(configured: str | PathLike[str] | None) -> str | None:
    """Read only a recognized install's immediate version directories."""
    if not configured:
        return None
    path = Path(configured)
    version = re.compile(r"[0-9a-fA-F]{16}")
    if (not path.is_absolute() or ".." in path.parts
            or str(path).startswith(("\\\\", "//"))
            or path.name.casefold() != "codex.exe"
            or not version.fullmatch(path.parent.name)
            or path.parent.parent.name.casefold() != "bin"
            or path.parent.parent.parent.name.casefold() != "codex"
            or path.parent.parent.parent.parent.name.casefold() != "openai"):
        return None
    install = path.parent.parent
    try:
        boundary = install.resolve(strict=True)
        candidates = set()
        for sibling in install.iterdir():
            if not version.fullmatch(sibling.name) or not sibling.is_dir():
                continue
            executable = (sibling / "codex.exe").resolve()
            if (executable.name.casefold() != "codex.exe"
                    or not version.fullmatch(executable.parent.name)
                    or executable.parent.parent != boundary or not executable.is_file()):
                continue
            candidates.add(executable)
            if len(candidates) > 1:
                return None
        return str(next(iter(candidates))) if candidates else None
    except (OSError, RuntimeError):
        # Missing/inaccessible installs or a link loop are unavailable. Do not
        # rewrite the binding or guess between different installed executables.
        return None


def resolve_codex_executable(
    configured: str | PathLike[str] | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Prefer configured, then PATH, then one recognized installed sibling.

    Desktop updates can remove a versioned installation directory while the
    current executable remains installed. Scheduled tasks can lack the desktop
    app's PATH additions. A final shallow lookup accepts exactly one resolved
    executable in the configured OpenAI/Codex/bin installation, never choosing
    a version by name or recency. Resolution changes no saved configuration or
    review evidence and launches nothing. ``which`` preserves caller injection.
    """
    if configured and Path(configured).is_file():
        return str(configured)
    discovered = (which if which is not None else shutil.which)("codex")
    if discovered and Path(discovered).is_file():
        return str(discovered)
    return _installed_sibling(configured)
