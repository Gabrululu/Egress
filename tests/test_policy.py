from pathlib import Path

import pytest

from copilot import fallback
from copilot.policy import check_compose_file, check_policy

OK_SVC = {"image": "postgres:16-alpine", "ports": ["5432:5432"]}


def _doc(**svc):
    return {"services": {"x": {**OK_SVC, **svc}}}


def test_template_passes(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(fallback.compose_text())
    (tmp_path / "app").mkdir()
    (tmp_path / "seaweedfs").mkdir()
    (tmp_path / "seaweedfs" / "s3.json").write_text("{}")
    assert check_compose_file(tmp_path / "docker-compose.yml") == []


@pytest.mark.parametrize("svc, needle", [
    ({"privileged": True}, "privileged"),
    ({"cap_add": ["SYS_ADMIN"]}, "cap_add"),
    ({"network_mode": "host"}, "network_mode"),
    ({"pid": "host"}, "pid"),
    ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, "docker.sock"),
    ({"volumes": ["/:/host"]}, "outside the output directory"),
    ({"volumes": ["../../etc:/etc2"]}, "outside the output directory"),
    ({"volumes": [{"type": "bind", "source": "/home", "target": "/h"}]}, "outside the output directory"),
    ({"image": "evil/miner:latest"}, "allowlist"),
    ({"ports": ["2222:22"]}, "host port 2222"),
    ({"image": None, "build": {"context": "../../"}}, "build context"),
    ({"image": None, "build": "https://github.com/evil/repo.git"}, "build context"),
])
def test_violations(tmp_path, svc, needle):
    errs = check_policy(_doc(**svc), tmp_path)
    assert any(needle in e for e in errs), errs


def test_allowed_variants(tmp_path):
    (tmp_path / "app").mkdir()
    doc = {"services": {
        "db": {"image": "docker.io/library/postgres:16@sha256:abc", "volumes": ["pgdata:/var/lib/pg"]},
        "s3": {"image": "chrislusf/seaweedfs:3.97", "volumes": ["./seaweedfs/s3.json:/etc/s3.json:ro"],
               "ports": [{"target": 8333, "published": "9000"}]},
        "api": {"build": "./app", "ports": ["127.0.0.1:5000:5000"]},
    }, "volumes": {"pgdata": {}}}
    assert check_policy(doc, tmp_path) == []


def test_driver_opts_volume_rejected(tmp_path):
    doc = {"services": {"db": OK_SVC},
           "volumes": {"v": {"driver": "local", "driver_opts": {"o": "bind", "device": "/"}}}}
    assert any("driver_opts" in e for e in check_policy(doc, tmp_path))
