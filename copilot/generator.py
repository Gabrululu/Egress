"""Migration generator: LLM first, per-part fallback to built-in templates."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from . import fallback
from .config import REPO_ROOT, LLMConfig, load_config
from .llm import SYSTEM_PROMPT, LLMError, build_user_prompt, chat_json

DEFAULT_OUT = REPO_ROOT / "output"
REQUIRED_SERVICES = {"postgres", "s3", "rabbitmq", "api", "worker"}
REQUIRED_APP_ENV = {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "AMQP_URL",
                    "QUEUE_NAME", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"}
GENERATED = ["app", "seaweedfs", "diffs", "docker-compose.yml", "docker-compose.llm.yml",
             "mapping.json", "mapping.md", "migration.diff", "MIGRATION_PLAN.md", "result.json"]


# --------------------------------------------------------------- validation
def validate_compose(path: Path) -> dict:
    """Validate with `docker compose config`; fall back to YAML parse if docker is missing."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return {"ok": False, "method": "yaml", "message": f"invalid YAML: {str(e)[:200]}"}
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        return {"ok": False, "method": "yaml", "message": "no 'services' mapping"}
    try:
        r = subprocess.run(["docker", "compose", "-f", str(path), "config", "-q"],
                           cwd=path.parent, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return {"ok": True, "method": "yaml (docker not found)", "message": "YAML structure OK"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "docker compose config", "message": "timeout"}
    if r.returncode != 0:
        return {"ok": False, "method": "docker compose config", "message": (r.stderr or r.stdout).strip()[:500]}
    return {"ok": True, "method": "docker compose config", "message": "valid"}


def check_contract(path: Path) -> str | None:
    """Ensure an LLM compose matches what the generated app/ code expects. Returns error or None."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    svcs = doc.get("services", {})
    missing = REQUIRED_SERVICES - set(svcs)
    if missing:
        return f"missing services: {sorted(missing)}"
    for name in ("api", "worker"):
        env = svcs[name].get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
        miss = REQUIRED_APP_ENV - set(env)
        if miss:
            return f"{name} missing env vars: {sorted(miss)}"
    text = json.dumps(doc)  # parsed content only, comments ignored
    if "awslogs" in text or "amazonaws.com" in text:
        return "compose still references AWS"
    return None


def _valid_mapping(m) -> list[dict] | None:
    if not isinstance(m, list):
        return None
    rows = [{"aws": str(r.get("aws", "")), "alternative": str(r.get("alternative", "")),
             "reason": str(r.get("reason", ""))}
            for r in m if isinstance(r, dict) and r.get("aws") and r.get("alternative")]
    return rows or None


def _valid_diffs(d) -> list[dict] | None:
    if not isinstance(d, list):
        return None
    rows = [{"file": str(x.get("file", "patch")), "diff": x["diff"].rstrip() + "\n"}
            for x in d if isinstance(x, dict) and isinstance(x.get("diff"), str)
            and "@@" in x["diff"] and ("---" in x["diff"] or "+++" in x["diff"])]
    return rows or None


def _valid_plan(p) -> str | None:
    if isinstance(p, dict):
        p = "\n\n".join(f"## {k}\n{v if isinstance(v, str) else json.dumps(v, indent=2)}" for k, v in p.items())
    return p if isinstance(p, str) and len(p.strip()) >= 80 else None


# --------------------------------------------------------------- writing
def _reset_output(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in GENERATED:
        p = out / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


def _mapping_md(rows: list[dict]) -> str:
    lines = ["| AWS | Alternative | Why |", "|---|---|---|"]
    lines += [f"| {r['aws']} | {r['alternative']} | {r['reason']} |" for r in rows]
    return "\n".join(lines) + "\n"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name.strip("/"))[:60] or "patch"


# --------------------------------------------------------------- main entry
def generate(target: str | Path, scan_report: dict | None = None, out_dir: str | Path = DEFAULT_OUT,
             use_llm: bool = True, cfg: LLMConfig | None = None) -> dict:
    target = Path(target).resolve()
    out = Path(out_dir).resolve()
    if scan_report is None:
        from scanner import scan
        scan_report = scan(target)
    cfg = cfg or load_config()
    services = list(scan_report.get("services", {}))
    errors: list[str] = []

    _reset_output(out)
    # Runnable reference implementation + storage config (always from templates).
    shutil.copytree(fallback.TEMPLATES / "app", out / "app")
    shutil.copytree(fallback.TEMPLATES / "seaweedfs", out / "seaweedfs")
    (out / "scan.json").write_text(json.dumps(scan_report, indent=2), encoding="utf-8")

    llm_data: dict = {}
    if use_llm and cfg.enabled:
        try:
            llm_data = chat_json(SYSTEM_PROMPT, build_user_prompt(scan_report, target), cfg)
        except LLMError as e:
            errors.append(str(e))
    elif use_llm:
        errors.append("LLM disabled: LLM_API_KEY not set (using offline templates)")
    else:
        errors.append("LLM skipped by request (--no-llm)")

    sources = {}

    # (a) mapping
    mapping = _valid_mapping(llm_data.get("mapping")) if llm_data else None
    if llm_data and mapping is None:
        errors.append("LLM mapping invalid")
    sources["mapping"] = "llm" if mapping else "fallback"
    mapping = mapping or fallback.mapping(services)

    # (b) docker-compose
    compose_path = out / "docker-compose.yml"
    validation = None
    llm_compose = llm_data.get("docker_compose") if llm_data else None
    if isinstance(llm_compose, str) and llm_compose.strip():
        cand = out / "docker-compose.llm.yml"
        cand.write_text(llm_compose.strip() + "\n", encoding="utf-8")
        v = validate_compose(cand)
        problem = None if v["ok"] else f"LLM compose failed validation: {v['message']}"
        if problem is None:
            try:
                problem = check_contract(cand)
                if problem:
                    problem = f"LLM compose contract: {problem}"
            except Exception as e:
                problem = f"LLM compose contract check error: {e}"
        if problem is None:
            shutil.copyfile(cand, compose_path)
            validation, sources["compose"] = v, "llm"
        else:
            errors.append(problem)
    elif llm_data:
        errors.append("LLM returned no docker_compose")
    if sources.get("compose") != "llm":
        compose_path.write_text(fallback.compose_text(), encoding="utf-8")
        validation = validate_compose(compose_path)
        sources["compose"] = "fallback"

    # (c) diffs
    diffs = _valid_diffs(llm_data.get("diffs")) if llm_data else None
    if llm_data and diffs is None:
        errors.append("LLM diffs invalid")
    sources["diffs"] = "llm" if diffs else "fallback"
    diffs = diffs or fallback.diffs(target)

    # (d) plan
    plan = _valid_plan(llm_data.get("plan")) if llm_data else None
    if llm_data and plan is None:
        errors.append("LLM plan invalid")
    sources["plan"] = "llm" if plan else "fallback"
    plan = plan or fallback.plan(scan_report)

    # write artifacts
    (out / "mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    (out / "mapping.md").write_text(_mapping_md(mapping), encoding="utf-8")
    (out / "diffs").mkdir()
    for i, d in enumerate(diffs, 1):
        (out / "diffs" / f"{i:02d}_{_safe_name(d['file'])}.diff").write_text(d["diff"], encoding="utf-8")
    (out / "migration.diff").write_text("".join(d["diff"] for d in diffs), encoding="utf-8")
    (out / "MIGRATION_PLAN.md").write_text(plan, encoding="utf-8")

    used = set(sources.values())
    path = "llm" if used == {"llm"} else "fallback" if used == {"fallback"} else "mixed"
    result = {
        "path": path,
        "sources": sources,
        "errors": errors,
        "llm": cfg.public(),
        "compose_validation": validation,
        "mapping": mapping,
        "compose": compose_path.read_text(encoding="utf-8"),
        "diffs": diffs,
        "plan": plan,
        "out_dir": str(out),
        "files": sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()),
    }
    (out / "result.json").write_text(json.dumps({k: v for k, v in result.items() if k != "compose"},
                                                indent=2), encoding="utf-8")
    return result
