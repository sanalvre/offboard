"""Formula-field (transformation) verification: numeric parsers for both systems, the solver's transformation check,
and the reference interpreter, checked against each other the same way as the condition solver:
the solver's counterexample must produce different outputs when the interpreter evaluates both formulas on it,
and a proven-equivalent pair must agree on an enumerated input grid."""
import itertools

import pytest

from app.models import Expr, Node
from app.parsers.numeric import parse_numeric
from app.reference import evaluate_expr
from app.solver import SchemaMismatch, check_transformation
from tests.specs import AT_FIELDS, FIELD_MAP, SF_FIELDS

INV = {v: k for k, v in FIELD_MAP.items()}
NUMS = [None, -1, 0, 0.1, 0.5, 1, 50, 100, 1000, 1_000_000]


def sf(f): return parse_numeric(f, SF_FIELDS, "sf")
def at(f): return parse_numeric(f, AT_FIELDS, "at")


def test_sf_numeric_parse_tree_by_hand():
    e = sf("Amount - (Amount * BLANKVALUE(Discount__c, 0) / 100)").expr
    assert e == Expr(op="sub", args=[Expr(op="field", field="Amount"),
                                     Expr(op="div", args=[Expr(op="mul", args=[Expr(op="field", field="Amount"), Expr(op="blankvalue", field="Discount__c", value=0.0)]),
                                                          Expr(op="num", value=100.0)])])


def test_precedence_and_unary_minus():
    e = sf("-Amount + 2 * 3").expr
    assert e == Expr(op="add", args=[Expr(op="neg", args=[Expr(op="field", field="Amount")]),
                                     Expr(op="mul", args=[Expr(op="num", value=2.0), Expr(op="num", value=3.0)])])


def test_airtable_blank_idiom_lowers_to_blankvalue_and_if_keeps_condition():
    e = at("{Amount} - {Amount} * IF({Discount} = BLANK(), 0, {Discount})").expr
    assert e.args[1].args[1] == Expr(op="blankvalue", field="Discount", value=0.0)
    e2 = at('IF({Stage} = "Closed Won", {Amount}, 0)').expr
    assert e2.op == "if" and e2.cond == Node(op="eq", field="Stage", value="Closed Won")


@pytest.mark.parametrize("formula,construct,system", [
    ("ROUND(Amount, 2)", "ROUND", "sf"), ("Amount * Unknown__c", "field:Unknown__c", "sf"),
    ("LEN({Loss Reason})", "LEN", "at"), ("{Stage} * 2", "non_numeric_field:Stage", "at"),
])
def test_unsupported_named(formula, construct, system):
    out = parse_numeric(formula, SF_FIELDS if system == "sf" else AT_FIELDS, system)
    assert out.expr is None and construct in out.unsupported


def grid(fields):
    for combo in itertools.product(*[NUMS for _ in fields]):
        yield dict(zip(fields, combo))


def run(sf_formula, at_formula, blanks="BlankAsBlank", sf_scale=1.0, at_scale=1.0):
    s, a = sf(sf_formula), at(at_formula)
    assert s.expr is not None and a.expr is not None, (s, a)
    res = check_transformation(s.expr, SF_FIELDS, blanks, sf_scale, a.expr, AT_FIELDS, FIELD_MAP, at_scale)
    fields = sorted(s.expr.fields() | {FIELD_MAP[f] for f in a.expr.fields()})
    def outputs(rec):
        sv = evaluate_expr(s.expr, rec, SF_FIELDS, "sf", blanks)
        av = evaluate_expr(a.expr, {INV[k]: v for k, v in rec.items()}, AT_FIELDS, "at")
        return (None if sv is None else sv / sf_scale), (None if av is None else av / at_scale)
    if res.status == "equivalent":
        for rec in grid(fields):
            o = outputs(rec)
            assert o[0] == o[1] or (o[0] is not None and o[1] is not None and abs(o[0] - o[1]) < 1e-9), (rec, o)
    elif res.status == "not_equivalent":
        o = outputs(res.counterexample)
        assert not (o[0] == o[1] or (o[0] is not None and o[1] is not None and abs(o[0] - o[1]) < 1e-9)), (res.counterexample, o)
        assert (res.outputs["source"] is None) == (o[0] is None) and (res.outputs["target"] is None) == (o[1] is None)
    return res


NET = "Amount - (Amount * BLANKVALUE(Discount__c, 0) / 100)"


