"""Phase 2 pipeline branches: formula fields (transformation) and record-triggered flows (condition + assignment).

Both reuse the Pipeline's helpers (tracing, Discord, audit record, bounded diff, claims) and differ only in how the
source is read, how the proposal is verified, and what is written:

  FormulaField  source = Metadata XML formula + return type + blank handling
                verify = check_transformation (numeric) or check_equivalence (checkbox formula)
                write  = Airtable formula field computing the same value; then a BEHAVIOURAL probe: create a record with
                         known inputs, read Airtable's computed value, compare with the reference interpreter on the
                         Salesforce formula, delete the probe. Live reads Airtable's real computation.
  Flow          source = entry conditions AND decision outcome (lowered to Node by inventory)
                verify = check_equivalence against the proposed IF(cond, 1, 0) formula
                write  = when the flow's only action assigns a field on the same record, a computed formula field is a
                         behaviourally equivalent, API-writable reconfiguration (PASS). Any other action (email, create
                         record, subflow) cannot be written through the API: the proven condition plus the action spec
                         are recorded and the verdict is PARTIAL.
"""
from __future__ import annotations

from typing import Any, Optional

from .adapters.airtable import OPP_TABLE, RULES_TABLE
from .inventory import Artefact
from .models import Claim, FieldSpec, Node, Proposal, RuleIR, RuleSource, SolverResult, Verdict
from .parsers.at_formula import parse_at_formula
from .parsers.numeric import parse_numeric
from .parsers.sf_formula import parse_sf_formula
from .reference import evaluate, evaluate_expr
from .solver import SchemaMismatch, check_equivalence, check_transformation

PROBE_INPUTS = {"Amount": 1000, "Discount__c": 0.1, "Probability": 0.3, "StageName": "Negotiation/Review", "Type": "New Customer"}


def node_to_sf_text(n: Node) -> str:
    """Render a lowered condition as Salesforce-style text for the LLM prompt (it never reaches the solver)."""
    if n.op in ("true", "false"):
        return "TRUE" if n.op == "true" else "FALSE"
    if n.op == "and":
        return "(" + " && ".join(node_to_sf_text(a) for a in n.args) + ")"
    if n.op == "or":
        return "(" + " || ".join(node_to_sf_text(a) for a in n.args) + ")"
    if n.op == "not":
        return "NOT(" + node_to_sf_text(n.args[0]) + ")"
    if n.op == "is_blank":
        return f"ISBLANK({n.field})"
    sym = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[n.op]
    v = f'"{n.value}"' if isinstance(n.value, str) else str(n.value)
    if isinstance(n.value, str) and n.op == "eq":
        return f'ISPICKVAL({n.field}, {v})'
    return f"{n.field} {sym} {v}"


