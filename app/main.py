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
def inventory(object: str = "Opportunity", mode: Mode = Depends(mode_dep)) -> dict[str, Any]:
    """Coverage receipt: every validation rule on the object with its latest status."""
    tracer = Tracer(mode=mode, rule=f"{object}.*", case_id="inventory")
    sf = MockSalesforce(tracer) if mode == "test" else LiveSalesforce(tracer)
    rules = sf.list_rules(object)
    latest: dict[str, dict] = {}
    for p in sorted(settings.traces_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if d["header"]["mode"] == mode and d.get("summary", {}).get("verdict") not in (None, "NOT_FOUND", "DUPLICATE"):
            latest[d["header"]["rule"]] = {"run_id": d["header"]["run_id"], "verdict": d["summary"]["verdict"]}
    status_map = {"PASS": "proven", "FAIL": "blocked", "AMBIGUOUS": "ambiguous", "ERROR": "error"}
    items = []
    for r in rules:
        key = f"{object}.{r['ValidationName']}"
        last = latest.get(key)
        items.append({"rule": key, "active": r["Active"], "description": r.get("Description") or "",
                      "status": status_map.get(last["verdict"], "error") if last else "not_attempted", "last_run": last})
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    tracer.finalize({"verdict": None, "inventory": counts})
    return {"object": object, "mode": mode, "rules": items, "counts": counts}


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
