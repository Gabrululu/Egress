"""File walking, rule matching and scoring."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .rules import RULES, SERVICE_PROFILES, SEVERITY_WEIGHT

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".terraform",
             ".pytest_cache", "output", "dist", "build"}
MAX_FILE_BYTES = 1_000_000

WEIGHTS = {"api": 0.5, "data": 0.3, "egress": 0.2}
FORMULA = (
    "service_score = (0.5*api + 0.3*data + 0.2*egress) * depth; "
    "depth = min(1, sum(severity_weight)) with low=0.3, medium=0.6, high=1.0; "
    "overall = round(0.5*max(service_scores) + 0.5*mean(service_scores))"
)


def file_kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".tf"):
        return "terraform"
    if name.endswith(".py"):
        return "python"
    if fnmatch.fnmatch(name, "requirements*.txt"):
        return "requirements"
    if fnmatch.fnmatch(name, "*compose*.y*ml"):
        return "compose"
    return None


def _is_comment(line: str, kind: str) -> bool:
    s = line.lstrip()
    if kind == "terraform":
        return s.startswith(("#", "//"))
    return s.startswith("#")


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def scan_file(path: Path, rel: str) -> list[dict]:
    kind = file_kind(path)
    if kind is None:
        return []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings, seen = [], set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or _is_comment(line, kind):
            continue
        for rule in RULES:
            if kind not in rule.kinds or not rule.pattern.search(line):
                continue
            key = (rule.id, lineno)
            if key in seen:
                continue
            seen.add(key)
            findings.append({
                "service": rule.service,
                "file": rel,
                "line": lineno,
                "evidence": line.strip()[:200],
                "lock_in_reason": rule.reason,
                "severity": rule.severity,
                "rule": rule.id,
            })
    return findings


def score_services(findings: list[dict]) -> dict[str, dict]:
    services: dict[str, dict] = {}
    for f in findings:
        services.setdefault(f["service"], []).append(f)
    out = {}
    for svc, items in services.items():
        p = SERVICE_PROFILES[svc]
        base = sum(WEIGHTS[d] * p[d] for d in WEIGHTS)
        depth = min(1.0, sum(SEVERITY_WEIGHT[f["severity"]] for f in items))
        out[svc] = {
            "score": round(base * depth),
            "findings": len(items),
            "max_severity": max((f["severity"] for f in items), key=SEVERITY_WEIGHT.get),
            "dimensions": {d: p[d] for d in WEIGHTS},
            "depth": round(depth, 2),
            "alternative": p["alternative"],
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["score"]))


def overall_score(services: dict[str, dict]) -> int:
    scores = [s["score"] for s in services.values()]
    if not scores:
        return 0
    return round(0.5 * max(scores) + 0.5 * sum(scores) / len(scores))


def rating(score: int) -> str:
    return "high" if score >= 70 else "medium" if score >= 40 else "low"


def scan(target: str | os.PathLike) -> dict:
    root = Path(target).resolve()
    if not root.exists():
        raise FileNotFoundError(f"path not found: {target}")
    base = root if root.is_dir() else root.parent
    findings, scanned = [], 0
    for path in iter_files(root):
        if file_kind(path) is None:
            continue
        scanned += 1
        findings.extend(scan_file(path, str(path.relative_to(base))))
    services = score_services(findings)
    overall = overall_score(services)
    return {
        "target": str(root),
        "scanned_files": scanned,
        "overall_score": overall,
        "rating": rating(overall),
        "services": services,
        "findings": findings,
        "formula": FORMULA,
    }
