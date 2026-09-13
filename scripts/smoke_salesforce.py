"""Block-0 smoke test through the CLI transport: API versions, Opportunity validation rules (+ formulas), IR field metadata."""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from sf_cli import rest, soql, describe, API
vers = [v["version"] for v in rest("../")]  # /services/data/ versions resource
print("org api versions: latest", vers[-1], "| using", API)
rules = soql("SELECT Id, ValidationName, Active, ErrorMessage, Description, EntityDefinition.QualifiedApiName "
             "FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = 'Opportunity'", tooling=True)["records"]
print("validation rules on Opportunity:", len(rules))
for r in rules:
    md = rest(f"sobjects/ValidationRule/{r['Id']}", tooling=True)["Metadata"]
    print(f"  - {r['ValidationName']} active={r['Active']}\n      formula: {md['errorConditionFormula']}\n      message: {r.get('ErrorMessage')}")
desc = describe("Opportunity")
want = ["StageName", "Amount", "Probability", "Type", "Loss_Reason__c", "Discount__c", "Region__c"]
have = {f["name"]: f for f in desc["fields"]}
for n in want:
    f = have.get(n)
    if not f: print(f"  field {n}: MISSING"); continue
    vals = [p["value"] for p in f.get("picklistValues", []) if p["active"]]
    print(f"  field {n}: type={f['type']} nillable={f['nillable']} precision={f.get('precision')} scale={f.get('scale')}" + (f" values={vals}" if vals else ""))
