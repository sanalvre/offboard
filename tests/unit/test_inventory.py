"""Inventory parsing against the committed metadata fixture (exported from the real org on 2026-09-13).
Expected values are read from the org's known configuration, not from running the parser."""
from app.inventory import assignment_criteria_node, flow_condition_node, parse_metadata, seed_dir
from app.models import Node

ARTS = {a.key: a for a in parse_metadata(seed_dir())}


def test_all_eight_validation_rules_are_found_and_verifiable():
    vrs = [a for a in ARTS.values() if a.type == "ValidationRule"]
    assert len(vrs) == 8 and all(a.verifiable and a.active for a in vrs)
    assert ARTS["Opportunity.ClosedWon_Requires_Amount"].details["formula"] == 'ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) || Amount <= 0)'


def test_formula_fields_found_with_blank_handling_and_type():
    net = ARTS["Opportunity.Net_Amount__c"]
    assert net.type == "FormulaField" and net.verifiable
    assert net.details["formula"] == "Amount - (Amount * BLANKVALUE(Discount__c, 0) / 100)"
    assert net.details["returnType"] == "Currency" and net.details["scale"] == "2" and net.details["formulaTreatBlanksAs"] == "BlankAsBlank"
    big = ARTS["Opportunity.Is_Big_Deal__c"]
    assert big.details["returnType"] == "Checkbox" and big.verifiable


def test_record_triggered_flow_is_lowered_to_node_with_decision_and_action():
    fl = ARTS["Flow.Flag_Stale_Negotiation"]
    assert fl.verifiable and fl.active and fl.object == "Opportunity"
    assert fl.condition == Node(op="and", args=[Node(op="eq", field="StageName", value="Negotiation/Review"),
                                                Node(op="lt", field="Probability", value=50)]).model_dump(mode="json")
    d = fl.details["decisions"][0]
    assert d["condition"] == Node(op="gt", field="Amount", value=100000).model_dump(mode="json") and d["target"] == "Set_Needs_Review"
    assert fl.details["actions"] == [{"kind": "assign", "name": "Set_Needs_Review", "field": "Needs_Review__c", "operator": "Assign", "value": True}]


def test_non_record_flows_are_inventoried_not_verifiable():
    tsf = ARTS["Flow.sfdc_default_ReportExport_Protection_Flow"]
    assert tsf.verifiable is False and "TransactionSecurityFlow" in tsf.reason


def test_workflow_field_update_and_assignment_rules_inventoried():
    assert ARTS["Workflow.Case.ChangePriorityToHigh"].verifiable is False
    lead = ARTS["AssignmentRule.Lead.Standard"]
    assert lead.verifiable and len(lead.details["entries"]) == 2
    first = lead.details["entries"][0]["condition"]
    assert first["op"] == "or" and [a["value"] for a in first["args"]] == ["US", "USA", "United States", "United States of America"]


def test_condition_lowering_reports_unsupported_instead_of_guessing():
    import xml.etree.ElementTree as ET
    ns = "http://soap.sforce.com/2006/04/metadata"
    c = ET.fromstring(f'<c xmlns="{ns}"><leftValueReference>$Record.Amount</leftValueReference><operator>IsChanged</operator><rightValue><booleanValue>true</booleanValue></rightValue></c>')
    node, uns = flow_condition_node([c], "and")
    assert node is None and uns == ["operator:IsChanged"]
    c2 = ET.fromstring(f'<c xmlns="{ns}"><leftValueReference>$User.Id</leftValueReference><operator>EqualTo</operator><rightValue><stringValue>x</stringValue></rightValue></c>')
    assert flow_condition_node([c2], "and") == (None, ["reference:$User.Id"])
    node, uns = flow_condition_node([], "1 AND (2 OR 3)")
    assert node == Node(op="true") and uns == []  # no leaves: trivially true, logic irrelevant
    it = ET.fromstring(f'<i xmlns="{ns}"><field>Lead.Rating</field><operation>contains</operation><value>Hot</value></i>')
    assert assignment_criteria_node([it]) == (None, ["operation:contains"])
