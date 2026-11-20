"""AST-based detection of AWS SDK clients in Python (stdlib `ast`, no dependencies).

Catches what line regexes miss:
  - multi-line calls:        boto3.client(\n    "s3", ...)
  - import aliases:          import boto3 as b3; b3.client("sqs")
  - direct imports:          from boto3 import client, resource; client("sns")
  - sessions:                boto3.Session().client("s3"); s = boto3.session.Session(); s.resource("dynamodb")
  - keyword service names:   boto3.client(service_name="lambda")
and records per call whether a custom (non-AWS) endpoint_url is configured, so mitigation is
precise per client instead of per file.
"""
from __future__ import annotations

import ast

from .rules import RULES

SERVICE_RULE = {
    "s3": ("S3", "py-s3-client"), "rds": ("RDS", "py-rds-client"), "rds-data": ("RDS", "py-rds-client"),
    "dynamodb": ("DynamoDB", "py-dynamo"), "sqs": ("SQS", "py-sqs-client"), "sns": ("SNS", "py-sns"),
    "lambda": ("Lambda", "py-lambda-client"), "logs": ("CloudWatch", "py-cw"),
    "cloudwatch": ("CloudWatch", "py-cw"), "iam": ("IAM", "py-iam"), "sts": ("IAM", "py-iam"),
}
# Regex rules superseded by the AST pass when a file parses.
AST_RULE_IDS = {rule for _, rule in SERVICE_RULE.values()} | {"py-boto3-import"}
_RULES = {r.id: r for r in RULES}
SDK_MODULES = ("boto3", "botocore", "aioboto3", "aiobotocore")


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.module_aliases: set[str] = set()      # names bound to boto3 module
        self.factory_names: set[str] = set()       # names bound to boto3.client/resource
        self.session_classes: set[str] = set()     # names bound to boto3.Session / session.Session
        self.session_vars: set[str] = set()        # variables holding a Session instance
        self.imports: list[ast.AST] = []
        self.clients: list[dict] = []

    # ---- imports
    def visit_Import(self, node: ast.Import):
        for a in node.names:
            root = a.name.split(".")[0]
            if root in SDK_MODULES:
                self.imports.append(node)
                if a.name == "boto3" or (a.asname is None and root == "boto3"):
                    self.module_aliases.add(a.asname or "boto3")
                elif a.name == "boto3.session":
                    self.module_aliases.add(a.asname or "boto3")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        if mod.split(".")[0] in SDK_MODULES:
            self.imports.append(node)
            for a in node.names:
                name = a.asname or a.name
                if mod == "boto3" and a.name in ("client", "resource"):
                    self.factory_names.add(name)
                elif mod in ("boto3", "boto3.session") and a.name == "Session":
                    self.session_classes.add(name)
                elif mod == "boto3" and a.name == "session":
                    self.module_aliases.add(name)  # session.Session(...)
        self.generic_visit(node)

    # ---- sessions
    def _is_session_ctor(self, node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        if isinstance(f, ast.Name) and f.id in self.session_classes:
            return True
        if isinstance(f, ast.Attribute) and f.attr == "Session":
            v = f.value
            if isinstance(v, ast.Name) and v.id in self.module_aliases:
                return True
            if isinstance(v, ast.Attribute) and v.attr == "session":
                return isinstance(v.value, ast.Name) and v.value.id in self.module_aliases
        return False

    def visit_Assign(self, node: ast.Assign):
        if self._is_session_ctor(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.session_vars.add(t.id)
        self.generic_visit(node)

    # ---- client/resource calls
    def _is_factory(self, func) -> bool:
        if isinstance(func, ast.Name):
            return func.id in self.factory_names
        if isinstance(func, ast.Attribute) and func.attr in ("client", "resource"):
            v = func.value
            if isinstance(v, ast.Name) and (v.id in self.module_aliases or v.id in self.session_vars):
                return True
            if self._is_session_ctor(v):
                return True
        return False

    def visit_Call(self, node: ast.Call):
        if self._is_factory(node.func):
            service = _const_str(node.args[0]) if node.args else None
            endpoint = None
            for kw in node.keywords:
                if kw.arg == "service_name":
                    service = _const_str(kw.value) or service
                elif kw.arg == "endpoint_url":
                    endpoint = kw.value
            custom = endpoint is not None and "amazonaws.com" not in (_const_str(endpoint) or "")
            self.clients.append({"service": service, "line": node.lineno, "custom_endpoint": custom})
        self.generic_visit(node)


def analyze(text: str, rel: str) -> tuple[list[dict], dict] | None:
    """Return (findings, info) or None when the file does not parse (caller falls back to regex)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    v = _Visitor()
    v.visit(tree)
    lines = text.splitlines()
    evidence = lambda n: lines[n - 1].strip()[:200] if 0 < n <= len(lines) else ""  # noqa: E731

    findings = []
    imp = _RULES["py-boto3-import"]
    for node in v.imports:
        findings.append({"service": "boto3", "file": rel, "line": node.lineno, "evidence": evidence(node.lineno),
                         "lock_in_reason": imp.reason, "severity": imp.severity, "rule": imp.id})
    for c in v.clients:
        svc, rule_id = SERVICE_RULE.get((c["service"] or "").lower(), (None, None))
        if rule_id:
            r = _RULES[rule_id]
            f = {"service": svc, "file": rel, "line": c["line"], "evidence": evidence(c["line"]),
                 "lock_in_reason": r.reason, "severity": r.severity, "rule": r.id}
        else:
            name = c["service"] or "dynamic service name"
            f = {"service": "boto3", "file": rel, "line": c["line"], "evidence": evidence(c["line"]),
                 "lock_in_reason": f"boto3 client for AWS service '{name}'.", "severity": "medium",
                 "rule": "py-ast-client-other"}
        # Only an S3 client can be made portable by pointing it at an S3-compatible endpoint.
        f["portable"] = bool(c["custom_endpoint"] and svc == "S3")
        findings.append(f)
    clients = [f for f in findings if f["rule"] != "py-boto3-import"]
    # Lines inside multi-line string literals (docstrings, prompts, embedded YAML): prose, not code.
    string_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and getattr(node, "end_lineno", None):
            if (isinstance(node, ast.JoinedStr) or isinstance(node.value, str)) and node.end_lineno > node.lineno:
                string_lines.update(range(node.lineno, node.end_lineno + 1))
    info = {"parsed": True, "portable_file": bool(clients) and all(f["portable"] for f in clients),
            "string_lines": string_lines}
    return findings, info
