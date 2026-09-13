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


def behavioural_check_verified(doc: dict) -> Optional[str]:
    """On PASS a probe record was created, Airtable's computed value matched the reference interpreter, probe deleted."""
    if doc["summary"]["verdict"] != "PASS":
        return None
    cl = {c["kind"]: c for c in doc["summary"]["claims"]}
    if "behavioural_check" not in cl:
        return "no behavioural check claim on a PASS"
    if cl["behavioural_check"]["verified"] is not True:
        return f"behavioural check not verified: {cl['behavioural_check']['detail']}"
    if cl.get("probe_deleted", {}).get("verified") is not True:
        return "probe record not confirmed deleted"
    return None


def outputs_recorded(doc: dict) -> Optional[str]:
    s = solver(doc)
    if not s or s["status"] != "not_equivalent":
        return f"expected a transformation counterexample, got {s and s['status']}"
    o = s.get("outputs") or {}
    return None if "source" in o and "target" in o and o["source"] != o["target"] else f"outputs missing or equal: {o}"


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
    Case("happy_closedwon_amount", HERO, "PASS", "A clean rule that should migrate: Closed Won deals need an amount. Proves the happy path end to end.",
         [solver_equivalent, system_confidence_at_least(0.9), guard_field_written, llm_confidence_recorded]),
    Case("happy_closedlost_reason", "Opportunity.ClosedLost_Requires_Reason", "SOLVER_TRUTH", "A different shape of rule: a text field becomes required at a certain stage. Checks the system is not tuned to one example.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("happy_percent_scaling", "Opportunity.Negotiation_Min_Probability", "SOLVER_TRUTH", "Percent stored as 50 in Salesforce and 0.5 in Airtable, plus a blank value. Two ways to get it subtly wrong at once.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("over_refusal_guard", "Opportunity.Large_Deal_Type_Guard", "PASS", "A valid, slightly unusual rule. The system must migrate it rather than play safe and hand it to a human.",
         [solver_equivalent, guard_field_written]),
    Case("record_not_found", "Opportunity.Does_Not_Exist", "NOT_FOUND", "A rule that does not exist. Must stop immediately: no model call, nothing written.",
         [no_llm_call, no_airtable_write], needs_llm=False),
    Case("duplicate_already_processed", HERO, "DUPLICATE", "Running the same rule twice. The second run must notice it was already migrated and write nothing.",
         [no_llm_call, no_airtable_write], cassette="happy_closedwon_amount", pre="run:happy_closedwon_amount", needs_llm=False, live_ok=False),
    Case("ambiguous_unsupported_construct", "Opportunity.Stage_Regression_Blocked", "AMBIGUOUS", "A rule using a construct that cannot be translated (PRIORVALUE). Must be flagged for a human, not guessed.",
         [unsupported_named("PRIORVALUE"), no_llm_call, system_confidence_at_most(0.0), only_audit_record_written], needs_llm=False),
    Case("ambiguous_schema_mismatch", "Opportunity.EMEA_Requires_Amount", "AMBIGUOUS", "The rule uses a field Airtable does not have. Must be flagged before anything is written.",
         [schema_mismatch_reason, system_confidence_at_most(0.0), only_audit_record_written]),
    Case("calibration_null_semantics", "Opportunity.ClosedWon_Amount_Naive", "SOLVER_TRUTH", "The blank-amount trap with the production prompt. Whatever the model does, the verdict must match the proof.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("informed")]),
    Case("calibration_null_semantics_naive_prompt", "Opportunity.ClosedWon_Amount_Naive", "SOLVER_TRUTH",
         "Same trap, but the model is not told about blank handling. Shows what the solver catches when the prompt is weaker.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("naive")],
         prompt_variant="naive", live_ok=False),
    Case("solver_catches_naive_null_translation", "Opportunity.ClosedWon_Amount_Naive", "FAIL",
         "The obvious word-for-word translation, forced. It flags blank amounts Salesforce accepts; the solver must block it with that exact record.",
         [counterexample_has("Amount", None), system_confidence_at_most(0.3), only_audit_record_written, llm_confidence_recorded],
         cassette="naive_null_translation", live_ok=False),
    Case("trap_percent_units", "Opportunity.Discount_Cap", "SOLVER_TRUTH", "The 50 vs 0.5 percent trap with the production prompt. Verdict must match the proof.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("trap_percent_units_naive_prompt", "Opportunity.Discount_Cap", "SOLVER_TRUTH", "Same percent trap without the hint about units.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, prompt_variant_is("naive")],
         prompt_variant="naive", live_ok=False),
    Case("solver_catches_percent_units", "Opportunity.Discount_Cap", "FAIL",
         "Copying the number 50 across systems, forced. The target would never fire where Salesforce fires at 50 percent; must be blocked.",
         [counterexample_has("Discount__c", ...), blocks_source_firing_record, system_confidence_at_most(0.3), only_audit_record_written],
         cassette="percent_units_literal", live_ok=False),
    Case("solver_catches_weaker_translation", HERO, "FAIL", "A boundary quietly moved from <= to <, forced. A human review would miss it; the solver must not.",
         [counterexample_has("Amount", ...), blocks_source_firing_record, system_confidence_at_most(0.3), only_audit_record_written], cassette="weaker_translation", live_ok=False),
    Case("recovery_after_partial_failure", HERO, "ERROR", "Airtable fails mid-write. The run must record the error, and a rerun must finish with exactly one record, no duplicates.",
         [error_captured("create_record", 503)], cassette="happy_closedwon_amount", inject_fault="airtable.create_record:503",
         rerun=True, expect_second="PASS", checks_second=[solver_equivalent, guard_field_written], live_ok=False),
    # ---- phase 2: formula fields and flows (skills/plan-phase2.md)
    Case("formula_net_amount", "Opportunity.Net_Amount__c", "SOLVER_TRUTH", "A formula field (net amount after discount). Must compute the same value for every input, then be confirmed by a real probe record in Airtable.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, behavioural_check_verified]),
    Case("formula_is_big_deal", "Opportunity.Is_Big_Deal__c", "SOLVER_TRUTH", "A yes/no formula field (is this a big deal). Verified like a condition and confirmed by a probe record.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded, behavioural_check_verified]),
    Case("formula_literal_copy_off_by_100", "Opportunity.Net_Amount__c", "FAIL", "Keeping the /100 from Salesforce when Airtable already stores 0.5, forced. Off by 100x; must be blocked with both computed values shown.",
         [outputs_recorded, system_confidence_at_most(0.3), only_audit_record_written], cassette="net_amount_literal_copy", live_ok=False),
    Case("flow_flag_stale_negotiation", "Flow.Flag_Stale_Negotiation", "SOLVER_TRUTH", "A flow that flags stale negotiations. Its conditions must be proven and, if right, rebuilt as a computed field.",
         [write_matches_verdict, confidence_consistent, llm_confidence_recorded]),
    Case("flow_not_verifiable_inventoried", "Flow.sfdc_default_ReportExport_Protection_Flow", "AMBIGUOUS", "A Salesforce default flow OffBoard cannot verify. Must be listed with a reason and left alone, no model call.",
         [no_llm_call, system_confidence_at_most(0.0), only_audit_record_written], needs_llm=False),
    Case("distractor_near_duplicate", HERO, "PASS", "Distractors in the base: 15 unrelated records and a second table. After the run they must be untouched, byte for byte.",
         [solver_equivalent, guard_field_written], cassette="happy_closedwon_amount", live_ok=False),
]

CASES_BY_ID = {c.case_id: c for c in CASES}
