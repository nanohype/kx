"""Directories a gate writes while it runs, removed when it is done with them."""

from __future__ import annotations

import contextlib
import pathlib
import subprocess
import tempfile
from collections.abc import Iterator


@contextlib.contextmanager
def directory(prefix: str) -> Iterator[pathlib.Path]:
    """A temporary directory, removed when the block ends, however it ends."""
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as d:
        yield pathlib.Path(d)


@contextlib.contextmanager
def tree(files: dict[str, str], *, prefix: str = "kx-tree-", git: bool = False,
         untracked: str | None = None) -> Iterator[pathlib.Path]:
    """A temporary directory holding `files`, by path relative to its root.

    With `git`, the directory is a repository with every file staged, so a check
    that reads `git ls-files` sees the fixture the way it sees the real tree.
    Staging is what puts a file in that set; no commit is needed. A key starting
    with `untracked` is written without the prefix and left unstaged. git's exit
    status is not checked, so a caller that needs the repository confirms git
    is present first.
    """
    with directory(prefix) as root:
        staged = []
        for key, body in files.items():
            unstaged = untracked is not None and key.startswith(untracked)
            rel = key[len(untracked):] if unstaged else key
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            if not unstaged:
                staged.append(rel)
        if git:
            subprocess.run(["git", "init", "-q", str(root)], check=False, capture_output=True,
                           timeout=60)
            if staged:
                subprocess.run(["git", "-C", str(root), "add", "--", *staged], check=False,
                               capture_output=True, timeout=60)
        yield root
