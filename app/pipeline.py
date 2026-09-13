"""The seven-step pipeline: extract, parse, (duplicate check), propose, verify, decide, write, report.

Principles (skills/plan.md 2.1, skills/problem-domain.md 6.3):
- the LLM is never in the verification path; parser + solver decide
- "cannot encode" is a first-class result (AMBIGUOUS), never a pass
- every semantic assumption is written to the trace with its evidence
- state is snapshotted before and after; the diff is graded; the final summary is structured claims that
  are checked against the post-state
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters.airtable import OPP_TABLE, RULES_TABLE, AirtableAdapter, AirtableError
from .adapters.discord import DiscordAdapter
from .adapters.salesforce import SalesforceAdapter
from .llm import Proposer
from .models import Claim, Decision, FieldSpec, Proposal, RuleIR, RunRequest, RunSummary, SolverResult, Verdict
from .parsers.at_formula import parse_at_formula
from .parsers.sf_formula import build_rule_ir
from .solver import SchemaMismatch, check_equivalence
from .trace import Tracer

ASSUMPTIONS = [
    ("sf_blank_number_is_null", "In a Salesforce validation rule a blank number is null and any numeric comparison on it is false.",
     "Dev org probe 2026-09-13 (scripts/seed_salesforce.py): blank Amount saved under `Amount <= 0`, Amount 0 rejected.", True),
    ("at_blank_number_is_zero", "In Airtable a blank number behaves as 0 in comparisons.",
     "Real base probe 2026-09-13 (scripts/smoke_airtable_naive.py): `{Amount}<=0` matched the blank record, `{Amount}<0` did not.", True),
    ("sf_percent_scale_100", "Salesforce percent fields store 50 for 50%; formula literals are in the same units.",
     "Opportunity describe: Probability type percent precision 3 scale 0; Salesforce field-type docs.", True),
    ("at_percent_scale_1", "Airtable percent fields store 0.5 for 50%.", "Airtable field model docs; base schema percent precision 2.", True),
    ("picklist_eq_on_blank_false", "ISPICKVAL / select equality on a blank value is false in both systems; <> is its negation.",
     "Salesforce formula docs; Airtable formula behaviour. Not probed empirically.", False),
]


class Pipeline:
    def __init__(self, tracer: Tracer, sf: SalesforceAdapter, at: AirtableAdapter, discord: DiscordAdapter, proposer: Proposer,
                 mode: str):
        self.t, self.sf, self.at, self.discord, self.proposer, self.mode = tracer, sf, at, discord, proposer, mode
        self.claims: list[Claim] = []

    # ------------------------------------------------------------------ helpers
    def _post(self, step: str, title: str, description: str = "", fields: Optional[list[tuple[str, str]]] = None) -> None:
        self.discord.post(f"receipt · {step} · {self.t.header['rule']}", description, fields, footer=f"run {self.t.run_id} · mode {self.mode}")

    def _decide(self, verdict: Verdict, system_confidence: float, reasons: list[str], proposal: Optional[Proposal], solver: Optional[SolverResult]) -> Decision:
        llm_conf = proposal.confidence if proposal else None
        truth = 1.0 if (solver and solver.status == "equivalent") else 0.0
        gap = max(0.0, llm_conf - truth) if llm_conf is not None else None
        d = Decision(verdict=verdict, system_confidence=round(system_confidence, 2), llm_confidence=llm_conf,
                     overconfidence_gap=None if gap is None else round(gap, 2), reasons=reasons)
        self.t.add("decision", **d.model_dump(mode="json"))
        return d

    def _diff(self, before: dict, after: dict) -> dict[str, Any]:
        diff: dict[str, Any] = {"fields_added": {}, "records_added": {}, "records_changed": {}, "records_removed": {}}
        for table in set(before) | set(after):
            b, a = before.get(table, {"fields": [], "records": {}}), after.get(table, {"fields": [], "records": {}})
            fa = sorted(set(a["fields"]) - set(b["fields"]))
            if fa:
                diff["fields_added"][table] = fa
            ra = sorted(set(a["records"]) - set(b["records"]))
            if ra:
                diff["records_added"][table] = ra
            rr = sorted(set(b["records"]) - set(a["records"]))
            if rr:
                diff["records_removed"][table] = rr
            ch = {rid: {"before": b["records"][rid], "after": a["records"][rid]} for rid in set(a["records"]) & set(b["records"])
                  if _norm(a["records"][rid]) != _norm(b["records"][rid])}
            if ch:
                diff["records_changed"][table] = ch
        return diff

    # ------------------------------------------------------------------ run
    def run(self, req: RunRequest) -> RunSummary:
        t = self.t
        obj, _, name = req.rule.partition(".")
        for a in ASSUMPTIONS:
            t.assumption(*a)
        proposal: Optional[Proposal] = None
        solver: Optional[SolverResult] = None
        ir: Optional[RuleIR] = None
        at_schema = None
        before: dict = {}
        expected_fields: list[str] = []
        expected_records = 0
        verdict: Verdict
        decision: Decision
        guard_field_result: Optional[dict] = None
        rule_record: Optional[dict] = None

        try:
            # [1] extract ---------------------------------------------------------------
            with t.span("step", name="extract"):
                source = self.sf.get_rule(obj, name)
            if source is None:
                self._post("extract", "rule not found", f"`{req.rule}` is not a validation rule on {obj}. No LLM call, no write.")
                decision = self._decide(Verdict.NOT_FOUND, 0.0, [f"no validation rule named {name!r} on {obj}"], None, None)
                self.claims.append(Claim(kind="no_write", detail="nothing was written to Airtable"))
                return self._finish(req, decision, before, {}, expected_fields, expected_records)
            self._post("extract", "rule extracted", f"```{source.formula}```", [("active", str(source.active)), ("intent", source.description[:200] or "(none)")])

            # [2] parse -----------------------------------------------------------------
            with t.span("step", name="describe_fields"):
                sf_fields = self.sf.describe_fields(obj)
            ir = build_rule_ir(source, sf_fields)
            t.add("rule_ir", ir=ir.model_dump(mode="json", exclude={"fields"}), fields_used=sorted(ir.condition.fields()) if ir.condition else [])
            if ir.condition is None:
                why = f"unsupported constructs: {ir.unsupported}" if ir.unsupported else f"parse error: {ir.parse_error}"
                self._post("parse", "AMBIGUOUS · cannot encode this rule", why, [("needs", "human review")])
                decision = self._decide(Verdict.AMBIGUOUS, 0.0, [why, "rule cannot be encoded for verification; surfaced instead of guessed"], None, None)
                at_schema = self.at.schema(); before = self.at.snapshot(at_schema)
                t.add("state_snapshot", phase="before", tables={k: {"fields": v["fields"], "record_count": len(v["records"])} for k, v in before.items()}, full=before)
                rule_record = self._write_audit(source, ir, None, None, decision)
                expected_records = 1
                return self._finish(req, decision, before, at_schema, expected_fields, expected_records)

            # [3] target schema + duplicate check -----------------------------------------
            with t.span("step", name="target_schema"):
                at_schema = self.at.schema()
                at_fields = self.at.table_fields(at_schema, OPP_TABLE)
            before = self.at.snapshot(at_schema)
            t.add("state_snapshot", phase="before", tables={k: {"fields": v["fields"], "record_count": len(v["records"])} for k, v in before.items()},
                  full=before)
            existing = self.at.list_records(RULES_TABLE, formula=f'{{Rule}} = "{source.name}"')
            done = [r for r in existing if r["fields"].get("Verdict") == "PASS"]
            if done:
                self._post("idempotency", "DUPLICATE · already migrated", f"{RULES_TABLE} already holds a PASS record for `{source.name}` ({done[0]['id']}). No write.")
                decision = self._decide(Verdict.DUPLICATE, 1.0, [f"rule already migrated in {RULES_TABLE} record {done[0]['id']}"], None, None)
                self.claims.append(Claim(kind="no_write", detail="nothing was written to Airtable"))
                return self._finish(req, decision, before, at_schema, expected_fields, expected_records)

            # [4] propose ---------------------------------------------------------------
            cassette_key = req.cassette or (req.case_id or source.name)
            proposal = self.proposer.propose(ir, at_fields, cassette_key, variant=req.prompt_variant)
            # the guard field NAME is a convention, not a claim to verify: fix it from the rule so reruns are idempotent
            if proposal.guard_field_name != f"Guard: {source.name}":
                t.note(f"guard field name normalised from {proposal.guard_field_name!r} to 'Guard: {source.name}'", llm_suggested_name=proposal.guard_field_name)
                proposal = proposal.model_copy(update={"guard_field_name": f"Guard: {source.name}"})
            self._post("propose", "LLM proposal (unverified)", f"```{proposal.guard_formula}```",
                       [("llm confidence", f"{proposal.confidence:.2f}"), ("intent", proposal.intent_statement[:300])])

            # [5] verify ----------------------------------------------------------------
            at_out = parse_at_formula(proposal.guard_formula, at_fields)
            field_map = {m.target_field: m.source_field for m in proposal.field_mapping}
            if at_out.node is None:
                missing = [u[len("field:"):] for u in at_out.unsupported if u.startswith("field:")]
                if missing:
                    why = f"schema mismatch: proposal references target field(s) {missing} that do not exist in the Airtable table"
                    conf = 0.0
                else:
                    why = f"proposal cannot be verified: {at_out.unsupported or at_out.error}"
                    conf = 0.2
                solver = SolverResult(status="ambiguous", reason=why)
                t.add("solver", **solver.model_dump(mode="json"))
                self._post("verify", "AMBIGUOUS · proposal not verifiable", why, [("needs", "human review")])
                decision = self._decide(Verdict.AMBIGUOUS, conf, [why], proposal, solver)
            else:
                try:
                    t0 = time.perf_counter()
                    solver = check_equivalence(ir.condition, sf_fields, at_out.node, at_fields, field_map)
                    t.add("solver", **solver.model_dump(mode="json"))
                except SchemaMismatch as e:
                    solver = SolverResult(status="ambiguous", reason=f"schema mismatch: {e}", elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
                    t.add("solver", **solver.model_dump(mode="json"))
                if solver.status == "equivalent":
                    self._post("verify", "solver: EQUIVALENT (unsat)", solver.reason, [("z3", solver.z3_result), ("unsat core", ", ".join(solver.unsat_core))])
                    decision = self._decide(Verdict.PASS, 0.95, ["solver proved the guard equivalent to the source rule for every record",
                                                                  f"Airtable guard is advisory: a formula flag, bypassable by API and import (Airtable has no validation rules)"], proposal, solver)
                elif solver.status == "not_equivalent":
                    self._post("verify", "solver: NOT EQUIVALENT (sat)", solver.reason,
                               [("counterexample", json.dumps(solver.counterexample)), ("llm confidence", f"{proposal.confidence:.2f}")])
                    decision = self._decide(Verdict.FAIL, 0.10, [solver.reason, "write blocked: the proposal would change the rule's meaning"], proposal, solver)
                elif solver.status == "ambiguous":
                    self._post("verify", "AMBIGUOUS · schema cannot express the rule", solver.reason, [("needs", "human review")])
                    decision = self._decide(Verdict.AMBIGUOUS, 0.0, [solver.reason], proposal, solver)
                else:
                    self._post("verify", "solver error", solver.reason)
                    decision = self._decide(Verdict.AMBIGUOUS, 0.3, [solver.reason], proposal, solver)

            # [6] write -----------------------------------------------------------------
            if decision.verdict == Verdict.PASS:
                guard_field_result = self._ensure_guard_field(at_schema, proposal)
                expected_fields = [proposal.guard_field_name] if guard_field_result.get("created") else []
            rule_record = self._write_audit(source, ir, proposal, solver, decision)
            expected_records = 1
            return self._finish(req, decision, before, at_schema, expected_fields, expected_records)

        except Exception as e:  # noqa: BLE001 - recorded verbatim; the run reports ERROR rather than crashing silently
            t.add("error", type=type(e).__name__, message=str(e))
            self._post("error", f"run failed: {type(e).__name__}", str(e)[:1000])
            decision = self._decide(Verdict.ERROR, 0.0, [f"{type(e).__name__}: {e}"], proposal, solver)
            return self._finish(req, decision, before, at_schema, expected_fields, expected_records)

    # ------------------------------------------------------------------ write helpers
    def _ensure_guard_field(self, at_schema: dict, proposal: Proposal) -> dict:
        existing = {f["name"]: f for f in self.at.raw_table(at_schema, OPP_TABLE)["fields"]}
        if proposal.guard_field_name in existing:
            f = existing[proposal.guard_field_name]
            same = (f.get("options") or {}).get("formula") is not None
            self.claims.append(Claim(kind="field_exists", detail=f"{OPP_TABLE}.{proposal.guard_field_name} already existed ({f['id']}); not recreated"))
            return {"created": False, "id": f["id"], "existing_formula_present": same}
        f = self.at.create_field(OPP_TABLE, proposal.guard_field_name, "formula", {"formula": proposal.guard_formula},
                                 description=f"Receipt guard for Salesforce rule {self.t.header['rule']}. Verified equivalent by Z3 in run {self.t.run_id}.")
        self.claims.append(Claim(kind="field_created", detail=f"{OPP_TABLE}.{proposal.guard_field_name} created as formula field {f['id']}"))
        return {"created": True, "id": f["id"]}

    def _write_audit(self, source, ir: RuleIR, proposal: Optional[Proposal], solver: Optional[SolverResult], decision: Decision) -> dict:
        fields = {
            "Rule": source.name, "Source Object": source.object, "Source Formula": source.formula,
            "Guard Formula": (proposal.guard_formula if proposal and decision.verdict == Verdict.PASS else
                              (f"NOT WRITTEN · {proposal.guard_formula}" if proposal else "NOT WRITTEN · rule could not be encoded")),
            "Verdict": decision.verdict.value if decision.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.AMBIGUOUS) else "AMBIGUOUS",
            "System Confidence": decision.system_confidence,
            "Run ID": self.t.run_id,
            "Trace Link": f"https://github.com/sanalvre/offboard/blob/main/traces/{self.t.run_id}.json",
            "Migrated At": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        if decision.llm_confidence is not None:
            fields["LLM Confidence"] = decision.llm_confidence
        rec = self.at.create_record(RULES_TABLE, fields)
        back = self.at.get_record(RULES_TABLE, rec["id"])
        ok = back["fields"].get("Rule") == source.name and back["fields"].get("Verdict") == fields["Verdict"]
        self.claims.append(Claim(kind="record_written", detail=f"{RULES_TABLE} record {rec['id']} for {source.name} with verdict {fields['Verdict']}",
                                 verified=ok, evidence_seq=self.t.entries[-1]["seq"]))
        return back

    # ------------------------------------------------------------------ finish
    def _finish(self, req: RunRequest, decision: Decision, before: dict, at_schema: Any, expected_fields: list[str], expected_records: int) -> RunSummary:
        t = self.t
        diff: dict[str, Any] = {}
        unexpected: list[str] = []
        if before:
            after_schema = self.at.schema()
            after = self.at.snapshot(after_schema)
            diff = self._diff(before, after)
            t.add("state_snapshot", phase="after", tables={k: {"fields": v["fields"], "record_count": len(v["records"])} for k, v in after.items()},
                  full=after, diff=diff)
            # bounded-state invariant: only the intended changes
            for table, fs in diff["fields_added"].items():
                if table != OPP_TABLE or sorted(fs) != sorted(expected_fields):
                    unexpected.append(f"fields added in {table}: {fs}")
            for table, rs in diff["records_added"].items():
                if table != RULES_TABLE or len(rs) != expected_records:
                    unexpected.append(f"records added in {table}: {rs}")
            if diff["records_changed"] or diff["records_removed"]:
                unexpected.append(f"records changed/removed: {list(diff['records_changed'])} {list(diff['records_removed'])}")
            # verify claims against post-state
            for c in self.claims:
                if c.kind == "field_created":
                    c.verified = any(c.detail.split(".")[1].split(" created")[0] in fs for fs in diff["fields_added"].values())
                elif c.kind == "field_exists":
                    c.verified = True
                elif c.kind == "no_write":
                    c.verified = not diff["fields_added"] and not diff["records_added"] and not diff["records_changed"]
        else:
            for c in self.claims:
                if c.kind == "no_write":
                    c.verified = True
        if decision.verdict == Verdict.PASS:
            self.claims.append(Claim(kind="rule_verified_equivalent", detail="Z3 proved the written guard equivalent to the source rule",
                                     verified=any(e["kind"] == "solver" and e.get("status") == "equivalent" for e in t.entries)))
        elif decision.verdict in (Verdict.FAIL, Verdict.AMBIGUOUS):
            self.claims.append(Claim(kind="rule_blocked", detail="no guard field was written for this rule",
                                     verified=OPP_TABLE not in diff.get("fields_added", {})))
        unsupported = sum(1 for c in self.claims if c.verified is False)
        t.add("claims", claims=[c.model_dump(mode="json") for c in self.claims], unsupported_claims=unsupported, unexpected_changes=unexpected)
        headline = (f"**{decision.verdict.value}** · system confidence {decision.system_confidence:.2f}"
                    + (f" · LLM confidence {decision.llm_confidence:.2f}" if decision.llm_confidence is not None else ""))
        self._post("result", headline, "\n".join(f"• {c.kind}: {c.detail}" for c in self.claims)[:3900],
                   [("unsupported claims", f"{unsupported}/{len(self.claims)}"), ("unexpected state changes", str(len(unexpected)))])
        summary = {
            "verdict": decision.verdict.value, "decision": decision.model_dump(mode="json"),
            "claims": [c.model_dump(mode="json") for c in self.claims], "unsupported_claims": unsupported,
            "unexpected_changes": unexpected, "state_diff": diff, "airtable_calls": getattr(self.at, "calls", None),
        }
        doc = t.finalize(summary)
        return RunSummary(run_id=t.run_id, mode=self.mode, rule=req.rule, case_id=req.case_id, attempt=req.attempt, verdict=decision.verdict,
                          decision=decision, claims=self.claims, unsupported_claims=unsupported, trace_path=str(t.json_path),
                          canonical_hash=doc["canonical_hash"])


def _norm(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if not str(k).startswith("Guard:")}
