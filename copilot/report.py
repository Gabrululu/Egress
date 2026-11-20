"""Downloadable artifacts: a Markdown report and a zip of the generated migration."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Never shipped in the zip: secrets, logs, rejected LLM candidates.
ZIP_EXCLUDE = {".env", ".deploy.log", "docker-compose.llm.yml"}


def _md_cell(v) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def build_report_md(scan: dict, migration: dict | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    target = Path(scan.get("target", "")).name or scan.get("target", "")
    lines = [f"# Egress lock-in report: `{target}`", "", f"_Generated {now} by Egress (Apache 2.0)._", ""]

    lines += ["## Summary", ""]
    lines.append(f"- **Lock-in score (before):** {scan['overall_score']}/100 ({scan['rating']})")
    if migration and migration.get("after"):
        a = migration["after"]
        lines.append(f"- **Lock-in score (after migration):** {a['overall_score']}/100 ({a['rating']}), "
                     f"{a['findings']} findings left, {a['mitigated']} mitigated")
    active = [f for f in scan["findings"] if not f.get("mitigated")]
    lines.append(f"- **Findings:** {len(active)} across {len(scan['services'])} services "
                 f"({scan.get('mitigated', 0)} mitigated) in {scan['scanned_files']} files")
    if migration:
        v = migration.get("compose_validation") or {}
        lines.append(f"- **Generation path:** {migration['path']} "
                     f"({', '.join(f'{k}: {s}' for k, s in migration['sources'].items())})")
        lines.append(f"- **Compose validation:** {'valid' if v.get('ok') else 'invalid'} ({v.get('method', '-')})")
    lines.append("")

    lines += ["## Services", "", "| Service | Score | Findings | Max severity | Alternative |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {s['score']} | {s['findings']} | {s['max_severity']} | {_md_cell(s['alternative'])} |"
              for k, s in scan["services"].items()]
    lines.append("")

    lines += ["## Findings", "", "| Severity | Service | Location | Evidence | Why it locks you in |",
              "|---|---|---|---|---|"]
    order = {"high": 0, "medium": 1, "low": 2}
    for f in sorted(active, key=lambda f: (order.get(f["severity"], 3), f["service"], f["file"], f["line"])):
        lines.append(f"| {f['severity']} | {f['service']} | `{f['file']}:{f['line']}` | "
                     f"`{_md_cell(f['evidence'])[:120]}` | {_md_cell(f['lock_in_reason'])} |")
    lines.append("")

    if migration:
        lines += ["## Migration mapping", "", "| AWS | Alternative | Why |", "|---|---|---|"]
        lines += [f"| {_md_cell(m['aws'])} | {_md_cell(m['alternative'])} | {_md_cell(m['reason'])} |"
                  for m in migration["mapping"]]
        lines += ["", "## Migration plan", "", migration["plan"].strip(), ""]
        lines += ["## Code changes", "",
                  f"{len(migration['diffs'])} unified diffs are included in `migration.diff` "
                  "inside the migration zip.", ""]
    lines += ["## Scoring formula", "", f"`{scan['formula']}`", ""]
    return "\n".join(lines)


def build_zip(out_dir: Path, report_md: str | None = None) -> bytes:
    """Zip output/ (minus secrets/logs) under an `egress-migration/` folder."""
    out_dir = Path(out_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_dir.rglob("*")):
            rel = p.relative_to(out_dir)
            if not p.is_file() or rel.name in ZIP_EXCLUDE or rel.name == ".gitkeep":
                continue
            if rel.as_posix() == "seaweedfs/s3.json":  # holds the generated S3 secret
                z.writestr(f"egress-migration/{rel.as_posix()}", _scrub_s3_config(p))
                continue
            z.write(p, f"egress-migration/{rel.as_posix()}")
        z.writestr("egress-migration/.env.example",
                   "# Copy to .env and set your own values.\n"
                   "# S3_SECRET_KEY must match secretKey in seaweedfs/s3.json.\n"
                   "DB_PASSWORD=\nRABBITMQ_PASSWORD=\nS3_SECRET_KEY=CHANGE_ME\n")
        if report_md:
            z.writestr("egress-migration/REPORT.md", report_md)
    return buf.getvalue()


def _scrub_s3_config(path: Path) -> str:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "{}"
    for ident in cfg.get("identities", []):
        for cred in ident.get("credentials", []):
            cred["secretKey"] = "CHANGE_ME"
    return json.dumps(cfg, indent=2)
