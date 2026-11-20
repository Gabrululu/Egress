"""Migration generator: LLM first, per-part fallback to built-in templates."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

from . import fallback
from .config import REPO_ROOT, LLMConfig, load_config
from .llm import SYSTEM_PROMPT, LLMError, build_user_prompt, chat_json
from .apply import build_app_from_source
from .datamig import generate_data_migration
from .policy import check_compose_file

# Overridable so a containerised Egress can write to a host path mounted at the same location
# (compose bind mounts are resolved by the host Docker daemon).
DEFAULT_OUT = Path(os.environ.get("EGRESS_OUTPUT_DIR") or REPO_ROOT / "output")
CACHE_DIR = Path(os.environ.get("EGRESS_CACHE_DIR") or REPO_ROOT / ".cache" / "llm")
CACHE_VERSION = "v4"  # bump when prompt/contract semantics change
REPAIR_PROMPT = (
    "Your docker_compose was rejected by automated checks:\n{problem}\n\n"
    "Return the COMPLETE JSON object again (all four keys). Fix docker_compose so it passes "
    "`docker compose config`, follows the contract in the instructions exactly, and uses no "
    "privileged mode, host mounts outside the project, docker.sock or non-allowlisted images."
)
REQUIRED_SERVICES = {"postgres", "s3", "rabbitmq", "api", "worker"}
REQUIRED_APP_ENV = {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "AMQP_URL",
                    "QUEUE_NAME", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"}
GENERATED = ["app", ".app-src.tmp", "data-migration", "seaweedfs", "diffs", "docker-compose.yml", "docker-compose.llm.yml",
             "mapping.json", "mapping.md", "migration.diff", "MIGRATION_PLAN.md", "result.json", ".deploy.log"]


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


# --------------------------------------------------------------- secrets
SECRET_KEYS = ("DB_PASSWORD", "RABBITMQ_PASSWORD", "S3_SECRET_KEY")
LEGACY_DB_PASSWORD = "egress-demo"  # default used before random secrets existed


def _read_env(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def _volume_exists(name: str) -> bool:
    try:
        return subprocess.run(["docker", "volume", "inspect", name], capture_output=True,
                              timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_secrets(out: Path) -> dict:
    """Create/reuse output/.env with random credentials and render seaweedfs/s3.json with them.

    Existing values are kept so a regenerated stack can still open its Postgres volume.
    Returns metadata (never the secret values)."""
    env_path = out / ".env"
    env = _read_env(env_path)
    created = [k for k in SECRET_KEYS if not env.get(k)]
    for k in created:
        env[k] = secrets.token_urlsafe(18)  # URL-safe: valid inside AMQP_URL
    legacy = False
    if "DB_PASSWORD" in created and _volume_exists("egress-migrated_pgdata"):
        env["DB_PASSWORD"], legacy = LEGACY_DB_PASSWORD, True  # data volume initialised with old default
    if created:
        lines = ["# Generated by Egress: local demo credentials. Never commit this file."]
        lines += [f"{k}={env[k]}" for k in SECRET_KEYS] + [f"{k}={v}" for k, v in env.items() if k not in SECRET_KEYS]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    s3_cfg = json.loads((fallback.TEMPLATES / "seaweedfs" / "s3.json").read_text(encoding="utf-8"))
    for ident in s3_cfg.get("identities", []):
        for cred in ident.get("credentials", []):
            cred["secretKey"] = env["S3_SECRET_KEY"]
    (out / "seaweedfs").mkdir(exist_ok=True)
    (out / "seaweedfs" / "s3.json").write_text(json.dumps(s3_cfg, indent=2), encoding="utf-8")
    generated = [k for k in created if not (legacy and k == "DB_PASSWORD")]
    return {"env_file": str(env_path), "created": generated,
            "reused": [k for k in SECRET_KEYS if k not in generated], "legacy_db_password": legacy}


def after_scan(out: Path) -> dict:
    """Re-scan the migrated stack (app code + final compose) to measure remaining lock-in."""
    from scanner import scan
    # data-migration/ talks to AWS on purpose (one-off source side), it is not part of the stack
    r = scan(out, exclude={"docker-compose.llm.yml"}, exclude_dirs={"data-migration"})
    return {"overall_score": r["overall_score"], "rating": r["rating"],
            "services": {k: v["score"] for k, v in r["services"].items()},
            "findings": len(r["findings"]) - r["mitigated"], "mitigated": r["mitigated"]}


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


def compose_problem(text, out: Path) -> tuple[str | None, dict | None]:
    """Write a candidate compose and run all gates. Returns (problem or None, validation)."""
    if not isinstance(text, str) or not text.strip():
        return "no docker_compose returned", None
    cand = out / "docker-compose.llm.yml"
    cand.write_text(text.strip() + "\n", encoding="utf-8")
    v = validate_compose(cand)
    if not v["ok"]:
        return f"validation ({v['method']}): {v['message']}", v
    try:
        contract = check_contract(cand)
    except Exception as e:  # malformed structure
        contract = f"contract check error: {e}"
    if contract:
        return f"contract: {contract}", v
    violations = check_compose_file(cand)
    if violations:
        return "security policy: " + "; ".join(violations[:6]), v
    return None, v


def _cache_file(cache_dir: Path, cfg: LLMConfig, user_prompt: str) -> Path:
    key = hashlib.sha256(json.dumps([CACHE_VERSION, cfg.base_url, cfg.model, SYSTEM_PROMPT, user_prompt])
                         .encode("utf-8")).hexdigest()[:32]
    return cache_dir / f"{key}.json"


def _call_llm(scan_report: dict, target: Path, out: Path, cfg: LLMConfig, use_cache: bool,
              cache_dir: Path, errors: list[str], meta: dict, include_files: list[str] | None = None) -> dict:
    """LLM call with response cache and one repair round for the compose part."""
    user_prompt = build_user_prompt(scan_report, target, include_files)
    cache_file = _cache_file(cache_dir, cfg, user_prompt)
    if use_cache and cache_file.is_file():
        try:
            meta["cache"] = "hit"
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta["cache"] = "corrupt"
    meta["cache"] = meta.get("cache") or ("miss" if use_cache else "off")

    try:
        data = chat_json(SYSTEM_PROMPT, user_prompt, cfg)
        meta["calls"] += 1
    except LLMError as e:
        errors.append(str(e))
        return {}

    problem, _ = compose_problem(data.get("docker_compose"), out)
    if problem:
        meta["repair"] = "attempted"
        history = [{"role": "assistant", "content": json.dumps(data)},
                   {"role": "user", "content": REPAIR_PROMPT.format(problem=problem[:1500])}]
        try:
            fixed = chat_json(SYSTEM_PROMPT, user_prompt, cfg, history=history)
            meta["calls"] += 1
            p2, _ = compose_problem(fixed.get("docker_compose"), out)
            if p2 is None:
                meta["repair"] = "fixed"
                errors.append(f"LLM compose rejected ({problem[:200]}); repaired on 2nd attempt")
                data["docker_compose"] = fixed["docker_compose"]
                # adopt other repaired parts only where the first answer was unusable
                for key, ok in (("mapping", _valid_mapping), ("diffs", _valid_diffs), ("plan", _valid_plan)):
                    if ok(data.get(key)) is None and ok(fixed.get(key)) is not None:
                        data[key] = fixed[key]
            else:
                meta["repair"] = "failed"
        except LLMError as e:
            meta["repair"] = "error"
            errors.append(f"repair call failed: {e}")

    final_problem, _ = compose_problem(data.get("docker_compose"), out)
    if use_cache and final_problem is None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    return data


# --------------------------------------------------------------- main entry
def generate(target: str | Path, scan_report: dict | None = None, out_dir: str | Path = DEFAULT_OUT,
             use_llm: bool = True, cfg: LLMConfig | None = None, use_cache: bool = True,
             cache_dir: str | Path | None = None, include_files: list[str] | None = None) -> dict:
    target = Path(target).resolve()
    out = Path(out_dir).resolve()
    if scan_report is None:
        from scanner import scan
        scan_report = scan(target)
    cfg = cfg or load_config()
    services = list(scan_report.get("services", {}))
    errors: list[str] = []
    meta = {"cache": None, "repair": None, "calls": 0}

    _reset_output(out)
    # Runnable reference implementation + storage config (always from templates).
    shutil.copytree(fallback.TEMPLATES / "app", out / "app")
    secrets_meta = ensure_secrets(out)
    (out / "scan.json").write_text(json.dumps(scan_report, indent=2), encoding="utf-8")

    llm_data: dict = {}
    if use_llm and cfg.enabled:
        llm_data = _call_llm(scan_report, target, out, cfg, use_cache,
                             Path(cache_dir) if cache_dir else CACHE_DIR, errors, meta, include_files)
    elif use_llm:
        errors.append("LLM disabled: LLM_API_KEY not set (using offline templates)")
    else:
        errors.append("LLM skipped: offline mode")

    sources = {}

    # (a) mapping
    mapping = _valid_mapping(llm_data.get("mapping")) if llm_data else None
    if llm_data and mapping is None:
        errors.append("LLM mapping invalid")
    sources["mapping"] = "llm" if mapping else "fallback"
    mapping = mapping or fallback.mapping(services)

    # (b) docker-compose: validation + contract + security policy, else template
    compose_path = out / "docker-compose.yml"
    validation = None
    if llm_data:
        problem, v = compose_problem(llm_data.get("docker_compose"), out)
        if problem is None:
            shutil.copyfile(out / "docker-compose.llm.yml", compose_path)
            validation, sources["compose"] = v, "llm"
        else:
            errors.append(f"LLM compose rejected: {problem}")
    if sources.get("compose") != "llm":
        compose_path.write_text(fallback.compose_text(), encoding="utf-8")
        validation = validate_compose(compose_path)
        violations = check_compose_file(compose_path)
        if violations:  # should never happen for the shipped template
            validation = {"ok": False, "method": "security policy", "message": "; ".join(violations)}
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

    # Runnable app: the scanned repo + diffs when they apply and verify; else the template.
    source_patch = build_app_from_source(target, diffs, out / "app")
    data_migration = generate_data_migration(target, scan_report, out)

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
        "llm": {**cfg.public(), **meta},
        "compose_validation": validation,
        "mapping": mapping,
        "compose": compose_path.read_text(encoding="utf-8"),
        "diffs": diffs,
        "plan": plan,
        "out_dir": str(out),
        "secrets": secrets_meta,
        "source_patch": source_patch,
        "data_migration": data_migration,
        "after": after_scan(out),
        "files": sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()),
    }
    (out / "result.json").write_text(json.dumps({k: v for k, v in result.items() if k != "compose"},
                                                indent=2), encoding="utf-8")
    return result