def run_formula(p, req, art: Artefact) -> Any:
    """Formula-field branch. `p` is the Pipeline (for helpers, adapters, tracer)."""
    t = p.t
    d = art.details
    source = RuleSource(object=art.object or "", name=art.name, formula=d["formula"], active=True, description=art.description,
                        error_message="", source_id=art.key)
    with t.span("step", name="describe_fields"):
        sf_fields = p.sf.describe_fields(art.object)
    is_bool = d["returnType"] == "Checkbox"
    result_scale = 100.0 if d["returnType"] == "Percent" else 1.0
    blanks = d.get("formulaTreatBlanksAs") or "BlankAsBlank"
    if is_bool:
        out = parse_sf_formula(d["formula"], sf_fields)
        cond, expr, uns, err = out.node, None, out.unsupported, out.error
    else:
        out = parse_numeric(d["formula"], sf_fields, "sf")
        cond, expr, uns, err = None, out.expr, out.unsupported, out.error
    ir = RuleIR(source=source, fields=sf_fields, condition=cond, unsupported=uns, parse_error=err)
    t.add("rule_ir", ir=ir.model_dump(mode="json", exclude={"fields"}), expr=expr.model_dump(mode="json") if expr else None,
          artefact_type="FormulaField", return_type=d["returnType"], blanks_as=blanks, result_scale=result_scale)
    if cond is None and expr is None:
        why = f"unsupported constructs: {uns}" if uns else f"parse error: {err}"
        return p.ambiguous_before_proposal(req, source, ir, why)

    at_schema, at_fields, before, dup = p.prepare_target(source)
    if dup is not None:
        return dup
    proposal = p.proposer.propose(ir, at_fields, req.cassette or (req.case_id or art.name), variant=req.prompt_variant, kind="formula")
    target_name = proposal.guard_field_name or art.label or art.name
    p._post("propose", "LLM proposal (unverified)", f"```{proposal.guard_formula}```", [("llm confidence", f"{proposal.confidence:.2f}"), ("target field", target_name)])
    field_map = {m.target_field: m.source_field for m in proposal.field_mapping}

    solver: Optional[SolverResult]
    if is_bool:
        at_out = parse_at_formula(proposal.guard_formula, at_fields)
        target_expr = None
    else:
        at_out = parse_numeric(proposal.guard_formula, at_fields, "at")
        target_expr = at_out.expr
    if (is_bool and at_out.node is None) or (not is_bool and target_expr is None):
        solver, decision = p.unverifiable(proposal, at_out.unsupported, at_out.error)
    else:
        try:
            if is_bool:
                solver = check_equivalence(cond, sf_fields, at_out.node, at_fields, field_map)
            else:
                solver = check_transformation(expr, sf_fields, blanks, result_scale, target_expr, at_fields, field_map, 1.0)
            t.add("solver", **solver.model_dump(mode="json"))
        except SchemaMismatch as e:
            solver = SolverResult(status="ambiguous", reason=f"schema mismatch: {e}")
            t.add("solver", **solver.model_dump(mode="json"))
        decision = p.decide_from_solver(solver, proposal, kind="formula")

    expected_fields: list[str] = []
    if decision.verdict == Verdict.PASS:
        res = p._ensure_formula_field(at_schema, target_name, proposal.guard_formula,
                                      f"OffBoard: computes Salesforce formula field {art.key} ({d['formula']}). Proven equivalent by Z3 in run {t.run_id}.")
        expected_fields = [target_name] if res.get("created") else []
        # behavioural probe: Airtable's own computation vs the reference interpreter on the Salesforce formula
        probe_fields = {"Name": f"PROBE {t.run_id}"}
        inv = {v: k for k, v in field_map.items()}
        for sf_name, val in PROBE_INPUTS.items():
            if sf_name in inv and inv[sf_name] in at_fields:
                probe_fields[inv[sf_name]] = val * (1 if sf_fields[sf_name].type != "percent" else 1)  # canonical == Airtable units
        rec = p.at.create_record(OPP_TABLE, probe_fields)
        back = p.at.get_record(OPP_TABLE, rec["id"])
        got = back["fields"].get(target_name)
        canon = {k: v for k, v in PROBE_INPUTS.items() if k in sf_fields}
        if is_bool:
            want: Any = evaluate(cond, canon, sf_fields, "sf")
            ok = (got in (1, "1", True, "VIOLATION")) == bool(want)
        else:
            raw = evaluate_expr(expr, canon, sf_fields, "sf", blanks)
            want = None if raw is None else raw / result_scale
            ok = (got is None and want is None) or (got is not None and want is not None and abs(float(got) - want) < 1e-6)
        p.claims.append(Claim(kind="behavioural_check", detail=f"probe record {rec['id']}: Airtable computed {got!r}, Salesforce formula gives {want!r}", verified=ok,
                              evidence_seq=t.entries[-1]["seq"]))
        p.at.delete_record(OPP_TABLE, rec["id"])
        p.claims.append(Claim(kind="probe_deleted", detail=f"probe record {rec['id']} deleted", verified=None))
        if not ok:
            decision = p._decide(Verdict.FAIL, 0.1, [f"behavioural check failed: Airtable computed {got!r}, expected {want!r}"], proposal, solver)
    p._write_audit(source, ir, proposal, solver, decision, formula_override=proposal.guard_formula)
    return p._finish(req, decision, before, at_schema, expected_fields, 1)


