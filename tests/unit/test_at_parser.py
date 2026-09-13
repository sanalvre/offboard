"""Airtable guard-formula parser. Expected trees are written by hand."""
import pytest

from app.models import Node
from app.parsers.at_formula import parse_at_formula
from tests.specs import AT_FIELDS


def eq(f, v): return Node(op="eq", field=f, value=v)
def blank(f): return Node(op="is_blank", field=f)


def test_guard_shape_if_violation_unwraps_condition():
    out = parse_at_formula('IF(AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<=0)), "VIOLATION", "")', AT_FIELDS)
    assert out.error is None and out.unsupported == []
    assert out.node == Node(op="and", args=[eq("Stage", "Closed Won"),
                                            Node(op="or", args=[blank("Amount"), Node(op="lte", field="Amount", value=0)])])


def test_if_one_zero_and_bare_condition_are_equivalent_shapes():
    a = parse_at_formula('IF(AND({Stage} = "Closed Won", {Amount} <= 0), 1, 0)', AT_FIELDS).node
    b = parse_at_formula('AND({Stage} = "Closed Won", {Amount} <= 0)', AT_FIELDS).node
    assert a == b == Node(op="and", args=[eq("Stage", "Closed Won"), Node(op="lte", field="Amount", value=0)])


def test_blank_comparisons_and_not_blank():
    assert parse_at_formula("{Amount}=BLANK()", AT_FIELDS).node == blank("Amount")
    assert parse_at_formula("BLANK()={Amount}", AT_FIELDS).node == blank("Amount")
    assert parse_at_formula("{Amount}!=BLANK()", AT_FIELDS).node == Node(op="not", args=[blank("Amount")])


def test_single_quotes_and_literal_first_flip():
    assert parse_at_formula("AND({Stage}='Closed Lost', 0.5 > {Probability})", AT_FIELDS).node == Node(
        op="and", args=[eq("Stage", "Closed Lost"), Node(op="lt", field="Probability", value=0.5)])


def test_non_guard_if_shape_is_unsupported():
    out = parse_at_formula('IF({Amount}>0, "ok", "VIOLATION")', AT_FIELDS)  # inverted: truthy in the else branch
    assert out.node is None and "IF:non_guard_shape" in out.unsupported


@pytest.mark.parametrize("formula,construct", [
    ('FIND("x", {Loss Reason}) > 0', "FIND"),
    ("{Amount} & {Stage}", "operator:&"),
    ("{Nope} > 1", "field:Nope"),
    ("{Amount} > {Probability}", "field_to_field_comparison"),
])
def test_unsupported_constructs_are_named(formula, construct):
    out = parse_at_formula(formula, AT_FIELDS)
    assert out.node is None and construct in out.unsupported


@pytest.mark.parametrize("formula", ["AND({Amount} > 0", "{Amount} > BLANK()", "IF()", "{Amount}"])
def test_malformed_returns_error(formula):
    out = parse_at_formula(formula, AT_FIELDS)
    assert out.node is None and out.error
