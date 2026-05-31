"""Path validation for the Personal Memory Wiki.

Every tool that accepts or builds a filesystem path must route it through
``resolve_under_root``. This is the single security primitive that guarantees
the server never reads or writes outside the wiki root, and never touches the
``long_term/private/`` area (which is invisible to all system components by
design).
"""

from __future__ import annotations

import os
from pathlib import Path


class WikiPathError(Exception):
    """Raised when a requested path escapes the wiki root or targets a
    forbidden area (``long_term/private/``)."""


def wiki_root() -> Path:
    """The absolute, resolved root of the wiki. Configurable via ``WIKI_ROOT``
    so the code stays path-agnostic across environments and a future repo move.
    """
    return Path(os.environ.get("WIKI_ROOT", "/srv/wiki")).resolve()


def resolve_under_root(relative: str) -> Path:
    """Resolve ``relative`` against the wiki root and guarantee the result
    stays inside the root and outside ``long_term/private/``.

    Raises:
        WikiPathError: if the resolved path escapes the root or lands in the
            private area.
    """
    root = wiki_root()
    candidate = (root / relative).resolve()

    if not candidate.is_relative_to(root):
        raise WikiPathError(f"Path escapes the wiki root: {relative!r}")

    private = (root / "long_term" / "private").resolve()
    if candidate.is_relative_to(private):
        raise WikiPathError("The long_term/private/ area is not accessible.")

    return candidate
