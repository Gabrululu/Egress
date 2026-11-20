"""Security policy for generated docker-compose files.

The Deploy button runs `docker compose up` on whatever the generator produced, and the
LLM prompt contains code from the scanned (possibly untrusted) repo. A prompt-injected
compose could otherwise mount the Docker socket or the host filesystem, run privileged
containers, or pull arbitrary images. This module rejects anything outside a small,
known-safe envelope. It is applied both at generation time and right before deploy.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

# Image repositories that may be pulled (tag is free; digest pinning recommended).
ALLOWED_IMAGES = {
    "postgres", "library/postgres", "chrislusf/seaweedfs", "rabbitmq", "library/rabbitmq",
    "dxflrs/garage", "minio/minio", "quay.io/minio/minio", "redis", "valkey/valkey", "nats",
    "scylladb/scylla", "prom/prometheus", "grafana/grafana", "grafana/loki",
    "quay.io/keycloak/keycloak",
}
# Host ports the demo is allowed to publish (same list as the preflight check).
ALLOWED_HOST_PORTS = {5000, 5432, 9000, 9001, 5672, 15672}

FORBIDDEN_KEYS = {
    "privileged": "privileged containers",
    "cap_add": "extra Linux capabilities",
    "devices": "host device access",
    "device_cgroup_rules": "device cgroup rules",
    "pid": "host PID namespace",
    "ipc": "shared IPC namespace",
    "userns_mode": "user namespace override",
    "cgroup_parent": "custom cgroup parent",
    "volumes_from": "volumes from other containers",
    "security_opt": "security option overrides",
    "sysctls": "kernel sysctls",
    "extends": "extending external files",
    "env_file": "reading env files from outside the output dir",
}


def _image_repo(image: str) -> str:
    ref = image.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:  # strip tag (but not a registry port like host:5000/x)
        ref = ref[: len(ref) - len(last)] + last.split(":", 1)[0]
    if ref.startswith("docker.io/"):
        ref = ref[len("docker.io/"):]
    return ref


def _inside(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _normalize_volume(v) -> dict:
    """Short syntax 'src:dst[:mode]' -> {'type','source','target'}."""
    if isinstance(v, dict):
        return v
    parts = str(v).split(":")
    if len(parts) == 1:
        return {"type": "volume", "target": parts[0]}
    src = parts[0]
    is_bind = src.startswith((".", "/", "~")) or "/" in src
    return {"type": "bind" if is_bind else "volume", "source": src, "target": parts[1]}


def _published_ports(ports) -> list[int]:
    out = []
    for p in ports or []:
        if isinstance(p, dict):
            pub = p.get("published")
        else:
            s = str(p).split("/", 1)[0]
            bits = s.split(":")
            pub = bits[-2] if len(bits) >= 2 else None
        if pub not in (None, ""):
            try:
                out.append(int(str(pub).split("-", 1)[0]))
            except ValueError:
                out.append(-1)
    return out


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host_ips(ports) -> list[tuple[int, str]]:
    """(published port, host ip or '') for every published port."""
    out = []
    for p in ports or []:
        if isinstance(p, dict):
            pub, ip = p.get("published"), str(p.get("host_ip") or "")
        else:
            bits = str(p).split("/", 1)[0].split(":")
            pub, ip = (bits[-2], ":".join(bits[:-2])) if len(bits) >= 2 else (None, "")
        if pub not in (None, ""):
            out.append((str(pub), ip.strip("[]")))
    return out


def check_policy(doc: dict, project_dir: Path) -> list[str]:
    """Return a list of violations (empty = allowed). `doc` may be raw YAML or normalized config."""
    project_dir = Path(project_dir)
    errors: list[str] = []
    if "docker.sock" in json.dumps(doc):
        errors.append("references the Docker socket (docker.sock)")
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return errors + ["'services' is not a mapping"]

    for name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"{name}: service definition is not a mapping")
            continue
        for key, why in FORBIDDEN_KEYS.items():
            if svc.get(key):
                errors.append(f"{name}: '{key}' not allowed ({why})")
        for key in ("network_mode",):
            val = str(svc.get(key) or "")
            if val == "host" or val.startswith(("container:", "service:")):
                errors.append(f"{name}: network_mode '{val}' not allowed")

        image, build = svc.get("image"), svc.get("build")
        if build:
            ctx = build if isinstance(build, str) else (build.get("context") or ".")
            ctx_path = ctx if Path(ctx).is_absolute() else str(project_dir / ctx)
            if "://" in str(ctx) or not _inside(ctx_path, project_dir):
                errors.append(f"{name}: build context '{ctx}' is outside the output directory")
            if isinstance(build, dict) and build.get("dockerfile_inline"):
                errors.append(f"{name}: inline Dockerfile not allowed")
        elif image:
            if _image_repo(str(image)) not in ALLOWED_IMAGES:
                errors.append(f"{name}: image '{image}' is not on the allowlist")
        else:
            errors.append(f"{name}: neither image nor build")

        for v in svc.get("volumes") or []:
            vol = _normalize_volume(v)
            vtype, src = vol.get("type", "volume"), str(vol.get("source") or "")
            if vtype in ("bind",):
                src_path = src if Path(src).is_absolute() else str(project_dir / src)
                if not _inside(src_path, project_dir):
                    errors.append(f"{name}: bind mount '{src}' is outside the output directory")
            elif vtype not in ("volume", "tmpfs"):
                errors.append(f"{name}: volume type '{vtype}' not allowed")

        for port in _published_ports(svc.get("ports")):
            if port not in ALLOWED_HOST_PORTS:
                errors.append(f"{name}: host port {port} not in {sorted(ALLOWED_HOST_PORTS)}")
        # Docker publishes on 0.0.0.0 by default and bypasses host firewalls (ufw): on a VM with a
        # public IP that exposes Postgres/RabbitMQ/S3 to the internet. Loopback only.
        for port, ip in _host_ips(svc.get("ports")):
            if ip not in LOOPBACK:
                errors.append(f"{name}: port {port} is published on all interfaces; "
                              f"use \"127.0.0.1:{port}:<container port>\"")

    for vname, vdef in (doc.get("volumes") or {}).items():
        if isinstance(vdef, dict) and (vdef.get("driver_opts") or vdef.get("external")):
            errors.append(f"volume '{vname}': driver_opts/external not allowed")
    return errors


def normalized_config(compose_file: Path) -> dict | None:
    """`docker compose config --format json` (resolves paths, short syntax). None if unavailable."""
    compose_file = Path(compose_file).resolve()
    try:
        r = subprocess.run(["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
                           cwd=compose_file.parent, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def check_compose_file(compose_file: Path) -> list[str]:
    """Policy check on a compose file: raw YAML plus docker's normalized view when available."""
    compose_file = Path(compose_file).resolve()
    try:
        raw = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return [f"cannot read compose: {e}"]
    errors = check_policy(raw, compose_file.parent)
    norm = normalized_config(compose_file)
    if norm:
        errors += [e for e in check_policy(norm, compose_file.parent) if e not in errors]
    return errors
