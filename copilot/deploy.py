"""docker compose wrappers for deploying the generated stack in output/."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class DeployError(Exception):
    pass


def _compose(out_dir: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    compose_file = Path(out_dir) / "docker-compose.yml"
    if not compose_file.is_file():
        raise DeployError("No output/docker-compose.yml yet. Generate the migration first.")
    try:
        return subprocess.run(["docker", "compose", "-f", str(compose_file), *args],
                              cwd=out_dir, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DeployError("docker is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise DeployError(f"docker compose {args[0]} timed out after {timeout}s.")


def _tail(r: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-n:])


def up(out_dir: Path, timeout: int = 420) -> str:
    r = _compose(out_dir, "up", "-d", "--build", "--wait", "--wait-timeout", str(timeout - 60),
                 timeout=timeout)
    if r.returncode != 0:
        raise DeployError(f"docker compose up failed:\n{_tail(r)}")
    return _tail(r)


def down(out_dir: Path) -> str:
    r = _compose(out_dir, "down", "--remove-orphans", timeout=120)
    if r.returncode != 0:
        raise DeployError(f"docker compose down failed:\n{_tail(r)}")
    return _tail(r)


def parse_ps(stdout: str) -> list[dict]:
    """`docker compose ps --format json` prints a JSON array (old) or NDJSON (new)."""
    stdout = stdout.strip()
    if not stdout:
        return []
    if stdout.startswith("["):
        items = json.loads(stdout)
    else:
        items = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    rows = []
    for c in items:
        ports = sorted({f"{p['PublishedPort']}->{p['TargetPort']}" for p in c.get("Publishers") or []
                        if p.get("PublishedPort")})
        rows.append({"service": c.get("Service", ""), "state": c.get("State", ""),
                     "health": c.get("Health", "") or "-", "status": c.get("Status", ""),
                     "ports": ", ".join(ports)})
    return sorted(rows, key=lambda r: r["service"])


def status(out_dir: Path) -> list[dict]:
    r = _compose(out_dir, "ps", "-a", "--format", "json", timeout=30)
    if r.returncode != 0:
        raise DeployError(f"docker compose ps failed:\n{_tail(r)}")
    return parse_ps(r.stdout)
