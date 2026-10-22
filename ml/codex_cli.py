"""Resolve an installed Codex CLI without changing frozen local configuration."""
from __future__ import annotations

from os import PathLike
from pathlib import Path
import shutil
from typing import Callable


def resolve_codex_executable(
    configured: str | PathLike[str] | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Prefer the configured file, then an existing ``codex`` file on PATH.

    Desktop updates can remove a versioned installation directory while the
    current executable remains discoverable on PATH. Resolution changes no
    saved configuration, workflow identity or review evidence and launches
    nothing. ``which`` permits callers to retain their existing lookup binding.
    """
    if configured and Path(configured).is_file():
        return str(configured)
    discovered = (which if which is not None else shutil.which)("codex")
    if discovered and Path(discovered).is_file():
        return str(discovered)
    return None
