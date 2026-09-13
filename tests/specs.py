"""Field metadata used across tests. Mirrors the real Dev org (describe, 2026-09-13) and the real Airtable base."""
from app.models import FieldSpec, Option

STAGES = ["Prospecting", "Qualification", "Needs Analysis", "Value Proposition", "Id. Decision Makers",
          "Perception Analysis", "Proposal/Price Quote", "Negotiation/Review", "Closed Won", "Closed Lost"]
TYPES = ["Existing Customer - Upgrade", "Existing Customer - Replacement", "Existing Customer - Downgrade", "New Customer"]


def opts(values):
    return [Option(api_name=v, label=v) for v in values]


SF_FIELDS = {
    "StageName": FieldSpec(name="StageName", type="picklist", nullable=False, options=opts(STAGES)),
    "Amount": FieldSpec(name="Amount", type="currency", nullable=True, scale=1),
    "Probability": FieldSpec(name="Probability", type="percent", nullable=True, scale=100),
    "Discount__c": FieldSpec(name="Discount__c", type="percent", nullable=True, scale=100),
    "Type": FieldSpec(name="Type", type="picklist", nullable=True, options=opts(TYPES)),
    "Loss_Reason__c": FieldSpec(name="Loss_Reason__c", type="text", nullable=True),
    "Region__c": FieldSpec(name="Region__c", type="picklist", nullable=True, options=opts(["Americas", "EMEA", "APAC"])),
    "Is_Strategic__c": FieldSpec(name="Is_Strategic__c", type="checkbox", nullable=False),
}

AT_FIELDS = {
    "Stage": FieldSpec(name="Stage", type="picklist", nullable=True, options=opts(STAGES + ["Negotiation", "Proposal"]), compare_on="label"),
    "Amount": FieldSpec(name="Amount", type="currency", nullable=True, scale=1, compare_on="label"),
    "Probability": FieldSpec(name="Probability", type="percent", nullable=True, scale=1, compare_on="label"),
    "Discount": FieldSpec(name="Discount", type="percent", nullable=True, scale=1, compare_on="label"),
    "Type": FieldSpec(name="Type", type="picklist", nullable=True, options=opts(TYPES + ["New Business", "Renewal", "Upsell"]), compare_on="label"),
    "Loss Reason": FieldSpec(name="Loss Reason", type="text", nullable=True, compare_on="label"),
    "Is Strategic": FieldSpec(name="Is Strategic", type="checkbox", nullable=False, compare_on="label"),
}

FIELD_MAP = {"Stage": "StageName", "Amount": "Amount", "Probability": "Probability", "Discount": "Discount__c",
             "Type": "Type", "Loss Reason": "Loss_Reason__c", "Is Strategic": "Is_Strategic__c"}
