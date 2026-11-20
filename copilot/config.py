"""LLM configuration from env vars (optionally loaded from a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader; never overrides variables already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class LLMConfig:
    base_url: str = "https://api.mistral.ai/v1"
    api_key: str = ""
    model: str = "mistral-large-latest"
    timeout: float = 60.0

    @property
    def is_local(self) -> bool:
        return any(h in self.base_url for h in ("localhost", "127.0.0.1", "ollama", "host.docker.internal"))

    @property
    def enabled(self) -> bool:
        # Local servers (Ollama, vLLM) usually need no key.
        return bool(self.api_key) or self.is_local

    def public(self) -> dict:
        return {"base_url": self.base_url, "model": self.model, "enabled": self.enabled}


def load_config() -> LLMConfig:
    load_dotenv()
    d = LLMConfig()
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", d.timeout))
    except ValueError:
        timeout = d.timeout
    return LLMConfig(
        base_url=os.environ.get("LLM_BASE_URL", "").strip() or d.base_url,
        api_key=os.environ.get("LLM_API_KEY", "").strip(),
        model=os.environ.get("LLM_MODEL", "").strip() or d.model,
        timeout=timeout,
    )
