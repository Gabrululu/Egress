"""File walking, rule matching and scoring."""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from . import pyast
from .rules import RULES, SERVICE_PROFILES, SEVERITY_WEIGHT

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".terraform",
             ".pytest_cache", "output", "dist", "build"}
MAX_FILE_BYTES = 1_000_000
# Inline suppression, like `# noqa`: put `egress: ignore` in a comment on the line.
IGNORE_MARK = re.compile(r"egress:\s*ignore", re.IGNORECASE)

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


def iter_files(root: Path, extra_skip: set[str] | None = None):
    if root.is_file():
        yield root
        return
    skip = SKIP_DIRS | (extra_skip or set())
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in skip and (rel_dir / d).as_posix() not in skip)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def scan_file(path: Path, rel: str, portable_out: set[str] | None = None) -> list[dict]:
    """Findings for one file. Python files are parsed with `ast` for SDK clients/imports
    (multi-line calls, aliases, sessions); regex rules cover the rest and act as fallback.
    Files whose SDK clients all use a custom S3-compatible endpoint are added to `portable_out`."""
    kind = file_kind(path)
    if kind is None:
        return []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings, seen, skip_rules, string_lines = [], set(), set(), set()
    if kind == "python":
        res = pyast.analyze(text, rel)
        if res is not None:
            ast_findings, info = res
            findings.extend(ast_findings)
            skip_rules = pyast.AST_RULE_IDS
            string_lines = info["string_lines"]
            if info["portable_file"] and portable_out is not None:
                portable_out.add(rel)
        elif portable_out is not None and CUSTOM_ENDPOINT.search(text):
            portable_out.add(rel)  # unparsable file: coarse regex heuristic
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or _is_comment(line, kind) or lineno in string_lines or IGNORE_MARK.search(line):
            continue
        for rule in RULES:
            if rule.id in skip_rules or kind not in rule.kinds or not rule.pattern.search(line):
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
    if IGNORE_MARK.search(text):  # inline suppression also applies to AST findings
        lines = text.splitlines()
        findings = [f for f in findings if not IGNORE_MARK.search(lines[f["line"] - 1])]
    findings.sort(key=lambda f: (f["line"], f["rule"]))
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


# A boto3 client pointed at a non-AWS endpoint speaks the portable S3 API (SeaweedFS, Garage,
# MinIO, Ceph...). In such files, SDK/S3 findings are kept for transparency but not scored.
CUSTOM_ENDPOINT = re.compile(r"""endpoint_url\s*=\s*(?!['"][^'"]*amazonaws\.com)""")
PORTABLE_RULES = {"py-boto3-import", "py-s3-client", "py-s3-calls"}
MITIGATION_NOTE = " Mitigated: client uses a custom S3-compatible endpoint_url (portable S3 API)."


def _apply_mitigations(findings: list[dict], portable_files: set[str]) -> None:
    for f in findings:
        if "portable" in f:  # AST client finding: exact per-call decision
            f["mitigated"] = f.pop("portable")
        elif f["file"] in portable_files and f.get("rule") in PORTABLE_RULES:
            f["mitigated"] = True
    # A requirements file's boto3 is portable when every boto3 import in its directory tree is.
    for req in (f for f in findings if f.get("rule") == "req-boto3"):
        req_dir = str(Path(req["file"]).parent)
        scope = "" if req_dir == "." else req_dir + "/"
        sdk = [f for f in findings if f.get("rule") == "py-boto3-import" and f["file"].startswith(scope)]
        if sdk and all(f.get("mitigated") for f in sdk):
            req["mitigated"] = True
    for f in findings:
        if f.get("mitigated"):
            f["lock_in_reason"] += MITIGATION_NOTE
        else:
            f["mitigated"] = False


def scan(target: str | os.PathLike, exclude: set[str] | None = None,
         exclude_dirs: set[str] | None = None) -> dict:
    root = Path(target).resolve()
    if not root.exists():
        raise FileNotFoundError(f"path not found: {target}")
    base = root if root.is_dir() else root.parent
    exclude = exclude or set()
    findings, scanned, portable = [], 0, set()
    for path in iter_files(root, {d.strip("/") for d in exclude_dirs or set()}):
        if file_kind(path) is None or path.name in exclude:
            continue
        scanned += 1
        rel = str(path.relative_to(base))
        findings.extend(scan_file(path, rel, portable))
    _apply_mitigations(findings, portable)
    services = score_services([f for f in findings if not f["mitigated"]])
    overall = overall_score(services)
    return {
        "target": str(root),
        "scanned_files": scanned,
        "overall_score": overall,
        "rating": rating(overall),
        "services": services,
        "findings": findings,
        "mitigated": sum(f["mitigated"] for f in findings),
        "formula": FORMULA,
    }
