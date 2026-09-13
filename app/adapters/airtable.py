"""Airtable adapter: live (pyairtable + REST) and mock (seeded from fixtures/seed/airtable.json, state persisted
to state/mock_airtable.json so DUPLICATE and recovery cases work across runs; reset via reset()).

Every call is traced as a `tool_call`. Fault injection (`inject_fault="airtable.create_record:503"`) lets the
eval harness simulate a partial failure in the mock; the live adapter ignores it.

Airtable facts this relies on (verified 2026-09-13 in the real base): formula fields CAN be created via
`POST .../fields` with type "formula"; field type/precision cannot be changed; there is no delete-field
endpoint; 1,000 API calls per workspace per month on the Free plan, so live runs are kept to a handful of calls.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import settings
from ..models import FieldSpec
from ..trace import Tracer
from .schema import at_fields

OPP_TABLE = "Opportunities"
RULES_TABLE = "Migration_Rules"


class AirtableError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class AirtableAdapter:
    name = "airtable"

    def __init__(self, tracer: Tracer, inject_fault: Optional[str] = None):
        self.tracer = tracer
        self.inject_fault = inject_fault  # "airtable.<op>:<status>" (mock only)
        self.calls = 0

    # transport hooks ---------------------------------------------------------------
    def _schema(self) -> dict[str, Any]: raise NotImplementedError
    def _list(self, table: str, formula: Optional[str]) -> list[dict]: raise NotImplementedError
    def _create_field(self, table: str, body: dict) -> dict: raise NotImplementedError
    def _create_record(self, table: str, fields: dict) -> dict: raise NotImplementedError
    def _get_record(self, table: str, record_id: str) -> dict: raise NotImplementedError
    def _delete_record(self, table: str, record_id: str) -> dict: raise NotImplementedError

    def _traced(self, op: str, request: dict, fn):
        self.calls += 1
        t0 = time.perf_counter()
        try:
            resp = fn()
        except Exception as e:  # noqa: BLE001
            err = {"type": type(e).__name__, "message": str(e)}
            if isinstance(e, AirtableError):
                err.update(status=e.status, body=e.body)
            self.tracer.tool_call(f"{self.name}.{op}", request, error=err, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            raise
        self.tracer.tool_call(f"{self.name}.{op}", request, response=resp, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return resp

    # domain ------------------------------------------------------------------------
    def schema(self) -> dict[str, Any]:
        return self._traced("get_schema", {"method": "GET", "path": "/meta/bases/{base}/tables"}, self._schema)

    def table_fields(self, schema: dict[str, Any], table: str) -> dict[str, FieldSpec]:
        t = next(t for t in schema["tables"] if t["name"] == table)
        return at_fields(t)

    def raw_table(self, schema: dict[str, Any], table: str) -> dict[str, Any]:
        return next(t for t in schema["tables"] if t["name"] == table)

    def list_records(self, table: str, formula: Optional[str] = None) -> list[dict]:
        return self._traced("list_records", {"method": "GET", "table": table, "filterByFormula": formula}, lambda: self._list(table, formula))

    def create_field(self, table: str, name: str, ftype: str, options: Optional[dict] = None, description: str = "") -> dict:
        body = {"name": name, "type": ftype, **({"options": options} if options else {}), **({"description": description[:20000]} if description else {})}
        return self._traced("create_field", {"method": "POST", "table": table, "body": body}, lambda: self._create_field(table, body))

    def create_record(self, table: str, fields: dict) -> dict:
        return self._traced("create_record", {"method": "POST", "table": table, "body": {"fields": fields}}, lambda: self._create_record(table, fields))

    def get_record(self, table: str, record_id: str) -> dict:
        return self._traced("get_record", {"method": "GET", "table": table, "id": record_id}, lambda: self._get_record(table, record_id))

    def delete_record(self, table: str, record_id: str) -> dict:
        return self._traced("delete_record", {"method": "DELETE", "table": table, "id": record_id}, lambda: self._delete_record(table, record_id))

    def snapshot(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Before/after state used for the bounded-diff invariant: field lists and all records of every table."""
        snap: dict[str, Any] = {}
        for t in schema["tables"]:
            recs = self.list_records(t["name"])
            snap[t["name"]] = {"fields": sorted(f["name"] for f in t["fields"]),
                               "records": {r["id"]: r["fields"] for r in recs}}
        return snap


