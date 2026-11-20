"""Resolve a scan source: local path or git URL (shallow clone to a temp dir)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

GIT_URL = re.compile(r"^(https://[\w.-]+/[\w./~-]+?|git@[\w.-]+:[\w./~-]+?)(\.git)?/?$")
CLONE_PREFIX = "egress-clone-"


class SourceError(Exception):
    pass


def is_git_url(s: str) -> bool:
    return bool(GIT_URL.match(s.strip()))


def resolve_source(src: str, timeout: int = 90) -> tuple[Path, Path | None]:
    """Return (path_to_scan, temp_dir_to_cleanup_or_None)."""
    src = (src or "").strip()
    if not src:
        raise SourceError("Enter a local path or a git URL.")
    if is_git_url(src):
        tmp = Path(tempfile.mkdtemp(prefix=CLONE_PREFIX))
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            r = subprocess.run(["git", "clone", "--depth", "1", "--single-branch", src, str(tmp / "repo")],
                               capture_output=True, text=True, timeout=timeout, env=env)
        except FileNotFoundError:
            cleanup(tmp)
            raise SourceError("git is not installed.")
        except subprocess.TimeoutExpired:
            cleanup(tmp)
            raise SourceError(f"git clone timed out after {timeout}s.")
        if r.returncode != 0:
            cleanup(tmp)
            msg = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise SourceError(f"git clone failed: {msg}")
        return tmp / "repo", tmp
    if "://" in src or src.startswith("git@"):
        raise SourceError("Unsupported URL. Use https://host/owner/repo(.git) or git@host:owner/repo.git")
    p = Path(src).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p)
    if not p.exists():
        raise SourceError(f"Path not found: {src}")
    return p.resolve(), None


def cleanup(tmp: Path | None) -> None:
    """Remove a temp clone dir (only dirs we created)."""
    if tmp and Path(tmp).name.startswith(CLONE_PREFIX) and Path(tmp).parent == Path(tempfile.gettempdir()):
        shutil.rmtree(tmp, ignore_errors=True)
