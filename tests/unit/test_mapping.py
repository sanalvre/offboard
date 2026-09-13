"""Schema-mapping report: expected outcomes derived by hand from the field definitions."""
from app.mapping import check_mapping, mapping_report, propose_targets
from app.models import FieldSpec, Option
from tests.specs import AT_FIELDS, SF_FIELDS


def opts(vals):
    return [Option(api_name=v, label=v) for v in vals]


def test_picklist_value_without_target_option_is_lossy_with_the_value():
    sf = FieldSpec(name="StageName", type="picklist", nullable=False, options=opts(["Closed Won", "Closed Won - Partner"]))
    at = FieldSpec(name="Stage", type="picklist", options=opts(["Closed Won"]), compare_on="label")
    m = check_mapping(sf, at)
    assert m.status == "lossy" and m.counterexample == {"StageName": "Closed Won - Partner"}


def test_picklist_subset_is_lossless_and_percent_scale_gets_a_transform():
    m = check_mapping(SF_FIELDS["StageName"], AT_FIELDS["Stage"])
    assert m.status == "lossless" and m.checks["options"].startswith("unsat")
    p = check_mapping(SF_FIELDS["Probability"], AT_FIELDS["Probability"], {"scale": 0, "target_precision": 2})
    assert p.status == "lossless" and p.transform == "target = source / 100"


def test_decimal_precision_loss_is_found_by_z3():
    # source currency with 2 decimals into a target with 0 decimals: 0.01 has no representation
    sf = FieldSpec(name="Amount", type="currency", scale=1)
    at = FieldSpec(name="Amount", type="currency", scale=1, compare_on="label")
    m = check_mapping(sf, at, {"scale": 2, "target_precision": 0})
    assert m.status == "lossy" and m.counterexample["Amount"] > 0 and round(m.counterexample["Amount"], 2) == m.counterexample["Amount"]
    ok = check_mapping(sf, at, {"scale": 2, "target_precision": 2})
    assert ok.status == "lossless"


def test_nullable_into_required_and_type_mismatch_and_missing_target():
    req = FieldSpec(name="Amount", type="currency", nullable=False, compare_on="label")
    assert check_mapping(SF_FIELDS["Amount"], req).status == "lossy"
    assert check_mapping(SF_FIELDS["Amount"], AT_FIELDS["Stage"]).status == "blocked"
    assert check_mapping(SF_FIELDS["Region__c"], None).status == "blocked"


def test_propose_targets_by_label_and_alias():
    labels = {"StageName": "Stage", "Amount": "Amount", "Loss_Reason__c": "Loss Reason", "Region__c": "Region"}
    t = propose_targets(SF_FIELDS, AT_FIELDS, labels)
    assert t["StageName"] == "Stage" and t["Loss_Reason__c"] == "Loss Reason" and t["Discount__c"] == "Discount" and t["Region__c"] is None
    rep = mapping_report(SF_FIELDS, AT_FIELDS, labels, {}, {}, only={"StageName", "Region__c", "Discount__c"})
    by = {m["source"]: m["status"] for m in rep["mappings"]}
    assert by == {"StageName": "lossless", "Region__c": "blocked", "Discount__c": "lossless"} and rep["counts"] == {"lossless": 2, "blocked": 1}
