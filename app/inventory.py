"""Configuration inventory: every logic artefact in the org, from Metadata API XML, with an honest status.

Capture path: `sf project retrieve start --metadata Flow Workflow ApexTrigger ApexClass CustomObject:<obj> AssignmentRules
ApprovalProcess` into a scratch SFDX project (live), or the committed copy under fixtures/seed/metadata (mock).
Flow Builder is a canvas; the XML is the truth, so no browser is involved.

Each artefact gets:
  verifiable   whether OffBoard can encode it for the solver today (and if not, the reason)
  status       proven | blocked | ambiguous | not_attempted | not_verifiable, from the latest trace for its key
  key          the identifier the pipeline uses (Object.Rule, Object.Field__c for formulas, Flow.Name, ...)

Flow and assignment-rule conditions are lowered to the same Node IR as validation rules here, so the solver and
reference interpreter need no changes to verify them (skills/plan-phase2.md 2.1).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import settings
from .models import Node

NS = {"m": "http://soap.sforce.com/2006/04/metadata"}
SF_BIN = shutil.which("sf") or r"C:\Users\User\AppData\Roaming\npm\sf.cmd"
FORMULA_SUPPORTED_TYPES = {"Currency", "Number", "Percent", "Checkbox"}
FLOW_OPS = {"EqualTo": "eq", "NotEqualTo": "ne", "GreaterThan": "gt", "LessThan": "lt",
            "GreaterThanOrEqualTo": "gte", "LessThanOrEqualTo": "lte", "IsNull": "is_blank"}
ASSIGN_OPS = {"equals": "eq", "notEqual": "ne", "greaterThan": "gt", "lessThan": "lt",
              "greaterOrEqual": "gte", "lessOrEqual": "lte"}


@dataclass
class Artefact:
    type: str            # ValidationRule | FormulaField | Flow | WorkflowRule | WorkflowFieldUpdate | AssignmentRule | ApexTrigger | ApexClass | ApprovalProcess
    key: str             # pipeline identifier
    name: str
    object: Optional[str]
    label: str = ""
    description: str = ""
    active: Optional[bool] = None
    verifiable: bool = False
    reason: str = ""     # why not verifiable, or what is verifiable
    status: str = "not_attempted"
    last_run: Optional[dict] = None
    details: dict = field(default_factory=dict)
    condition: Optional[dict] = None  # Node as dict when lowered


def _t(el: Optional[ET.Element], tag: str, default: str = "") -> str:
    if el is None:
        return default
    x = el.find(f"m:{tag}", NS)
    return (x.text or "") if x is not None and x.text is not None else default


def _val(el: ET.Element) -> Any:
    """Flow <value>/<rightValue>: stringValue | numberValue | booleanValue | elementReference."""
    for tag, conv in (("stringValue", str), ("numberValue", float), ("booleanValue", lambda s: s == "true")):
        x = el.find(f"m:{tag}", NS)
        if x is not None:
            v = conv(x.text or "")
            return int(v) if isinstance(v, float) and v.is_integer() else v
    x = el.find("m:elementReference", NS)
    if x is not None:
        return {"ref": x.text}
    return None


# ------------------------------------------------------------------------------------ retrieval


def retrieve_live(objects: list[str], alias: str = settings.sf_cli_alias, max_age_s: int = 300) -> Path:
    """Retrieve into state/sfproj; reuse if fresher than max_age_s. Returns the default source dir."""
    proj = settings.state_dir / "sfproj"
    src = proj / "force-app" / "main" / "default"
    stamp = proj / ".retrieved_at"
    if stamp.exists() and time.time() - float(stamp.read_text()) < max_age_s and src.exists():
        return src
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "sfdx-project.json").write_text(json.dumps({"packageDirectories": [{"path": "force-app", "default": True}],
                                                         "namespace": "", "sourceApiVersion": settings.sf_api_version}), encoding="utf-8")
    # do not rmtree: OneDrive/Windows holds directory handles; `sf project retrieve` overwrites files in place
    src.mkdir(parents=True, exist_ok=True)
    md = ["Flow", "Workflow", "ApexTrigger", "ApexClass", "AssignmentRules", "ApprovalProcess"] + [f"CustomObject:{o}" for o in objects]
    p = subprocess.run([SF_BIN, "project", "retrieve", "start", "--metadata", *md, "--target-org", alias, "--json"],
                       cwd=proj, capture_output=True, text=True, encoding="utf-8")
    out = json.loads(p.stdout or "{}")
    if out.get("status") != 0:
        raise RuntimeError(f"metadata retrieve failed: {json.dumps(out)[:400]}")
    stamp.write_text(str(time.time()))
    return src


def seed_dir() -> Path:
    return settings.fixtures_dir / "seed" / "metadata"


# ------------------------------------------------------------------------------------ lowering to Node


def flow_condition_node(conditions: list[ET.Element], logic: str) -> tuple[Optional[Node], list[str]]:
    leaves: list[Node] = []
    unsupported: list[str] = []
    for c in conditions:
        left = _t(c, "leftValueReference") or _t(c, "field")
        op = _t(c, "operator")
        fld = left.split(".", 1)[1] if left.startswith("$Record.") else left
        if left.startswith("$") and not left.startswith("$Record."):
            unsupported.append(f"reference:{left}")
            continue
        if op not in FLOW_OPS:
            unsupported.append(f"operator:{op}")
            continue
        rv = c.find("m:rightValue", NS)
        rv = rv if rv is not None else c.find("m:value", NS)
        val = _val(rv) if rv is not None else None
        if isinstance(val, dict):
            unsupported.append(f"reference:{val['ref']}")
            continue
        if FLOW_OPS[op] == "is_blank":
            leaf = Node(op="is_blank", field=fld)
            leaves.append(leaf if val in (True, None) else Node(op="not", args=[leaf]))
        else:
            leaves.append(Node(op=FLOW_OPS[op], field=fld, value=val))
    if unsupported:
        return None, unsupported
    if not leaves:
        return Node(op="true"), []
    logic = (logic or "and").strip().lower()
    if logic == "and":
        return (leaves[0] if len(leaves) == 1 else Node(op="and", args=leaves)), []
    if logic == "or":
        return (leaves[0] if len(leaves) == 1 else Node(op="or", args=leaves)), []
    return None, [f"conditionLogic:{logic}"]  # custom logic strings like "1 AND (2 OR 3)" are phase 3


def assignment_criteria_node(items: list[ET.Element]) -> tuple[Optional[Node], list[str]]:
    leaves: list[Node] = []
    unsupported: list[str] = []
    for it in items:
        fld = _t(it, "field").split(".", 1)[-1]
        op = _t(it, "operation")
        raw = _t(it, "value")
        if op not in ASSIGN_OPS:
            unsupported.append(f"operation:{op}")
            continue
        vals = [v.strip() for v in raw.split(",")] if raw else [""]
        if ASSIGN_OPS[op] == "eq" and len(vals) > 1:
            leaves.append(Node(op="or", args=[Node(op="eq", field=fld, value=v) for v in vals]))
        elif ASSIGN_OPS[op] == "ne" and len(vals) > 1:
            leaves.append(Node(op="and", args=[Node(op="ne", field=fld, value=v) for v in vals]))
        else:
            v: Any = vals[0]
            try:
                v = float(v) if ASSIGN_OPS[op] not in ("eq", "ne") else v
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
            except ValueError:
                pass
            leaves.append(Node(op=ASSIGN_OPS[op], field=fld, value=v))
    if unsupported:
        return None, unsupported
    return (leaves[0] if len(leaves) == 1 else Node(op="and", args=leaves)), []


# ------------------------------------------------------------------------------------ parsing


def parse_metadata(src: Path) -> list[Artefact]:
    out: list[Artefact] = []
    # validation rules + fields per object
    for obj_dir in sorted((src / "objects").glob("*")) if (src / "objects").exists() else []:
        obj = obj_dir.name
        for f in sorted((obj_dir / "validationRules").glob("*.xml")):
            r = ET.parse(f).getroot()
            name = _t(r, "fullName")
            out.append(Artefact("ValidationRule", f"{obj}.{name}", name, obj, name, _t(r, "description"), _t(r, "active") == "true",
                                True, "boolean formula over record fields; verified by the phase 1 pipeline",
                                details={"formula": _t(r, "errorConditionFormula"), "errorMessage": _t(r, "errorMessage"),
                                         "errorDisplayField": _t(r, "errorDisplayField")}))
        for f in sorted((obj_dir / "fields").glob("*.xml")):
            r = ET.parse(f).getroot()
            formula = _t(r, "formula")
            if not formula:
                continue
            name = _t(r, "fullName")
            ftype = _t(r, "type")
            ok = ftype in FORMULA_SUPPORTED_TYPES
            out.append(Artefact("FormulaField", f"{obj}.{name}", name, obj, _t(r, "label"), _t(r, "description"), True, ok,
                                "numeric/boolean formula; verifiable as a transformation" if ok else f"formula returning {ftype}: text/date formulas are not verifiable in phase 2",
                                details={"formula": formula, "returnType": ftype, "scale": _t(r, "scale"), "precision": _t(r, "precision"),
                                         "formulaTreatBlanksAs": _t(r, "formulaTreatBlanksAs", "BlankAsBlank")}))
    # flows
    for f in sorted((src / "flows").glob("*.xml")) if (src / "flows").exists() else []:
        r = ET.parse(f).getroot()
        name = f.name.split(".flow")[0]
        ptype = _t(r, "processType")
        start = r.find("m:start", NS)
        trig = _t(start, "triggerType")
        obj = _t(start, "object") or None
        det: dict[str, Any] = {"processType": ptype, "triggerType": trig, "recordTriggerType": _t(start, "recordTriggerType"),
                               "status": _t(r, "status"), "decisions": [], "actions": []}
        reason, ok, cond = "", False, None
        if ptype != "AutoLaunchedFlow" or not trig.startswith("Record"):
            reason = f"{ptype or 'flow'} with trigger {trig or 'none'}: only record-triggered autolaunched flows are verifiable"
        else:
            node, uns = flow_condition_node(start.findall("m:filters", NS), _t(start, "filterLogic"))
            det["entry_unsupported"] = uns
            for d in r.findall("m:decisions", NS):
                for rule in d.findall("m:rules", NS):
                    n2, u2 = flow_condition_node(rule.findall("m:conditions", NS), _t(rule, "conditionLogic"))
                    det["decisions"].append({"decision": _t(d, "name"), "outcome": _t(rule, "name"), "label": _t(rule, "label"),
                                             "condition": n2.model_dump(mode="json") if n2 else None, "unsupported": u2,
                                             "target": _t(rule.find("m:connector", NS), "targetReference")})
            for a in r.findall("m:assignments", NS):
                for it in a.findall("m:assignmentItems", NS):
                    v = it.find("m:value", NS)
                    det["actions"].append({"kind": "assign", "name": _t(a, "name"), "field": _t(it, "assignToReference").replace("$Record.", ""),
                                           "operator": _t(it, "operator"), "value": _val(v) if v is not None else None})
            for u in r.findall("m:recordUpdates", NS):
                det["actions"].append({"kind": "recordUpdate", "name": _t(u, "name"), "inputReference": _t(u, "inputReference"),
                                       "assignments": [{"field": _t(ia, "field"), "value": _val(ia.find("m:value", NS))} for ia in u.findall("m:inputAssignments", NS)]})
            for ac in r.findall("m:actionCalls", NS):
                det["actions"].append({"kind": "actionCall", "name": _t(ac, "name"), "actionType": _t(ac, "actionType"), "actionName": _t(ac, "actionName")})
            bad = uns + [x for d in det["decisions"] for x in d["unsupported"]]
            ok = node is not None and not bad
            cond = node.model_dump(mode="json") if node else None
            reason = ("entry and decision conditions verifiable; actions captured as a spec (Airtable automations have no API)"
                      if ok else f"unsupported constructs in conditions: {bad}")
        out.append(Artefact("Flow", f"Flow.{name}", name, obj, _t(r, "label"), _t(r, "description"), _t(r, "status") == "Active", ok, reason, details=det, condition=cond))
    # workflows (rules + field updates + alerts)
    for f in sorted((src / "workflows").glob("*.xml")) if (src / "workflows").exists() else []:
        r = ET.parse(f).getroot()
        obj = f.name.split(".workflow")[0]
        for rule in r.findall("m:rules", NS):
            out.append(Artefact("WorkflowRule", f"Workflow.{obj}.{_t(rule, 'fullName')}", _t(rule, "fullName"), obj, _t(rule, "fullName"),
                                _t(rule, "description"), _t(rule, "active") == "true", False,
                                "Workflow Rules are retired; inventoried only. New orgs cannot create them.",
                                details={"formula": _t(rule, "formula"), "triggerType": _t(rule, "triggerType")}))
        for fu in r.findall("m:fieldUpdates", NS):
            out.append(Artefact("WorkflowFieldUpdate", f"Workflow.{obj}.{_t(fu, 'fullName')}", _t(fu, "fullName"), obj, _t(fu, "name"), "", None, False,
                                "field update action with no active rule referencing it; inventoried only",
                                details={"field": _t(fu, "field"), "literalValue": _t(fu, "literalValue"), "operation": _t(fu, "operation")}))
        for al in r.findall("m:alerts", NS):
            out.append(Artefact("WorkflowAlert", f"Workflow.{obj}.{_t(al, 'fullName')}", _t(al, "fullName"), obj, _t(al, "description"), "", None, False,
                                "email alert; inventoried only", details={"template": _t(al, "template")}))
    # assignment rules
    for f in sorted((src / "assignmentRules").glob("*.xml")) if (src / "assignmentRules").exists() else []:
        r = ET.parse(f).getroot()
        obj = f.name.split(".assignmentRules")[0]
        for ar in r.findall("m:assignmentRule", NS):
            entries = []
            bad: list[str] = []
            for i, e in enumerate(ar.findall("m:ruleEntry", NS), 1):
                node, uns = assignment_criteria_node(e.findall("m:criteriaItems", NS))
                bad += uns
                entries.append({"order": i, "assignedTo": _t(e, "assignedTo"), "assignedToType": _t(e, "assignedToType"),
                                "condition": node.model_dump(mode="json") if node else None, "unsupported": uns})
            out.append(Artefact("AssignmentRule", f"AssignmentRule.{obj}.{_t(ar, 'fullName')}", _t(ar, "fullName"), obj, _t(ar, "fullName"), "",
                                _t(ar, "active") == "true", not bad,
                                "criteria are verifiable conditions; assignee mapping captured as a spec" if not bad else f"unsupported: {bad}",
                                details={"entries": entries}))
    # apex + approval processes: inventoried, not verifiable
    for sub, typ in (("triggers", "ApexTrigger"), ("classes", "ApexClass")):
        for f in sorted((src / sub).glob("*.cls")) + sorted((src / sub).glob("*.trigger")) if (src / sub).exists() else []:
            body = f.read_text(encoding="utf-8", errors="replace")
            out.append(Artefact(typ, f"{typ}.{f.stem}", f.stem, None, f.stem, "", None, False,
                                "procedural code; not verifiable by the solver", details={"lines": body.count("\n") + 1}))
    for f in sorted((src / "approvalProcesses").glob("*.xml")) if (src / "approvalProcesses").exists() else []:
        out.append(Artefact("ApprovalProcess", f"ApprovalProcess.{f.stem}", f.stem, f.stem.split(".")[0], f.stem, "", None, False,
                            "multi-step approval; entry criteria could be lowered in a later phase"))
    return out


# ------------------------------------------------------------------------------------ status from traces


def attach_status(arts: list[Artefact], mode: str) -> list[Artefact]:
    latest: dict[str, dict] = {}
    for p in sorted(settings.traces_dir.glob("run_*.json"), key=lambda p: p.stat().st_mtime):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        v = d.get("summary", {}).get("verdict")
        if d["header"]["mode"] == mode and v not in (None, "NOT_FOUND", "DUPLICATE"):
            latest[d["header"]["rule"]] = {"run_id": d["header"]["run_id"], "verdict": v}
    smap = {"PASS": "proven", "FAIL": "blocked", "AMBIGUOUS": "ambiguous", "ERROR": "error", "PARTIAL": "partial"}
    for a in arts:
        if not a.verifiable:
            a.status = "not_verifiable"
            continue
        last = latest.get(a.key)
        a.last_run = last
        a.status = smap.get(last["verdict"], "error") if last else "not_attempted"
    return arts


def inventory(mode: str, objects: list[str] = ("Opportunity",)) -> dict[str, Any]:
    src = seed_dir() if mode == "test" else retrieve_live(list(objects))
    arts = attach_status(parse_metadata(src), mode)
    counts: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for a in arts:
        counts[a.status] = counts.get(a.status, 0) + 1
        by_type[a.type] = by_type.get(a.type, 0) + 1
    return {"mode": mode, "objects": list(objects), "source": str(src), "found": len(arts),
            "verifiable": sum(1 for a in arts if a.verifiable), "counts": counts, "by_type": by_type,
            "artefacts": [asdict(a) for a in arts]}
