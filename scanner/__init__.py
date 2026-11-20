"""Egress scanner: rule-based detection of AWS lock-in + scoring."""
from .engine import scan

__version__ = "0.1.0"
__all__ = ["scan"]
