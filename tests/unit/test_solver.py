"""Solver correctness, checked against the independent reference interpreter.

Method (anti-bias): for every (Salesforce rule, Airtable guard) pair we
  1. enumerate a grid of concrete records over the fields involved and evaluate BOTH sides with the
     plain-Python interpreter (app/reference.py, no Z3); the grid says whether a disagreement exists;
  2. run the solver;
  3. require: solver says equivalent  => grid found no disagreement, and
              solver says not_equivalent => the interpreter, evaluated on the solver's own counterexample,
              confirms the two sides really disagree there (the counterexample is real, not an artefact).
The grid cannot prove equivalence, so (3a) is one-directional by design; (3b) is the strong check.
"""
import itertools

import pytest

from app.models import Node
from app.parsers.at_formula import parse_at_formula
from app.parsers.sf_formula import parse_sf_formula
from app.reference import evaluate
from app.solver import SchemaMismatch, check_equivalence
from tests.specs import AT_FIELDS, FIELD_MAP, SF_FIELDS

INV = {v: k for k, v in FIELD_MAP.items()}
NUMS = [None, -1, 0, 0.3, 0.5, 0.6, 1, 49, 50, 51, 100, 1_000_000, 1_000_001]


def grid_values(sf_field):
    spec = SF_FIELDS[sf_field]
    if spec.type == "picklist":
        vals = [o.api_name for o in spec.options] + ["__other__"]
        return vals if not spec.nullable else vals + [None]
    if spec.type == "checkbox":
        return [True, False]
    if spec.type == "text":
        return [None, "", "budget", "x"]
    return NUMS


def rename_to_source(record):
    return {FIELD_MAP.get(k, k): v for k, v in record.items()}


def grid_disagreements(sf_node, at_node):
    fields = sorted(sf_node.fields() | {FIELD_MAP[f] for f in at_node.fields()})
    at_fields_src = {FIELD_MAP[k]: v for k, v in AT_FIELDS.items()}
    out = []
    for combo in itertools.product(*[grid_values(f) for f in fields]):
        rec = dict(zip(fields, combo))
        sf = evaluate(sf_node, rec, SF_FIELDS, "sf")
        at = evaluate(at_node, {INV[k]: v for k, v in rec.items()}, AT_FIELDS, "at")
        if sf != at:
            out.append((rec, sf, at))
    return out


def run(sf_formula, at_formula):
    sf = parse_sf_formula(sf_formula, SF_FIELDS)
    at = parse_at_formula(at_formula, AT_FIELDS)
    assert sf.node is not None, sf
    assert at.node is not None, at
    res = check_equivalence(sf.node, SF_FIELDS, at.node, AT_FIELDS, FIELD_MAP)
    dis = grid_disagreements(sf.node, at.node)
    if res.status == "equivalent":
        assert dis == [], f"solver said equivalent but grid disagrees at {dis[:3]}"
    elif res.status == "not_equivalent":
        rec = res.counterexample
        sf_v = evaluate(sf.node, rec, SF_FIELDS, "sf")
        at_v = evaluate(at.node, {INV[k]: v for k, v in rec.items() if k in INV}, AT_FIELDS, "at")
        assert sf_v != at_v, f"solver counterexample {rec} is not a real disagreement per the interpreter"
        assert (res.source_fires, res.target_flags) == (sf_v, at_v)
    return res, dis


HERO = 'ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) || Amount <= 0)'


@pytest.mark.parametrize("sf,at", [
    (HERO, 'IF(AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<=0)), "VIOLATION", "")'),
    (HERO, 'IF(AND({Stage}="Closed Won", {Amount}<=0), "VIOLATION", "")'),  # blank is 0 in Airtable, so this IS equivalent to the null-safe rule
    ('ISPICKVAL(StageName,"Closed Lost") && ISBLANK(Loss_Reason__c)', 'IF(AND({Stage}="Closed Lost", {Loss Reason}=BLANK()), 1, 0)'),
    ('ISPICKVAL(StageName,"Negotiation/Review") && Probability < 50', 'AND({Stage}="Negotiation/Review", {Probability}<0.5, {Probability}!=BLANK())'),
    ('Amount > 1000000 && NOT(ISPICKVAL(Type,"New Customer"))', 'AND({Amount}>1000000, NOT({Type}="New Customer"))'),
    ("Discount__c > 50", "AND({Discount}>0.5)"),
    ("Is_Strategic__c && Amount < 10", 'AND({Is Strategic}, {Amount}<10, {Amount}!=BLANK())'),
])
def test_equivalent_pairs(sf, at):
    res, _ = run(sf, at)
    assert res.status == "equivalent", res.reason
    assert res.z3_result == "unsat" and "sides_disagree" in res.unsat_core
    assert res.wellformed["source"]["can_fire"] and res.wellformed["source"]["can_pass"]


