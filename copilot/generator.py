"""Migration generator: LLM first, per-part fallback to built-in templates."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

from . import fallback
from .config import REPO_ROOT, LLMConfig, load_config
from .llm import SYSTEM_PROMPT, LLMError, build_user_prompt, chat_json
from .apply import build_app_from_source
from .codegen import code_problem, files_to_diffs
from .datamig import generate_data_migration
from .policy import check_compose_file

# Overridable so a containerised Egress can write to a host path mounted at the same location
# (compose bind mounts are resolved by the host Docker daemon).
DEFAULT_OUT = Path(os.environ.get("EGRESS_OUTPUT_DIR") or REPO_ROOT / "output")
CACHE_DIR = Path(os.environ.get("EGRESS_CACHE_DIR") or REPO_ROOT / ".cache" / "llm")
CACHE_VERSION = "v6"  # bump when prompt/contract semantics change
REPAIR_PROMPT = (
    "Your answer was rejected by automated checks:\n{problem}\n\n"
    "Return the COMPLETE JSON object again (all four keys), fixing what failed. docker_compose must "
    "pass `docker compose config`, follow the contract in the instructions exactly, and use no "
    "privileged mode, host mounts outside the project, docker.sock or non-allowlisted images. "
    "files must be complete, compilable source files (never diffs) that remove the AWS lock-in listed."
)
ALLOWED_BIND_SOURCES = {"./seaweedfs/s3.json"}
REQUIRED_SERVICES = {"postgres", "s3", "rabbitmq", "api", "worker"}
REQUIRED_APP_ENV = {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "AMQP_URL",
                    "QUEUE_NAME", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"}
GENERATED = ["app", ".app-src.tmp", "data-migration", "seaweedfs", "diffs", "docker-compose.yml", "docker-compose.llm.yml",
             "mapping.json", "mapping.md", "migration.diff", "MIGRATION_PLAN.md", "result.json", ".deploy.log",
             "app.llm-failed", ".deploy-result.json"]


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


_INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+][^}]*)?\}")


def check_contract(path: Path) -> str | None:
    """Ensure an LLM compose matches what the generated app/ code expects. Returns error or None."""
    raw = path.read_text(encoding="utf-8")
    # Only the generated .env exists at deploy time: any other ${VAR} silently becomes "".
    unset = sorted({m.group(1) for m in _INTERP.finditer(raw)
                    if m.group(1) not in SECRET_KEYS and not (m.group(2) or "").lstrip(":").startswith("-")})
    if unset:
        return (f"uses ${{...}} variables that are never set: {unset}; write literal values "
                f"(only {', '.join(SECRET_KEYS)} come from .env)")
    doc = yaml.safe_load(raw)
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
    hc = (svcs["s3"].get("healthcheck") or {}).get("test")
    if not hc or "8888" not in json.dumps(hc):
        return ("s3 healthcheck must probe the filer, which needs no credentials: "
                '["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8888/ || exit 1"]')
    hc = (svcs["api"].get("healthcheck") or {}).get("test")
    if not hc or "/health" not in json.dumps(hc):
        return "api needs a healthcheck that calls http://127.0.0.1:5000/health (so deploy can verify it)"
    for name in ("postgres", "s3", "rabbitmq"):
        for vol in svcs[name].get("volumes") or []:
            src = vol.get("source", "") if isinstance(vol, dict) else str(vol).split(":", 1)[0]
            if src.startswith((".", "/", "~")) and src.rstrip("/") not in ALLOWED_BIND_SOURCES:
                return (f"{name} bind-mounts {src}: keep data in named volumes "
                        "(only ./seaweedfs/s3.json may be bind-mounted)")
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


def code_files_problem(files, target: Path) -> tuple[list[dict] | None, str | None]:
    """LLM `files` -> (exact diffs, None) if they build and verify, else (None, problem)."""
    diffs, problems = files_to_diffs(files, target)
    if problems:
        return None, "; ".join(problems)[:1500]
    problem = code_problem(diffs, target)
    return (None, problem) if problem else (diffs, None)


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


def _pinned_refs() -> dict[str, str]:
    """'repo:tag' -> 'repo:tag@sha256:...' for every image pinned in the shipped template."""
    refs = re.findall(r"^\s*image:\s*[\"']?(\S+?@sha256:[0-9a-f]{64})", fallback.compose_text(), re.M)
    return {r.split("@")[0]: r for r in refs}


def pin_images(text: str) -> str:
    """Pin LLM-chosen images to the template's digests when the tag matches (supply chain)."""
    pinned = _pinned_refs()

    def sub(m):
        ref = m.group(2)
        return f"{m.group(1)}{pinned.get(ref, ref)}{m.group(3)}"
    return re.sub(r"^(\s*image:\s*[\"']?)([^\s\"'@]+)([\"']?\s*)$", sub, text, flags=re.M)


