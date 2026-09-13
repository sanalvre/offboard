"""Block-0 smoke test: SOAP login, API version, list Opportunity validation rules, fetch one formula.
Never prints secrets."""
import os, json, urllib.parse, sys
from dotenv import load_dotenv
load_dotenv()
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed
try:
    sf = Salesforce(username=os.environ["SF_USERNAME"], password=os.environ["SF_PASSWORD"],
                    security_token=os.environ["SF_SECURITY_TOKEN"], version="68.0")
except SalesforceAuthenticationFailed as e:
    print("AUTH FAILED:", str(e)[:300]); sys.exit(1)
print("login ok; instance:", sf.sf_instance, "api version:", sf.sf_version)
soql = ("SELECT Id, ValidationName, Active, ErrorMessage, EntityDefinition.QualifiedApiName "
        "FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = 'Opportunity'")
res = sf.toolingexecute("query/?q=" + urllib.parse.quote(soql))
print("validation rules on Opportunity:", res["size"])
for r in res["records"]:
    print("  -", r["ValidationName"], "active=" + str(r["Active"]))
if res["records"]:
    rid = res["records"][0]["Id"]
    md = sf.toolingexecute(f"sobjects/ValidationRule/{rid}")["Metadata"]
    print("first rule formula:", md["errorConditionFormula"])
# field metadata we need for the IR
desc = sf.Opportunity.describe()
want = {"StageName","Amount","Probability","Type","Loss_Reason__c","Discount__c"}
for f in desc["fields"]:
    if f["name"] in want:
        vals = [p["value"] for p in f.get("picklistValues",[]) if p["active"]]
        print(f"  field {f['name']}: type={f['type']} nillable={f['nillable']} scale={f.get('scale')} " + (f"values={vals}" if vals else ""))
missing = want - {f["name"] for f in desc["fields"]}
print("missing fields:", sorted(missing) or "none")
