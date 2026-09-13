"""Block-0 seed: create custom fields and the 8 demo validation rules on Opportunity via the Tooling API,
and run the empirical null-semantics probe while only the naive rule exists. Idempotent: skips what exists.
Descriptions carry the business intent of each rule; the pipeline reads them into the RuleIR."""
import sys, os, json, time; sys.path.insert(0, os.path.dirname(__file__))
from sf_cli import rest, soql, describe, delete_record, SfCliError

FIELDS = [
  {"FullName": "Opportunity.Loss_Reason__c", "Metadata": {"label": "Loss Reason", "type": "Text", "length": 255,
     "description": "Why a deal was lost. Required at Closed Lost so win/loss analysis has data."}},
  {"FullName": "Opportunity.Discount__c", "Metadata": {"label": "Discount", "type": "Percent", "precision": 5, "scale": 2,
     "description": "Discount granted, as a percent (50 = 50%). Capped by finance policy."}},
  {"FullName": "Opportunity.Region__c", "Metadata": {"label": "Region", "type": "Picklist", "description": "Sales region for partner routing.",
     "valueSet": {"valueSetDefinition": {"sorted": False, "value": [{"fullName": v, "label": v, "default": False} for v in ("Americas", "EMEA", "APAC")]}}}},
]
def rule(name, formula, message, field, description):
    return {"FullName": f"Opportunity.{name}", "Metadata": {"active": True, "errorConditionFormula": formula,
            "errorMessage": message, "errorDisplayField": field, "description": description}}
NAIVE = rule("ClosedWon_Amount_Naive", 'ISPICKVAL(StageName,"Closed Won") && Amount <= 0',
             "A Closed Won opportunity must have a positive amount.", "Amount",
             "Finance: no deal may be booked as won with zero or negative revenue. NOTE: written without ISBLANK, so a blank Amount slips through; kept as the calibration trap.")
RULES = [
  rule("ClosedWon_Requires_Amount", 'ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) || Amount <= 0)',
       "A Closed Won opportunity must have a positive amount.", "Amount",
       "Finance: revenue recognition depends on every won deal carrying a real amount. Blank counts as missing."),
  rule("ClosedLost_Requires_Reason", 'ISPICKVAL(StageName,"Closed Lost") && ISBLANK(Loss_Reason__c)',
       "Enter a loss reason before closing as lost.", "Loss_Reason__c",
       "Sales ops: win/loss analysis needs a reason on every lost deal."),
  rule("Negotiation_Min_Probability", 'ISPICKVAL(StageName,"Negotiation/Review") && Probability < 50',
       "Deals in Negotiation/Review must have probability of at least 50%.", "Probability",
       "Forecasting: negotiation-stage deals below 50% distort the weighted pipeline."),
  rule("Large_Deal_Type_Guard", 'Amount > 1000000 && NOT(ISPICKVAL(Type,"New Customer"))',
       "Deals over $1M must be typed as New Customer for exec review.", "Type",
       "Exec review: every seven-figure deal is routed as new business regardless of account history."),
  rule("Discount_Cap", "Discount__c > 50",
       "Discounts above 50% require a separate approval; enter 50 or less.", "Discount__c",
       "Finance policy: maximum self-serve discount is 50 percent. Percent field stores 50 for 50%."),
  rule("Stage_Regression_Blocked", 'ISPICKVAL(PRIORVALUE(StageName),"Closed Won") && NOT(ISPICKVAL(StageName,"Closed Won"))',
       "A Closed Won opportunity cannot be reopened; clone it instead.", "StageName",
       "Revenue integrity: booked deals are immutable. Uses PRIORVALUE, which has no Airtable equivalent."),
  rule("EMEA_Requires_Amount", 'ISPICKVAL(Region__c,"EMEA") && ISBLANK(Amount)',
       "EMEA opportunities must carry an amount at creation.", "Amount",
       "EMEA partner routing needs an amount to pick the partner tier. Region has no counterpart in the Airtable base."),
]

def ensure_field(spec):
    name = spec["FullName"].split(".")[1]
    if name in {f["name"] for f in describe("Opportunity")["fields"]}:
        print("field exists:", name); return
    r = rest("sobjects/CustomField", "POST", spec, tooling=True); print("field created:", name, r.get("id"))

def existing_rules():
    return {r["ValidationName"]: r["Id"] for r in soql("SELECT Id, ValidationName FROM ValidationRule WHERE EntityDefinition.QualifiedApiName='Opportunity'", tooling=True)["records"]}

def ensure_rule(spec):
    name = spec["FullName"].split(".")[1]
    if name in existing_rules(): print("rule exists:", name); return
    r = rest("sobjects/ValidationRule", "POST", spec, tooling=True); print("rule created:", name, r.get("id"))

def try_insert(label, fields):
    try:
        r = rest("sobjects/Opportunity", "POST", fields); print(f"  probe {label}: SAVED id={r['id']}"); return r["id"]
    except SfCliError as e:
        print(f"  probe {label}: REJECTED ->", e.body[:160]); return None

for f in FIELDS: ensure_field(f)
time.sleep(2)
ensure_rule(NAIVE)
print("null-semantics probe with ONLY the naive rule active:")
base = {"Name": "PROBE", "StageName": "Closed Won", "CloseDate": "2026-12-31"}
ids = [try_insert("blank Amount", base), try_insert("Amount=0", {**base, "Amount": 0}), try_insert("Amount=100", {**base, "Amount": 100})]
for r in RULES: ensure_rule(r)
print("probe with the ISBLANK-safe hero rule also active:")
ids.append(try_insert("blank Amount (hero active)", base))
for i in filter(None, ids): delete_record("Opportunity", i)
print("probe records deleted; rules now:", sorted(existing_rules()))