_SHORT_PORT = re.compile(r"^(\s*-\s*[\"']?)(\d+(?:-\d+)?:\d+(?:-\d+)?(?:/(?:tcp|udp))?[\"']?\s*(?:#.*)?)$", re.M)


def bind_localhost(text: str) -> str:
    """Publish short-syntax ports ("5432:5432") on 127.0.0.1 only (policy requires loopback)."""
    return _SHORT_PORT.sub(lambda m: f"{m.group(1)}127.0.0.1:{m.group(2)}", text)


def compose_problem(text, out: Path) -> tuple[str | None, dict | None]:
    """Write a candidate compose and run all gates. Returns (problem or None, validation)."""
    if not isinstance(text, str) or not text.strip():
        return "no docker_compose returned", None
    cand = out / "docker-compose.llm.yml"
    cand.write_text(bind_localhost(pin_images(text.strip())) + "\n", encoding="utf-8")
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


def _part_problems(data: dict, target: Path, out: Path) -> dict:
    """Problems per gated part of an LLM answer ({} = everything passes)."""
    problems = {}
    p, _ = compose_problem(data.get("docker_compose"), out)
    if p:
        problems["docker_compose"] = p
    _, cp = code_files_problem(data.get("files"), target)
    if cp:
        problems["files"] = cp
    return problems


def _call_llm(scan_report: dict, target: Path, out: Path, cfg: LLMConfig, use_cache: bool,
              cache_dir: Path, errors: list[str], meta: dict, include_files: list[str] | None = None,
              cache_only: bool = False) -> dict:
    """LLM call with response cache and one repair round for the gated parts (compose, code)."""
    user_prompt = build_user_prompt(scan_report, target, include_files)
    cache_file = _cache_file(cache_dir, cfg, user_prompt)
    if use_cache and cache_file.is_file():
        try:
            meta["cache"] = "hit"
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta["cache"] = "corrupt"
    meta["cache"] = meta.get("cache") or ("miss" if use_cache else "off")
    if cache_only:
        errors.append("No cached LLM answer for this input: public demo visitors get offline templates "
                      "(new LLM calls need the presenter key)")
        return {}

    try:
        data = chat_json(SYSTEM_PROMPT, user_prompt, cfg)
        meta["calls"] += 1
    except LLMError as e:
        errors.append(str(e))
        return {}

    problems = _part_problems(data, target, out)
    if problems:
        meta["repair"] = "attempted"
        listing = "\n".join(f"- {k}: {v}" for k, v in problems.items())
        history = [{"role": "assistant", "content": json.dumps(data)},
                   {"role": "user", "content": REPAIR_PROMPT.format(problem=listing[:3000])}]
        try:
            fixed = chat_json(SYSTEM_PROMPT, user_prompt, cfg, history=history)
            meta["calls"] += 1
            still = _part_problems(fixed, target, out)
            repaired = [k for k in problems if k not in still]
            for k in repaired:
                data[k] = fixed[k]
            # adopt other repaired parts only where the first answer was unusable
            for key, ok in (("mapping", _valid_mapping), ("plan", _valid_plan)):
                if ok(data.get(key)) is None and ok(fixed.get(key)) is not None:
                    data[key] = fixed[key]
            meta["repair"] = "fixed" if len(repaired) == len(problems) else "partial" if repaired else "failed"
            for k in repaired:
                errors.append(f"LLM {k} rejected ({problems[k][:200]}); repaired on 2nd attempt")
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
             cache_dir: str | Path | None = None, include_files: list[str] | None = None,
             llm_cache_only: bool = False) -> dict:
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
                             Path(cache_dir) if cache_dir else CACHE_DIR, errors, meta, include_files,
                             cache_only=llm_cache_only)
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

    # (c) code: complete files from the LLM -> exact diffs, built + verified; else template diffs
    diffs = None
    if llm_data:
        diffs, problem = code_files_problem(llm_data.get("files"), target)
        if problem:
            errors.append(f"LLM code rejected: {problem}")
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
