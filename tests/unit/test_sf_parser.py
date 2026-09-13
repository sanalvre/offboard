"""Salesforce formula parser. Expected trees are written by hand from the formula text."""
import pytest

from app.models import Node
from app.parsers.sf_formula import build_rule_ir, infer_dependency, parse_sf_formula
from app.models import RuleSource
from tests.specs import SF_FIELDS


def eq(f, v): return Node(op="eq", field=f, value=v)
def blank(f): return Node(op="is_blank", field=f)


def test_hero_rule_parses_to_and_of_eq_and_or():
    out = parse_sf_formula('ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) || Amount <= 0)', SF_FIELDS)
    assert out.error is None and out.unsupported == []
    assert out.node == Node(op="and", args=[eq("StageName", "Closed Won"),
                                            Node(op="or", args=[blank("Amount"), Node(op="lte", field="Amount", value=0)])])


def test_function_style_and_or_not_and_text_equals():
    out = parse_sf_formula('AND(NOT(ISPICKVAL(Type,"New Customer")), OR(Amount > 1000000, TEXT(StageName) = "Closed Won"))', SF_FIELDS)
    assert out.node == Node(op="and", args=[
        Node(op="not", args=[eq("Type", "New Customer")]),
        Node(op="or", args=[Node(op="gt", field="Amount", value=1000000), eq("StageName", "Closed Won")]),
    ])


def test_literal_on_left_is_flipped():
    out = parse_sf_formula("50 > Probability", SF_FIELDS)
    assert out.node == Node(op="lt", field="Probability", value=50)


def test_not_equal_variants_and_isnull_alias():
    a = parse_sf_formula('TEXT(StageName) <> "Closed Won"', SF_FIELDS).node
    b = parse_sf_formula('TEXT(StageName) != "Closed Won"', SF_FIELDS).node
    assert a == b == Node(op="ne", field="StageName", value="Closed Won")
    assert parse_sf_formula("ISNULL(Amount)", SF_FIELDS).node == blank("Amount")


def test_blankvalue_rewrites_to_null_safe_expression():
    # BLANKVALUE(Amount, 0) <= 0 : default 0 satisfies <= 0, so blank counts as a hit
    out = parse_sf_formula("BLANKVALUE(Amount, 0) <= 0", SF_FIELDS)
    assert out.node == Node(op="or", args=[blank("Amount"), Node(op="lte", field="Amount", value=0)])
    # BLANKVALUE(Amount, 0) > 0 : default 0 does not satisfy > 0, so blank must be excluded
    out = parse_sf_formula("BLANKVALUE(Amount, 0) > 0", SF_FIELDS)
    assert out.node == Node(op="and", args=[Node(op="not", args=[blank("Amount")]), Node(op="gt", field="Amount", value=0)])


def test_bare_checkbox_and_negative_literal():
    assert parse_sf_formula("Is_Strategic__c && Amount < -5", SF_FIELDS).node == Node(op="and", args=[
        eq("Is_Strategic__c", True), Node(op="lt", field="Amount", value=-5)])


def test_priorvalue_is_reported_as_unsupported_not_guessed():
    out = parse_sf_formula('ISPICKVAL(PRIORVALUE(StageName),"Closed Won") && NOT(ISPICKVAL(StageName,"Closed Won"))', SF_FIELDS)
    assert out.node is None
    assert "PRIORVALUE" in out.unsupported


@pytest.mark.parametrize("formula,construct", [
    ("TODAY() > CloseDate", "TODAY"),
    ("$User.Id = OwnerId", "$User"),
    ('CONTAINS(Name, "x")', "CONTAINS"),
    ("Amount > Expected_Amount__c", "field_to_field_comparison"),
    ("Unknown_Field__c > 1", "field:Unknown_Field__c"),
    ('ISPICKVAL(StageName,"Closed Won - Partner")', "option:StageName=Closed Won - Partner"),
])
def test_unsupported_constructs_are_named(formula, construct):
    out = parse_sf_formula(formula, SF_FIELDS)
    assert out.node is None
    assert construct in out.unsupported


@pytest.mark.parametrize("formula", ["ISPICKVAL(StageName)", "Amount <= ", "AND(Amount > 0", 'ISBLANK("x")', "Amount"])
def test_malformed_formulas_return_error_not_exception(formula):
    out = parse_sf_formula(formula, SF_FIELDS)
    assert out.node is None and out.error


def test_dependency_inference_and_rule_ir():
    src = RuleSource(object="Opportunity", name="R", formula='ISPICKVAL(StageName,"Closed Lost") && ISBLANK(Loss_Reason__c)')
    ir = build_rule_ir(src, SF_FIELDS)
    assert ir.dependency.trigger_field == "StageName" and ir.dependency.constrained_field == "Loss_Reason__c"
    assert infer_dependency(parse_sf_formula("Discount__c > 50", SF_FIELDS).node, SF_FIELDS).constrained_field == "Discount__c"
    bad = build_rule_ir(RuleSource(object="Opportunity", name="B", formula="PRIORVALUE(Amount) > 0"), SF_FIELDS)
    assert bad.condition is None and bad.unsupported == ["PRIORVALUE"]
