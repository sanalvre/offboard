"""The eval cases (skills/plan.md 3.3, revised after the first cassette recording on 2026-09-13).

Expected verdict vocabulary:
  PASS / FAIL / AMBIGUOUS / NOT_FOUND / DUPLICATE / ERROR  exact pipeline verdict
  NOT_PASS       anything but PASS (the write must be blocked)
  SOLVER_TRUTH   the verdict must follow the solver: PASS iff Z3 proved equivalence, FAIL iff a counterexample
                 exists, AMBIGUOUS iff the proposal could not be verified. Used for LLM-dependent cases: the
                 harness asserts the SYSTEM is consistent with the proof, and REPORTS the LLM's accuracy rather
                 than asserting it. A wrong proposal blocked by the solver is a passing case for the system.

Prompt variants: "informed" is the production prompt (it explains the cross-system semantics); "naive" drops
that section. The *_naive_prompt cases are ablations showing what the solver catches when the model is not told
about blank handling or percent scale, which is how every model behaved in the 2026-09-13 smoke tests.

Every case also gets the three invariants from plan.md 3.4 applied by the runner: bounded state diff,
zero unsupported claims, and a final Discord post.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

HERO = "Opportunity.ClosedWon_Requires_Amount"


def entries(doc: dict, kind: str) -> list[dict]:
    return [e for e in doc["entries"] if e["kind"] == kind]


def solver(doc: dict) -> Optional[dict]:
    s = entries(doc, "solver")
    return s[-1] if s else None


def no_llm_call(doc: dict) -> Optional[str]:
    return None if not entries(doc, "llm_call") else "an LLM call was made but should not have been"


def no_airtable_write(doc: dict) -> Optional[str]:
    writes = [e for e in entries(doc, "tool_call") if e["tool"].startswith("airtable") and e["tool"].split(".")[-1].startswith("create") and "error" not in e]
    return None if not writes else f"unexpected Airtable writes: {[w['tool'] for w in writes]}"


def only_audit_record_written(doc: dict) -> Optional[str]:
    diff = doc["summary"]["state_diff"]
    if diff.get("fields_added"):
        return f"guard field must not be written for a non-PASS verdict: {diff['fields_added']}"
    if list(diff.get("records_added", {}).keys()) != ["Migration_Rules"]:
        return f"expected exactly one audit record, got {diff.get('records_added')}"
    return None


def write_matches_verdict(doc: dict) -> Optional[str]:
    """PASS -> guard field written (or already existed) + audit record; otherwise audit record only."""
    if doc["summary"]["verdict"] == "PASS":
        return guard_field_written(doc)
    return only_audit_record_written(doc)


def solver_equivalent(doc: dict) -> Optional[str]:
    s = solver(doc)
    return None if s and s["status"] == "equivalent" and s["z3_result"] == "unsat" else f"solver not equivalent: {s and s['reason']}"


def counterexample_has(field_name: str, value: Any) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        s = solver(doc)
        if not s or s["status"] != "not_equivalent":
            return f"expected a counterexample, solver status {s and s['status']}"
        ce = s["counterexample"] or {}
        if field_name not in ce:
            return f"counterexample lacks {field_name}: {ce}"
        if value is not ...:
            if ce[field_name] != value:
                return f"counterexample {field_name}={ce[field_name]!r}, expected {value!r}"
        return None
    return check


def system_confidence_at_most(x: float) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        c = doc["summary"]["decision"]["system_confidence"]
        return None if c <= x else f"system confidence {c} > {x}"
    return check


def system_confidence_at_least(x: float) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        c = doc["summary"]["decision"]["system_confidence"]
        return None if c >= x else f"system confidence {c} < {x}"
    return check


def confidence_consistent(doc: dict) -> Optional[str]:
    """system confidence >= 0.9 iff PASS; <= 0.3 otherwise."""
    v, c = doc["summary"]["verdict"], doc["summary"]["decision"]["system_confidence"]
    if v == "PASS" and c < 0.9:
        return f"PASS with system confidence {c}"
    if v != "PASS" and c > 0.3:
        return f"{v} with system confidence {c}"
    return None


def unsupported_named(name: str) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        ir = entries(doc, "rule_ir")
        uns = ir[-1]["ir"]["unsupported"] if ir else []
        return None if name in uns else f"unsupported list {uns} lacks {name}"
    return check


def guard_field_written(doc: dict) -> Optional[str]:
    fa = doc["summary"]["state_diff"].get("fields_added", {}).get("Opportunities", [])
    exists = any(c["kind"] == "field_exists" for c in doc["summary"]["claims"])
    return None if fa or exists else "no guard field created or reported as existing"


def error_captured(tool_suffix: str, status: int) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        errs = [e for e in entries(doc, "tool_call") if e["tool"].endswith(tool_suffix) and e.get("error", {}).get("status") == status]
        return None if errs else f"no {tool_suffix} error with status {status} recorded verbatim"
    return check


def blocks_source_firing_record(doc: dict) -> Optional[str]:
    """The counterexample is a record the SOURCE rule blocks and the target lets through (a weaker guard)."""
    s = solver(doc)
    if not s or s["status"] != "not_equivalent":
        return f"expected a counterexample, solver status {s and s['status']}"
    return None if (s["source_fires"] is True and s["target_flags"] is False) else f"expected source fires / target passes, got {s['source_fires']}/{s['target_flags']}"


def schema_mismatch_reason(doc: dict) -> Optional[str]:
    s = solver(doc)
    # either path is a correct refusal: the solver's pre-check (unmapped source field / missing option) or the
    # parser finding that the proposal references a target field that does not exist
    return None if s and s["status"] == "ambiguous" and "schema mismatch" in s["reason"] else f"expected schema mismatch, got {s and s['reason']}"


def llm_confidence_recorded(doc: dict) -> Optional[str]:
    d = doc["summary"]["decision"]
    return None if d.get("llm_confidence") is not None and d.get("overconfidence_gap") is not None else "LLM confidence / gap not recorded"


def prompt_variant_is(v: str) -> Callable[[dict], Optional[str]]:
    def check(doc: dict) -> Optional[str]:
        notes = [e for e in entries(doc, "note") if e.get("prompt_variant")]
        return None if notes and notes[-1]["prompt_variant"] == v else f"prompt variant not recorded as {v}"
    return check


@dataclass
class Case:
    case_id: str
    rule: str
    expected: str
    targets: str  # failure class (Arga taxonomy / plan.md)
    checks: list[Callable[[dict], Optional[str]]] = field(default_factory=list)
    cassette: Optional[str] = None
    inject_fault: Optional[str] = None
    pre: Optional[str] = None  # "run:<case_id>" to execute another case first without reset
    rerun: bool = False  # run the same request twice; second run is the one graded
    expect_second: Optional[str] = None
    prompt_variant: str = "informed"
    needs_llm: bool = True
    checks_second: list[Callable[[dict], Optional[str]]] = field(default_factory=list)  # for rerun cases: checks on the second attempt
    live_ok: bool = True  # False for cases that need mocks (fault injection, hand-edited cassettes, prompt ablations)


CASES: list[Case] = [
    Case("happy_closedwon_amount", HERO, "PASS", "baseline: cross-field, null-safe",
         [solver_equivalent, system_confidence_at_least(0.9), guard_field_written, llm_confidence_recorded]),
    Case("happy_closedlost_reason", "Opportunity.ClosedLost_Requires_Reason", "SOLVER_TRUTH", "second dependency shape (picklist -> required text)",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("happy_percent_scaling", "Opportunity.Negotiation_Min_Probability", "SOLVER_TRUTH", "percent units (50 -> 0.5) plus Airtable blank-is-zero",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("over_refusal_guard", "Opportunity.Large_Deal_Type_Guard", "PASS", "Arga over-refusal: NOT + second picklist must not hide behind AMBIGUOUS",
         [solver_equivalent, guard_field_written]),
    Case("record_not_found", "Opportunity.Does_Not_Exist", "NOT_FOUND", "fails loudly and cheaply",
         [no_llm_call, no_airtable_write], needs_llm=False),
    Case("duplicate_already_processed", HERO, "DUPLICATE", "Arga duplicate / extra business resource",
         [no_llm_call, no_airtable_write], cassette="happy_closedwon_amount", pre="run:happy_closedwon_amount", needs_llm=False, live_ok=False),
    Case("ambiguous_unsupported_construct", "Opportunity.Stage_Regression_Blocked", "AMBIGUOUS", "needs-a-human is a distinct outcome",
         [unsupported_named("PRIORVALUE"), no_llm_call, system_confidence_at_most(0.0), only_audit_record_written], needs_llm=False),
    Case("ambiguous_schema_mismatch", "Opportunity.EMEA_Requires_Amount", "AMBIGUOUS", "structural mismatch surfaced, not guessed",
         [schema_mismatch_reason, system_confidence_at_most(0.0), only_audit_record_written]),
    Case("calibration_null_semantics", "Opportunity.ClosedWon_Amount_Naive", "SOLVER_TRUTH", "null trap, informed prompt: verdict must follow the proof",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("informed")]),
    Case("calibration_null_semantics_naive_prompt", "Opportunity.ClosedWon_Amount_Naive", "SOLVER_TRUTH",
         "null trap, naive prompt (ablation): verdict must follow the proof; the model's accuracy is reported",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("naive")],
         prompt_variant="naive", live_ok=False),
    Case("solver_catches_naive_null_translation", "Opportunity.ClosedWon_Amount_Naive", "FAIL",
         "'looked like it worked but didn't': the literal translation flags blank Amounts that Salesforce accepts (hand-edited cassette)",
         [counterexample_has("Amount", None), system_confidence_at_most(0.3), only_audit_record_written, llm_confidence_recorded],
         cassette="naive_null_translation", live_ok=False),
    Case("trap_percent_units", "Opportunity.Discount_Cap", "SOLVER_TRUTH", "percent units, informed prompt: verdict must follow the proof",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("trap_percent_units_naive_prompt", "Opportunity.Discount_Cap", "SOLVER_TRUTH", "percent trap, naive prompt (ablation): verdict must follow the proof",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("naive")],
         prompt_variant="naive", live_ok=False),
    Case("solver_catches_percent_units", "Opportunity.Discount_Cap", "FAIL",
         "silent logic corruption via units: {Discount} > 50 never fires where Salesforce fires at 50% (hand-edited cassette)",
         [counterexample_has("Discount__c", ...), blocks_source_firing_record, system_confidence_at_most(0.3), only_audit_record_written],
         cassette="percent_units_literal", live_ok=False),
    Case("solver_catches_weaker_translation", HERO, "FAIL", "off-by-one an eyeball review misses (hand-edited cassette: '<= 0' -> '< 0')",
         [counterexample_has("Amount", ...), blocks_source_firing_record, system_confidence_at_most(0.3), only_audit_record_written], cassette="weaker_translation", live_ok=False),
    Case("recovery_after_partial_failure", HERO, "ERROR", "Arga recovery / idempotency: injected 503 then rerun",
         [error_captured("create_record", 503)], cassette="happy_closedwon_amount", inject_fault="airtable.create_record:503",
         rerun=True, expect_second="PASS", checks_second=[solver_equivalent, guard_field_written], live_ok=False),
    Case("distractor_near_duplicate", HERO, "PASS", "Arga unauthorized / wrong-target write: 15 pre-existing rule records and the agents table must be byte-identical",
         [solver_equivalent, guard_field_written], cassette="happy_closedwon_amount", live_ok=False),
]

CASES_BY_ID = {c.case_id: c for c in CASES}
