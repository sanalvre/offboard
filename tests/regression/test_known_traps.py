"""Regression pins for the traps verified empirically on 2026-09-13 (skills/plan.md 1.2, 1.3).

If a parser or encoding change makes any of these pass as equivalent, real migrations would silently
change meaning. Each test asserts the counterexample CONTENT, not just the status.
"""
from app.parsers.at_formula import parse_at_formula
from app.parsers.sf_formula import parse_sf_formula
from app.solver import check_equivalence
from tests.specs import AT_FIELDS, FIELD_MAP, SF_FIELDS


def solve(sf, at):
    return check_equivalence(parse_sf_formula(sf, SF_FIELDS).node, SF_FIELDS, parse_at_formula(at, AT_FIELDS).node, AT_FIELDS, FIELD_MAP)


def test_trap_null_semantics_blank_amount():
    # Dev org probe: blank Amount SAVED under the naive rule; Airtable probe: naive formula matched the blank record.
    r = solve('ISPICKVAL(StageName,"Closed Won") && Amount <= 0', 'IF(AND({Stage}="Closed Won", {Amount}<=0), "VIOLATION", "")')
    assert r.status == "not_equivalent"
    assert r.counterexample == {"StageName": "Closed Won", "Amount": None}
    assert r.source_fires is False and r.target_flags is True


def test_trap_percent_units():
    r = solve("Discount__c > 50", 'IF({Discount}>50, "VIOLATION", "")')
    assert r.status == "not_equivalent"
    assert r.source_fires is True and r.target_flags is False
    assert 0.5 < r.counterexample["Discount__c"] <= 50


def test_trap_off_by_one_boundary():
    r = solve('ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) || Amount <= 0)',
              'IF(AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<0)), "VIOLATION", "")')
    assert r.status == "not_equivalent"
    assert r.counterexample == {"StageName": "Closed Won", "Amount": 0}
    assert r.source_fires is True and r.target_flags is False


def test_unsupported_construct_never_reaches_the_solver():
    out = parse_sf_formula('ISPICKVAL(PRIORVALUE(StageName),"Closed Won") && NOT(ISPICKVAL(StageName,"Closed Won"))', SF_FIELDS)
    assert out.node is None and out.unsupported == ["PRIORVALUE"]