# ------------------------------------------------------------------------------------ live


class LiveAirtable(AirtableAdapter):
    name = "airtable"

    def __init__(self, tracer: Tracer, inject_fault: Optional[str] = None):
        super().__init__(tracer, None)  # never inject faults into the real base
        if not settings.airtable_pat or not settings.airtable_base_id:
            raise RuntimeError("AIRTABLE_PAT / AIRTABLE_BASE_ID not configured")
        self.base = settings.airtable_base_id
        self.h = {"Authorization": f"Bearer {settings.airtable_pat}"}
        self.http = httpx.Client(timeout=30)

    def _req(self, method: str, url: str, **kw) -> Any:
        r = self.http.request(method, url, headers=self.h, **kw)
        if r.status_code >= 400:
            raise AirtableError(r.status_code, r.text[:500])
        return r.json()

    def _schema(self):
        return self._req("GET", f"https://api.airtable.com/v0/meta/bases/{self.base}/tables")

    def _list(self, table, formula):
        params = {"filterByFormula": formula} if formula else {}
        out, offset = [], None
        while True:
            if offset:
                params["offset"] = offset
            page = self._req("GET", f"https://api.airtable.com/v0/{self.base}/{httpx.URL(table).path}", params=params)
            out += page.get("records", [])
            offset = page.get("offset")
            if not offset:
                return out

    def _create_field(self, table, body):
        tid = self._table_id(table)
        return self._req("POST", f"https://api.airtable.com/v0/meta/bases/{self.base}/tables/{tid}/fields", json=body)

    def _create_record(self, table, fields):
        return self._req("POST", f"https://api.airtable.com/v0/{self.base}/{httpx.URL(table).path}", json={"fields": fields, "typecast": True})

    def _get_record(self, table, record_id):
        return self._req("GET", f"https://api.airtable.com/v0/{self.base}/{httpx.URL(table).path}/{record_id}")

    def _delete_record(self, table, record_id):
        return self._req("DELETE", f"https://api.airtable.com/v0/{self.base}/{httpx.URL(table).path}/{record_id}")

    def _table_id(self, table: str) -> str:
        for t in self._schema()["tables"]:
            if t["name"] == table:
                return t["id"]
        raise AirtableError(404, f"table {table} not found")


# ------------------------------------------------------------------------------------ mock


