"""Pydantic models shared by the pipeline, solver, adapters, trace and eval harness.

Design rule (skills/problem-domain.md 6.3 #1): a formula is meaningless without its field metadata, so
FieldSpec carries type, nullability, scale and option lists, and every RuleIR carries its FieldSpecs.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- fields

FieldType = Literal["picklist", "currency", "number", "percent", "text", "checkbox"]


class Option(BaseModel):
    api_name: str
    label: str


class FieldSpec(BaseModel):
    name: str
    type: FieldType
    nullable: bool = True
    # numeric scale: Salesforce percent stores 50 for 50% (scale 100); Airtable/HubSpot store 0.5 (scale 1).
    scale: float = 1.0
    options: list[Option] = Field(default_factory=list)
    # what a string comparison is matched against: Salesforce ISPICKVAL uses api_name, Airtable selects use label
    compare_on: Literal["api_name", "label"] = "api_name"
    description: str = ""

    def option_keys(self) -> list[str]:
        return [o.api_name if self.compare_on == "api_name" else o.label for o in self.options]


# --------------------------------------------------------------------------- expression IR

Op = Literal["and", "or", "not", "eq", "ne", "lt", "lte", "gt", "gte", "is_blank", "true", "false"]


class Node(BaseModel):
    """Boolean expression over record fields.

    Leaves: eq/ne/lt/lte/gt/gte compare `field` with literal `value`; is_blank tests `field`.
    Branches: and/or take `args`; not takes exactly one arg.
    """

    op: Op
    field: Optional[str] = None
    value: Any = None
    args: list["Node"] = Field(default_factory=list)

    def fields(self) -> set[str]:
        out = {self.field} if self.field else set()
        for a in self.args:
            out |= a.fields()
        return out


Node.model_rebuild()


ExprOp = Literal["num", "field", "add", "sub", "mul", "div", "neg", "if", "blankvalue", "min", "max", "blank"]


class Expr(BaseModel):
    """Numeric expression over record fields (formula fields). `if` carries a boolean Node in `cond`.
    Values of fields are in canonical units; each system's scale is applied by the evaluator/solver."""

    op: ExprOp
    value: Optional[float] = None
    field: Optional[str] = None
    args: list["Expr"] = Field(default_factory=list)
    cond: Optional[Node] = None

    def fields(self) -> set[str]:
        out = {self.field} if self.field else set()
        for a in self.args:
            out |= a.fields()
        if self.cond is not None:
            out |= self.cond.fields()
        return out


Expr.model_rebuild()


# --------------------------------------------------------------------------- source rule and IR


class RuleSource(BaseModel):
    object: str
    name: str
    formula: str
    active: bool = True
    error_message: str = ""
    error_display_field: str = ""
    description: str = ""
    source_id: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.object}.{self.name}"


class Dependency(BaseModel):
    trigger_field: Optional[str] = None
    constrained_field: Optional[str] = None


class RuleIR(BaseModel):
    source: RuleSource
    fields: dict[str, FieldSpec]
    condition: Optional[Node] = None  # None when the formula could not be encoded
    dependency: Dependency = Field(default_factory=Dependency)
    unsupported: list[str] = Field(default_factory=list)  # construct names, e.g. ["PRIORVALUE"]
    parse_error: Optional[str] = None


# --------------------------------------------------------------------------- LLM proposal


class TargetField(BaseModel):
    source_field: str
    target_field: str
    target_type: Literal["singleSelect", "currency", "number", "percent", "singleLineText", "checkbox"]


class Proposal(BaseModel):
    """What the LLM proposes for the Airtable side. Verified by the parser + solver, never trusted."""

    intent_statement: str = Field(description="One sentence: the business reason this rule exists, in plain language.")
    field_mapping: list[TargetField]
    guard_field_name: str = Field(description="Name of the Airtable formula field to create, e.g. 'Guard: <rule>'.")
    guard_formula: str = Field(description="Airtable formula returning 'VIOLATION' when the record violates the rule, else ''.")
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0, description="Self-assessed probability that guard_formula is exactly equivalent for every possible record.")
    caveats: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- solver


class SolverResult(BaseModel):
    status: Literal["equivalent", "not_equivalent", "ambiguous", "error"]
    reason: str = ""
    counterexample: Optional[dict[str, Any]] = None  # record on which the two sides disagree
    outputs: Optional[dict[str, Any]] = None  # transformation checks: {"source": value|None, "target": value|None} on the counterexample
    source_fires: Optional[bool] = None  # under the counterexample
    target_flags: Optional[bool] = None
    unsat_core: list[str] = Field(default_factory=list)
    wellformed: dict[str, Any] = Field(default_factory=dict)  # per side: satisfiable / falsifiable
    sexpr: str = ""  # SMT-LIB text of the equivalence problem as posed
    z3_result: str = ""  # raw "sat" / "unsat" / "unknown"
    elapsed_ms: float = 0.0


# --------------------------------------------------------------------------- decision and claims


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"
    PARTIAL = "PARTIAL"  # condition proven, action has no API-writable equivalent; spec captured for a human


class Decision(BaseModel):
    verdict: Verdict
    system_confidence: float
    llm_confidence: Optional[float] = None
    overconfidence_gap: Optional[float] = None
    reasons: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    kind: str  # field_created | field_exists | record_written | rule_verified_equivalent | rule_blocked | no_write | behavioural_check | probe_deleted | automation_spec
    detail: str
    verified: Optional[bool] = None
    evidence_seq: Optional[int] = None


class RunRequest(BaseModel):
    rule: str = Field(description="Object.RuleName, e.g. Opportunity.ClosedWon_Requires_Amount")
    case_id: Optional[str] = None
    attempt: int = 1
    # eval-only knobs, ignored in live mode unless explicitly allowed
    inject_fault: Optional[str] = None  # e.g. "airtable.write_record:503"
    cassette: Optional[str] = None  # cassette key override (test mode)
    prompt_variant: Literal["informed", "naive"] = "informed"  # "naive" drops the cross-system semantics hints (eval ablation)


class RunSummary(BaseModel):
    run_id: str
    mode: Literal["test", "live"]
    rule: str
    case_id: Optional[str] = None
    attempt: int = 1
    verdict: Verdict
    decision: Decision
    claims: list[Claim] = Field(default_factory=list)
    unsupported_claims: int = 0
    trace_path: str
    canonical_hash: str = ""
