"""SARIF 2.1.0 export so findings show up in GitHub Code Scanning (and other SARIF viewers)."""
from __future__ import annotations

import hashlib

from . import __version__
from .rules import RULES

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVEL = {"high": "error", "medium": "warning", "low": "note"}
# GitHub uses `security-severity` (0-10) to bucket alerts: >=7 high, >=4 medium, else low.
SECURITY_SEVERITY = {"high": "8.0", "medium": "5.0", "low": "2.0"}
_EXTRA_RULES = {
    "py-ast-client-other": ("boto3", "medium", "boto3 client for another AWS service."),
}


def _rule_descriptors() -> list[dict]:
    items = [(r.id, r.service, r.severity, r.reason) for r in RULES]
    items += [(rid, svc, sev, reason) for rid, (svc, sev, reason) in _EXTRA_RULES.items()]
    return [{
        "id": rid,
        "name": f"AwsLockIn/{svc}/{rid}",
        "shortDescription": {"text": f"AWS lock-in: {svc}"},
        "fullDescription": {"text": reason},
        "help": {"text": f"{reason} See the Egress migration plan for an open-source alternative.",
                 "markdown": f"**{svc} lock-in.** {reason}\n\nRun Egress to generate a migration to an "
                             "open-source / EU-sovereign alternative."},
        "defaultConfiguration": {"level": LEVEL[sev]},
        "properties": {"tags": ["lock-in", "aws", svc.lower()], "security-severity": SECURITY_SEVERITY[sev],
                       "precision": "high"},
    } for rid, svc, sev, reason in items]


def to_sarif(report: dict, uri_prefix: str = "") -> dict:
    """Convert a scan report. `uri_prefix` makes paths repo-relative when the scan target is a
    subdirectory (e.g. "sample_app/")."""
    rules = _rule_descriptors()
    index = {r["id"]: i for i, r in enumerate(rules)}
    prefix = uri_prefix.strip("/") + "/" if uri_prefix.strip("/") else ""
    results = []
    for f in report["findings"]:
        uri = prefix + f["file"].replace("\\", "/")
        fp = hashlib.sha256(f"{f['rule']}|{uri}|{f['evidence']}".encode()).hexdigest()[:32]
        res = {
            "ruleId": f["rule"],
            "level": LEVEL[f["severity"]],
            "message": {"text": f"{f['service']}: {f['lock_in_reason']}"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": uri, "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": int(f["line"]), "snippet": {"text": f["evidence"]}},
            }}],
            # stable across line moves: rule + file + evidence text
            "partialFingerprints": {"egressFinding/v1": fp},
            "properties": {"service": f["service"], "severity": f["severity"]},
        }
        if f["rule"] in index:
            res["ruleIndex"] = index[f["rule"]]
        if f.get("mitigated"):
            res["suppressions"] = [{"kind": "inSource",
                                    "justification": "Client uses a custom S3-compatible endpoint_url."}]
        results.append(res)
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "Egress",
                "informationUri": "https://github.com/",
                "semanticVersion": __version__,
                "rules": rules,
            }},
            "originalUriBaseIds": {"%SRCROOT%": {"description": {"text": "Repository root"}}},
            "results": results,
            "properties": {"overallScore": report["overall_score"], "rating": report["rating"]},
        }],
    }