def run_flow(p, req, art: Artefact) -> Any:
    """Record-triggered flow branch: entry conditions AND one decision outcome -> computed field or PARTIAL spec."""
    t = p.t
    d = art.details
    entry = Node.model_validate(art.condition) if art.condition else Node(op="true")
    outcomes = [x for x in d.get("decisions", []) if x.get("condition")]
    actions = d.get("actions", [])
    cond = Node(op="and", args=[entry] + [Node.model_validate(outcomes[0]["condition"])]) if outcomes else entry
    formula_text = node_to_sf_text(cond)
    source = RuleSource(object=art.object or "", name=art.name, formula=formula_text, active=bool(art.active), description=art.description, source_id=art.key)
    with t.span("step", name="describe_fields"):
        sf_fields = p.sf.describe_fields(art.object)
    ir = RuleIR(source=source, fields=sf_fields, condition=cond)
    t.add("rule_ir", ir=ir.model_dump(mode="json", exclude={"fields"}), artefact_type="Flow", trigger=d.get("triggerType"),
          decisions=d.get("decisions"), actions=actions)
    simple_assign = len(actions) == 1 and actions[0]["kind"] == "assign" and actions[0]["operator"] == "Assign" and actions[0]["value"] is True \
        and len(outcomes) <= 1
    at_schema, at_fields, before, dup = p.prepare_target(source)
    if dup is not None:
        return dup
    proposal = p.proposer.propose(ir, at_fields, req.cassette or (req.case_id or art.name), variant=req.prompt_variant, kind="flow")
    target_name = proposal.guard_field_name or (actions[0]["field"].replace("__c", "").replace("_", " ") if actions else art.label)
    p._post("propose", "LLM proposal (unverified)", f"```{proposal.guard_formula}```", [("llm confidence", f"{proposal.confidence:.2f}"), ("target field", target_name)])
    field_map = {m.target_field: m.source_field for m in proposal.field_mapping}
    at_out = parse_at_formula(proposal.guard_formula, at_fields)
    if at_out.node is None:
        solver, decision = p.unverifiable(proposal, at_out.unsupported, at_out.error)
    else:
        try:
            solver = check_equivalence(cond, sf_fields, at_out.node, at_fields, field_map)
            t.add("solver", **solver.model_dump(mode="json"))
        except SchemaMismatch as e:
            solver = SolverResult(status="ambiguous", reason=f"schema mismatch: {e}")
            t.add("solver", **solver.model_dump(mode="json"))
        decision = p.decide_from_solver(solver, proposal, kind="flow")
    expected_fields: list[str] = []
    if decision.verdict == Verdict.PASS:
        if simple_assign:
            res = p._ensure_formula_field(at_schema, target_name, proposal.guard_formula,
                                          f"OffBoard: computed equivalent of Salesforce flow {art.name} (sets {actions[0]['field']}). Condition proven equivalent by Z3 in run {t.run_id}.")
            expected_fields = [target_name] if res.get("created") else []
        else:
            spec = {"trigger_condition": proposal.guard_formula, "actions": actions}
            p.claims.append(Claim(kind="automation_spec", detail=f"condition proven; actions {[(a['kind'], a.get('field') or a.get('name')) for a in actions]} need an Airtable automation (no API): spec recorded", verified=True))
            decision = p._decide(Verdict.PARTIAL, 0.6, ["trigger condition proven equivalent", "actions have no API-writable equivalent in Airtable; automation spec recorded for a human"], proposal, solver)
            t.add("note", message="automation spec", spec=spec)
    p._write_audit(source, ir, proposal, solver, decision, formula_override=proposal.guard_formula)
    return p._finish(req, decision, before, at_schema, expected_fields, 1)
