"""Egress copilot: LLM migration generator with offline template fallback."""
from .generator import generate, validate_compose

__all__ = ["generate", "validate_compose"]
