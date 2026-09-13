"""FastAPI service. X-API-Key decides the mode: sk_test_* -> mocks + cassettes, sk_live_* -> real systems."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .adapters.airtable import LiveAirtable, MockAirtable
from .adapters.discord import LiveDiscord, MockDiscord
from .adapters.salesforce import LiveSalesforce, MockSalesforce
from .config import Mode, resolve_mode, settings
from .llm import Proposer
from .models import RunRequest, RunSummary
from .pipeline import Pipeline
from .trace import Tracer

app = FastAPI(title="OffBoard", version="0.1", description="Salesforce logic capture and Z3-verified migration to Airtable, with a receipt for every decision.")


def mode_dep(x_api_key: Optional[str] = Header(default=None)) -> Mode:
    mode = resolve_mode(x_api_key)
    if mode is None:
        raise HTTPException(401, "missing or unknown API key (use sk_test_* or sk_live_*)")
    return mode


def build_pipeline(mode: Mode, req: RunRequest, llm_mode: Optional[str] = None) -> Pipeline:
    tracer = Tracer(mode=mode, rule=req.rule, case_id=req.case_id, attempt=req.attempt)
    if mode == "test":
        sf, at, dc = MockSalesforce(tracer), MockAirtable(tracer, inject_fault=req.inject_fault), MockDiscord(tracer)
        proposer = Proposer(tracer, llm_mode or "cassette")
    else:
        sf, at, dc = LiveSalesforce(tracer), LiveAirtable(tracer), LiveDiscord(tracer)
        proposer = Proposer(tracer, llm_mode or "live")
    return Pipeline(tracer, sf, at, dc, proposer, mode)


@app.post("/runs", response_model=RunSummary)
def create_run(req: RunRequest, mode: Mode = Depends(mode_dep)) -> RunSummary:
    return build_pipeline(mode, req).run(req)


@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    out = []
    for p in sorted(settings.traces_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append({"run_id": d["header"]["run_id"], "mode": d["header"]["mode"], "rule": d["header"]["rule"], "case_id": d["header"].get("case_id"),
                    "started_at": d["header"].get("started_at"), "verdict": d.get("summary", {}).get("verdict"),
                    "system_confidence": d.get("summary", {}).get("decision", {}).get("system_confidence"),
                    "llm_confidence": d.get("summary", {}).get("decision", {}).get("llm_confidence"),
                    "unsupported_claims": d.get("summary", {}).get("unsupported_claims"), "canonical_hash": d.get("canonical_hash")})
    return out


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> Any:
    p = settings.traces_dir / f"{run_id}.json"
    if not p.exists() or not run_id.startswith("run_"):
        raise HTTPException(404, "no such run")
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/inventory")
def inventory_endpoint(object: str = "Opportunity", mode: Mode = Depends(mode_dep)) -> dict[str, Any]:
    """Coverage receipt across every logic type in the org (validation rules, formula fields, flows, workflow rules,
    assignment rules, Apex, approval processes), each with a verifiability reason and its latest status."""
    from .inventory import inventory
    return inventory(mode, [object])


@app.get("/mapping")
def mapping_endpoint(object: str = "Opportunity", mode: Mode = Depends(mode_dep)) -> dict[str, Any]:
    """Schema-mapping report: for every source field a verifiable artefact touches, can every legal value land in Airtable?"""
    from .adapters.schema import at_fields as at_specs, sf_fields as sf_specs
    from .inventory import parse_metadata, retrieve_live, seed_dir
    from .mapping import mapping_report
    tracer = Tracer(mode=mode, rule=f"{object}.mapping", case_id="mapping")
    sf = MockSalesforce(tracer) if mode == "test" else LiveSalesforce(tracer)
    at = MockAirtable(tracer) if mode == "test" else LiveAirtable(tracer)
    desc = sf.call(f"sobjects/{object}/describe")
    sfields = sf_specs(desc)
    labels = {f["name"]: f.get("label", f["name"]) for f in desc["fields"]}
    sf_meta = {f["name"]: {"scale": f.get("scale"), "precision": f.get("precision"), "length": f.get("length")} for f in desc["fields"]}
    schema = at.schema()
    table = at.raw_table(schema, "Opportunities")
    afields = at_specs(table)
    at_meta = {f["name"]: {"precision": (f.get("options") or {}).get("precision")} for f in table["fields"]}
    src = seed_dir() if mode == "test" else retrieve_live([object])
    used: set[str] = set()
    for a in parse_metadata(src):
        if a.object == object and a.verifiable:
            for e in (a.details.get("formula"), ):
                pass
            used |= set(_fields_in(a, sfields))
    rep = mapping_report(sfields, afields, labels, sf_meta, at_meta, only=used or None)
    tracer.add("mapping_report", **rep)
    tracer.finalize({"verdict": None, "mapping": rep["counts"]})
    return {"object": object, "mode": mode, "fields_considered": sorted(used), **rep}


def _fields_in(a, sfields) -> set[str]:
    """Fields referenced by an artefact: from its lowered condition, its formula text, or its flow details."""
    import re
    names = set()
    if a.condition:
        stack = [a.condition]
        while stack:
            n = stack.pop()
            if n.get("field"):
                names.add(n["field"])
            stack.extend(n.get("args", []))
    for d in a.details.get("decisions", []) or []:
        c = d.get("condition")
        stack = [c] if c else []
        while stack:
            n = stack.pop()
            if n.get("field"):
                names.add(n["field"])
            stack.extend(n.get("args", []))
    for act in a.details.get("actions", []) or []:
        if act.get("field"):
            names.add(act["field"])
    formula = a.details.get("formula") or ""
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", formula):
        if tok in sfields:
            names.add(tok)
    return names


@app.post("/state/reset")
def reset_state(mode: Mode = Depends(mode_dep)) -> dict[str, str]:
    if mode != "test":
        raise HTTPException(400, "live scenario reset is a separate, deliberate script: python -m eval.reset_live")
    MockAirtable(Tracer(mode="test", rule="reset", case_id="reset")).reset()
    return {"status": "mock airtable state reset to seed"}


@app.get("/eval/report")
def eval_report() -> Any:
    p = settings.root / "eval" / "report.json"
    if not p.exists():
        raise HTTPException(404, "no report yet; run python -m eval.run_eval")
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/", response_class=HTMLResponse)
def ui() -> Any:
    return FileResponse(Path(__file__).parent / "ui" / "index.html")