def test_net_amount_correct_translation_is_proven():
    # Salesforce Discount__c is 50 for 50%; Airtable {Discount} is 0.5, so no /100. Blank Amount is blank in SF and 0 in Airtable:
    # both sides yield blank/0... they differ (None vs 0) unless the target guards. The proven form guards Amount too.
    res = run(NET, "IF({Amount} = BLANK(), 0, {Amount}) - IF({Amount} = BLANK(), 0, {Amount}) * IF({Discount} = BLANK(), 0, {Discount})")
    # SF: blank Amount -> blank result; Airtable: 0. Not equivalent because presence differs.
    assert res.status == "not_equivalent" and res.counterexample["Amount"] is None
    assert res.outputs == {"source": None, "target": 0}


def test_net_amount_with_blank_as_zero_source_is_proven_equivalent():
    res = run(NET, "{Amount} - {Amount} * IF({Discount} = BLANK(), 0, {Discount})", blanks="BlankAsZero")
    assert res.status == "equivalent" and res.z3_result == "unsat"


def test_net_amount_literal_copy_is_off_by_100x():
    res = run(NET, "{Amount} - ({Amount} * IF({Discount} = BLANK(), 0, {Discount}) / 100)", blanks="BlankAsZero")
    assert res.status == "not_equivalent"
    rec, o = res.counterexample, res.outputs
    assert rec["Amount"] not in (None, 0) and rec["Discount__c"] not in (None, 0)
    assert o["source"] != o["target"]


def test_percent_return_type_scales_result():
    # Salesforce percent formula returns 50 for 50%; Airtable percent field returns 0.5
    res = run("Discount__c / 2", "{Discount} / 2", blanks="BlankAsZero", sf_scale=100.0, at_scale=1.0)
    assert res.status == "equivalent"


def test_division_by_zero_is_blank_on_both_sides():
    res = run("Amount / Discount__c", "{Amount} / ({Discount} * 100)", blanks="BlankAsZero")
    assert res.status == "equivalent"


def test_if_over_picklist_condition():
    res = run('IF(ISPICKVAL(Type,"New Customer"), Amount, 0)', 'IF({Type} = "New Customer", {Amount}, 0)', blanks="BlankAsZero")
    assert res.status == "equivalent"


def test_schema_mismatch_for_unmapped_source_field():
    with pytest.raises(SchemaMismatch, match="Region__c|no target"):
        s = parse_numeric("Amount", SF_FIELDS, "sf").expr
        a = parse_numeric("{Amount}", AT_FIELDS, "at").expr
        check_transformation(Expr(op="add", args=[s, Expr(op="field", field="Region__c")]), {**SF_FIELDS}, "BlankAsZero", 1.0, a, AT_FIELDS, FIELD_MAP)


def test_reference_expr_semantics_by_hand():
    e = sf(NET).expr
    assert evaluate_expr(e, {"Amount": 1000, "Discount__c": 0.1}, SF_FIELDS, "sf") == 900.0   # 0.1 canonical == 10 in SF units
    assert evaluate_expr(e, {"Amount": 1000, "Discount__c": None}, SF_FIELDS, "sf") == 1000.0  # BLANKVALUE guard
    assert evaluate_expr(e, {"Amount": None, "Discount__c": 0.1}, SF_FIELDS, "sf") is None      # BlankAsBlank
    assert evaluate_expr(e, {"Amount": None, "Discount__c": 0.1}, SF_FIELDS, "sf", "BlankAsZero") == 0.0
    a = at("{Amount} - {Amount} * IF({Discount} = BLANK(), 0, {Discount})").expr
    assert evaluate_expr(a, {"Amount": 1000, "Discount": 0.1}, AT_FIELDS, "at") == 900.0
    assert evaluate_expr(a, {"Amount": None, "Discount": 0.1}, AT_FIELDS, "at") == 0.0


def test_blank_result_matches_salesforce_blank_as_blank():
    # Airtable can return a blank result with BLANK(); that makes the BlankAsBlank net amount provably equivalent
    res = run(NET, "IF({Amount} = BLANK(), BLANK(), {Amount} - {Amount} * IF({Discount} = BLANK(), 0, {Discount}))")
    assert res.status == "equivalent"
    e = at("IF({Amount} = BLANK(), BLANK(), 1)").expr
    assert e.op == "if" and e.args[0].op == "blank"
    assert evaluate_expr(e, {"Amount": None}, AT_FIELDS, "at") is None and evaluate_expr(e, {"Amount": 5}, AT_FIELDS, "at") == 1.0


def test_solver_is_deterministic_across_repeated_checks():
    a = run(NET, "{Amount} - ({Amount} * IF({Discount} = BLANK(), 0, {Discount}) / 100)", blanks="BlankAsZero")
    b = run(NET, "{Amount} - ({Amount} * IF({Discount} = BLANK(), 0, {Discount}) / 100)", blanks="BlankAsZero")
    assert (a.counterexample, a.outputs, a.sexpr) == (b.counterexample, b.outputs, b.sexpr)