@pytest.mark.parametrize("sf,at,expect_field", [
    # null-semantics trap: SF blank does not fire; Airtable blank is 0 and flags
    ('ISPICKVAL(StageName,"Closed Won") && Amount <= 0', 'AND({Stage}="Closed Won", {Amount}<=0)', "Amount"),
    # off-by-one
    (HERO, 'AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<0))', "Amount"),
    # percent units: SF literal 50 means 50%; Airtable stores 0.5
    ("Discount__c > 50", "{Discount}>50", "Discount__c"),
    # negation of picklist on blank handled the same, but wrong value
    ('ISPICKVAL(StageName,"Closed Won")', '{Stage}="Closed Lost"', "StageName"),
    # Airtable blank-is-zero makes `< 50%` fire on blank; SF does not
    ('ISPICKVAL(StageName,"Negotiation/Review") && Probability < 50', 'AND({Stage}="Negotiation/Review", {Probability}<0.5)', "Probability"),
])
def test_non_equivalent_pairs_yield_real_counterexamples(sf, at, expect_field):
    res, dis = run(sf, at)
    assert res.status == "not_equivalent", res.reason
    assert dis, "grid should also see a disagreement for these pairs"
    assert expect_field in res.counterexample
    assert res.wellformed["leaf_evaluation"]["source"] and res.wellformed["leaf_evaluation"]["target"]
    assert res.z3_result == "sat" and res.sexpr.startswith("(")


def test_null_trap_counterexample_is_the_blank_record():
    res, _ = run('ISPICKVAL(StageName,"Closed Won") && Amount <= 0', 'AND({Stage}="Closed Won", {Amount}<=0)')
    # the only disagreement possible is blank Amount with Closed Won: SF does not fire, Airtable flags
    assert res.counterexample == {"StageName": "Closed Won", "Amount": None}
    assert (res.source_fires, res.target_flags) == (False, True)


def test_percent_trap_counterexample_lies_between_half_and_fifty():
    res, _ = run("Discount__c > 50", "{Discount}>50")
    d = res.counterexample["Discount__c"]
    assert d is not None and 0.5 < d <= 50  # SF fires (d*100 > 50), Airtable does not (d <= 50)
    assert (res.source_fires, res.target_flags) == (True, False)


def test_schema_mismatch_is_raised_before_any_solving():
    sf = parse_sf_formula('ISPICKVAL(Region__c,"EMEA") && ISBLANK(Amount)', SF_FIELDS).node
    at = parse_at_formula('AND({Stage}="EMEA", {Amount}=BLANK())', AT_FIELDS).node
    with pytest.raises(SchemaMismatch):
        check_equivalence(sf, SF_FIELDS, at, AT_FIELDS, FIELD_MAP)  # Region__c has no target field
    # a source picklist value missing from the target select options
    at_fields = dict(AT_FIELDS)
    at_fields["Stage"] = AT_FIELDS["Stage"].model_copy(update={"options": [o for o in AT_FIELDS["Stage"].options if o.label != "Closed Won"]})
    sf2 = parse_sf_formula('ISPICKVAL(StageName,"Closed Won")', SF_FIELDS).node
    at2 = parse_at_formula('{Stage}="Closed Won"', at_fields).node
    with pytest.raises(SchemaMismatch, match="Closed Won"):
        check_equivalence(sf2, SF_FIELDS, at2, at_fields, FIELD_MAP)


def test_reference_interpreter_semantics_by_hand():
    """Pin the oracle itself to hand-derived facts so a bug there cannot hide a solver bug."""
    amt_lte0 = Node(op="lte", field="Amount", value=0)
    assert evaluate(amt_lte0, {"Amount": None}, SF_FIELDS, "sf") is False   # SF: blank is null
    assert evaluate(Node(op="lte", field="Amount", value=0), {"Amount": None}, {"Amount": AT_FIELDS["Amount"]}, "at") is True  # Airtable: blank is 0
    assert evaluate(Node(op="gt", field="Discount__c", value=50), {"Discount__c": 0.6}, SF_FIELDS, "sf") is True  # 60% > 50
    assert evaluate(Node(op="gt", field="Discount", value=50), {"Discount": 0.6}, AT_FIELDS, "at") is False
    assert evaluate(Node(op="eq", field="StageName", value="Closed Won"), {"StageName": None}, SF_FIELDS, "sf") is False
    assert evaluate(Node(op="ne", field="StageName", value="Closed Won"), {"StageName": None}, SF_FIELDS, "sf") is True


def test_placeholder_mapping_target_is_a_schema_mismatch_not_a_crash():
    """Recorded 2026-09-13: the model mapped Region__c -> "(no counterpart)" and dropped the condition."""
    sf = parse_sf_formula('ISPICKVAL(Region__c,"EMEA") && ISBLANK(Amount)', SF_FIELDS).node
    at = parse_at_formula('IF({Amount} = BLANK(), "VIOLATION", "")', AT_FIELDS).node
    fmap = {"Amount": "Amount", "(no counterpart)": "Region__c"}
    with pytest.raises(SchemaMismatch, match="Region__c"):
        check_equivalence(sf, SF_FIELDS, at, AT_FIELDS, fmap)
