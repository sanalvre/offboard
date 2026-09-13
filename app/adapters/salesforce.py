"""Salesforce adapter: live (via the Salesforce CLI transport) and mock (seeded from fixtures/seed/salesforce.json).

Both record every call through the tracer as a `tool_call` with request and response (or error) verbatim.
Auth for live: the CLI holds the refresh token from `sf org login web --alias dev`; nothing is stored here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..models import FieldSpec, RuleSource
from ..trace import Tracer
from .schema import sf_fields


class SalesforceError(RuntimeError):
    pass


class SalesforceAdapter:
    """Interface. Subclasses implement _rest(); the public methods add tracing and shaping."""

    name = "salesforce"

    def __init__(self, tracer: Tracer, api_version: str = settings.sf_api_version):
        self.tracer = tracer
        self.api = api_version

    # ---- transport -------------------------------------------------------------------
    def _rest(self, path: str, method: str = "GET", body: Any = None, tooling: bool = False) -> Any:
        raise NotImplementedError

    def call(self, path: str, method: str = "GET", body: Any = None, tooling: bool = False) -> Any:
        base = f"/services/data/v{self.api}/" + ("tooling/" if tooling else "")
        req = {"method": method, "url": base + path, "body": body}
        t0 = time.perf_counter()
        try:
            resp = self._rest(path, method, body, tooling)
        except Exception as e:  # noqa: BLE001
            self.tracer.tool_call(f"{self.name}.rest", req, error={"type": type(e).__name__, "message": str(e)},
                                  latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            raise
        self.tracer.tool_call(f"{self.name}.rest", req, response=resp, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return resp

    # ---- domain -----------------------------------------------------------------------
    def list_rules(self, sobject: str) -> list[dict[str, Any]]:
        q = ("SELECT Id, ValidationName, Active, ErrorMessage, Description, ErrorDisplayField FROM ValidationRule "
             f"WHERE EntityDefinition.QualifiedApiName = '{sobject}' ORDER BY ValidationName")
        return self.call("query?q=" + urllib.parse.quote(q), tooling=True)["records"]

    def get_rule(self, sobject: str, name: str) -> Optional[RuleSource]:
        match = [r for r in self.list_rules(sobject) if r["ValidationName"] == name]
        if not match:
            return None
        r = match[0]
        md = self.call(f"sobjects/ValidationRule/{r['Id']}", tooling=True)["Metadata"]
        return RuleSource(object=sobject, name=name, formula=md["errorConditionFormula"], active=bool(r["Active"]),
                          error_message=r.get("ErrorMessage") or "", error_display_field=r.get("ErrorDisplayField") or md.get("errorDisplayField") or "",
                          description=r.get("Description") or md.get("description") or "", source_id=r["Id"])

    def describe_fields(self, sobject: str) -> dict[str, FieldSpec]:
        return sf_fields(self.call(f"sobjects/{sobject}/describe"))


class LiveSalesforce(SalesforceAdapter):
    name = "salesforce"

    def __init__(self, tracer: Tracer, alias: str = settings.sf_cli_alias, api_version: str = settings.sf_api_version):
        super().__init__(tracer, api_version)
        self.alias = alias
        self.bin = shutil.which("sf") or r"C:\Users\User\AppData\Roaming\npm\sf.cmd"

    def _rest(self, path: str, method: str = "GET", body: Any = None, tooling: bool = False) -> Any:
        base = f"/services/data/v{self.api}/" + ("tooling/" if tooling else "")
        cmd = [self.bin, "api", "request", "rest", base + path, "--target-org", self.alias, "--method", method]
        tmp = None
        if body is not None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(body, tmp); tmp.close()
            cmd += ["--body", "@" + tmp.name, "--header", "Content-Type: application/json"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        finally:
            if tmp:
                os.unlink(tmp.name)
        out = (p.stdout or "").strip()
        if p.returncode != 0 or '"errorCode"' in out[:300]:
            raise SalesforceError(out or p.stderr)
        return json.loads(out) if out else None


class MockSalesforce(SalesforceAdapter):
    """Seeded from fixtures/seed/salesforce.json (exported from the real org). Read-only in the pipeline."""

    name = "salesforce.mock"

    def __init__(self, tracer: Tracer, seed_path: Optional[Path] = None):
        super().__init__(tracer)
        path = seed_path or settings.fixtures_dir / "seed" / "salesforce.json"
        self.seed = json.loads(Path(path).read_text(encoding="utf-8"))

    def _rest(self, path: str, method: str = "GET", body: Any = None, tooling: bool = False) -> Any:
        if method != "GET":
            raise SalesforceError("mock Salesforce is read-only")
        if tooling and path.startswith("query?q="):
            q = urllib.parse.unquote(path[len("query?q="):])
            if "FROM ValidationRule" in q:
                recs = [{"attributes": {"type": "ValidationRule"}, "Id": r["id"], "ValidationName": r["name"], "Active": r["active"],
                         "ErrorMessage": r["error_message"], "Description": r["description"], "ErrorDisplayField": r["error_display_field"]}
                        for r in self.seed["rules"] if f"'{r['object']}'" in q]
                return {"size": len(recs), "totalSize": len(recs), "done": True, "records": recs}
            raise SalesforceError(f"mock: unsupported tooling query {q!r}")
        if tooling and path.startswith("sobjects/ValidationRule/"):
            rid = path.rsplit("/", 1)[1]
            for r in self.seed["rules"]:
                if r["id"] == rid:
                    return {"Id": rid, "FullName": f"{r['object']}.{r['name']}", "Metadata": {
                        "active": r["active"], "errorConditionFormula": r["formula"], "errorMessage": r["error_message"],
                        "errorDisplayField": r["error_display_field"] or None, "description": r["description"] or None}}
            raise SalesforceError(json.dumps([{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]))
        if path.endswith("/describe"):
            return self.seed["describe"]
        raise SalesforceError(f"mock: unsupported path {path!r}")
