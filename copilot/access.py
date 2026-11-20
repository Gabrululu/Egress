"""Public demo mode: safe to share the UI URL with anyone.

EGRESS_PUBLIC=1 turns it on. Visitors can scan the bundled sample app, generate migrations
(offline templates or answers already in the LLM cache: they never spend LLM credits) into a
private per-session folder, explore and download everything. Actions that cost money, touch
the host or change the running stack (scanning other paths/URLs, new LLM calls, Deploy/Stop)
need the presenter key, passed once as `?key=...` in the URL.

The key comes from EGRESS_ADMIN_KEY or is generated into .cache/egress-admin-key (chmod 600).
"""
from __future__ import annotations

import hmac
import os
import secrets
import tempfile
from pathlib import Path

from .config import REPO_ROOT, load_dotenv

KEY_FILE = REPO_ROOT / ".cache" / "egress-admin-key"
SAMPLE_APP = REPO_ROOT / "sample_app"
VISITOR_ROOT = Path(tempfile.gettempdir()) / "egress-visitors"


def public_mode() -> bool:
    load_dotenv()
    return os.environ.get("EGRESS_PUBLIC", "").strip().lower() in ("1", "true", "yes", "on")


def admin_key(create: bool = True) -> str:
    """Presenter key: EGRESS_ADMIN_KEY, else a random key persisted in .cache/ (gitignored)."""
    load_dotenv()
    env = os.environ.get("EGRESS_ADMIN_KEY", "").strip()
    if env:
        return env
    try:
        if KEY_FILE.is_file():
            return KEY_FILE.read_text(encoding="utf-8").strip()
        if not create:
            return ""
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(16)
        KEY_FILE.write_text(key + "\n", encoding="utf-8")
        KEY_FILE.chmod(0o600)
        return key
    except OSError:  # read-only image (docker-run without EGRESS_ADMIN_KEY): no presenter key
        return ""


def is_presenter(supplied: str | None) -> bool:
    """Full access when not in public mode, or with the right key (constant-time compare)."""
    if not public_mode():
        return True
    key = admin_key()
    return bool(supplied) and bool(key) and hmac.compare_digest(str(supplied), key)


def visitor_source_allowed(src: str) -> bool:
    """Visitors may only scan the bundled sample app (no host paths, no clones)."""
    try:
        return Path(src.strip()).resolve() == SAMPLE_APP.resolve()
    except (OSError, ValueError):
        return False


def visitor_out_dir(session_id: str) -> Path:
    """Per-session output folder so visitors never overwrite the presenter's output/."""
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "anon"
    d = VISITOR_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def main() -> int:
    """Print the visitor and presenter URLs (Codespaces) for the public demo."""
    import ui_theme  # noqa: PLC0415 (repo root is on sys.path via -m from the repo)
    base = ui_theme.public_url(8501, "/")
    print(f"Visitors : {base}")
    print(f"Presenter: {base}?key={admin_key()}   (keep this one private)")
    print("\nThe UI must run with `make run-public` (EGRESS_PUBLIC=1) so visitors are sandboxed.")
    print("Close it again with `make private`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