class MockAirtable(AirtableAdapter):
    """In-memory base seeded from the fixture; guard fields are stripped at load so the mock starts pre-migration.
    State persists in state/mock_airtable.json between runs (reset() restores the seed)."""

    name = "airtable.mock"
    STATE = settings.state_dir / "mock_airtable.json"

    def __init__(self, tracer: Tracer, inject_fault: Optional[str] = None, seed_path: Optional[Path] = None):
        super().__init__(tracer, inject_fault)
        self.seed_path = Path(seed_path or settings.fixtures_dir / "seed" / "airtable.json")
        self.STATE.parent.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(self.STATE.read_text(encoding="utf-8")) if self.STATE.exists() else self._fresh()
        self._fault_fired = False

    def _fresh(self) -> dict[str, Any]:
        seed = json.loads(self.seed_path.read_text(encoding="utf-8"))
        tables = copy.deepcopy(seed["tables"])
        for t in tables.values():
            guard_ids = {f["id"] for f in t["fields"] if f["name"].startswith("Guard:")}
            t["fields"] = [f for f in t["fields"] if f["id"] not in guard_ids]
            for r in t["records"]:
                for f in list(r["fields"]):
                    if f.startswith("Guard:"):
                        del r["fields"][f]
        return {"tables": tables, "counter": 0}

    def reset(self) -> None:
        self.state = self._fresh()
        self._save()

    def _save(self) -> None:
        self.STATE.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def _maybe_fault(self, op: str) -> None:
        if self.inject_fault and not self._fault_fired and self.inject_fault.startswith(f"airtable.{op}:"):
            self._fault_fired = True
            status = int(self.inject_fault.rsplit(":", 1)[1])
            raise AirtableError(status, {"error": {"type": "SERVICE_UNAVAILABLE" if status == 503 else "INJECTED", "message": f"injected fault for eval ({op})"}})

    def _t(self, table: str) -> dict:
        if table not in self.state["tables"]:
            raise AirtableError(404, {"error": {"type": "TABLE_NOT_FOUND", "message": f"Could not find table {table}"}})
        return self.state["tables"][table]

    def _schema(self):
        return {"tables": [{"id": t["id"], "name": name, "primaryFieldId": t["primaryFieldId"],
                            "fields": [{k: v for k, v in f.items() if v is not None} for f in t["fields"]]}
                           for name, t in self.state["tables"].items()]}

    def _list(self, table, formula):
        t = self._t(table)
        recs = [self._with_formulas(table, r) for r in t["records"]]
        if formula:
            # only the shape the pipeline uses: {Rule} = "name"  or FIND('x', {Name})
            import re
            m = re.fullmatch(r"\{(?P<f>[^}]+)\}\s*=\s*['\"](?P<v>.*)['\"]", formula.strip())
            if not m:
                raise AirtableError(422, {"error": {"type": "INVALID_FILTER_BY_FORMULA", "message": f"mock supports only {{Field}}=\"value\" (got {formula!r})"}})
            recs = [r for r in recs if str(r["fields"].get(m["f"], "")) == m["v"]]
        return recs

    def _create_field(self, table, body):
        self._maybe_fault("create_field")
        t = self._t(table)
        if any(f["name"] == body["name"] for f in t["fields"]):
            raise AirtableError(422, {"error": {"type": "DUPLICATE_OR_EMPTY_FIELD_NAME", "message": "Field name already exists"}})
        self.state["counter"] += 1
        f = {"id": f"fldMOCK{self.state['counter']:010d}", "name": body["name"], "type": body["type"], "options": body.get("options"),
             "description": body.get("description")}
        t["fields"].append(f)
        self._save()
        return {k: v for k, v in f.items() if v is not None}

    def _create_record(self, table, fields):
        self._maybe_fault("create_record")
        t = self._t(table)
        known = {f["name"] for f in t["fields"]}
        unknown = [k for k in fields if k not in known]
        if unknown:
            raise AirtableError(422, {"error": {"type": "UNKNOWN_FIELD_NAME", "message": f"Unknown field name: {unknown[0]}"}})
        self.state["counter"] += 1
        r = {"id": f"recMOCK{self.state['counter']:010d}", "createdTime": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "fields": dict(fields)}
        t["records"].append(r)
        self._save()
        return r

    def _get_record(self, table, record_id):
        for r in self._t(table)["records"]:
            if r["id"] == record_id:
                return self._with_formulas(table, r)
        raise AirtableError(404, {"error": "NOT_FOUND"})

    def _delete_record(self, table, record_id):
        t = self._t(table)
        before = len(t["records"])
        t["records"] = [r for r in t["records"] if r["id"] != record_id]
        if len(t["records"]) == before:
            raise AirtableError(404, {"error": "NOT_FOUND"})
        self._save()
        return {"id": record_id, "deleted": True}

    def _with_formulas(self, table, r):
        """Mock evaluation of formula fields with the project's own parsers and reference interpreter under Airtable
        semantics. Live mode reads Airtable's real computation; the mock approximates it."""
        from ..parsers.at_formula import parse_at_formula
        from ..parsers.numeric import parse_numeric
        from ..reference import evaluate, evaluate_expr
        from .schema import at_fields
        t = self._t(table)
        specs = at_fields({"fields": t["fields"]})
        out = {"id": r["id"], "createdTime": r["createdTime"], "fields": dict(r["fields"])}
        for f in t["fields"]:
            if f["type"] != "formula":
                continue
            formula = (f.get("options") or {}).get("formula", "")
            rec = dict(r["fields"])
            num = parse_numeric(formula, specs, "at")
            if num.expr is not None:
                val = evaluate_expr(num.expr, rec, specs, "at")
                if val is not None:
                    out["fields"][f["name"]] = val
                continue
            b = parse_at_formula(formula, specs)
            if b.node is not None:
                fired = evaluate(b.node, rec, specs, "at")
                text = '"VIOLATION"' in formula
                out["fields"][f["name"]] = ("VIOLATION" if fired else "") if text else (1 if fired else 0)
        return out
