"""Export the live Salesforce org and Airtable base into fixtures/seed/*.json so the test-mode mocks mirror
reality byte-for-byte. Re-run after changing either system. Never writes secrets."""
import sys, os, json, urllib.parse
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from sf_cli import rest, soql, describe
from pyairtable import Api

# ---- Salesforce
desc = describe("Opportunity")
keep_types = {"picklist","currency","percent","double","int","string","textarea","boolean","date","reference","id"}
fields = [{k: f.get(k) for k in ("name","label","type","nillable","precision","scale","inlineHelpText","custom")} |
          {"picklistValues": [{"value": p["value"], "label": p["label"], "active": p["active"]} for p in f.get("picklistValues", [])]}
          for f in desc["fields"] if f["type"] in keep_types]
rules = []
for r in soql("SELECT Id, ValidationName, Active, ErrorMessage, Description, ErrorDisplayField FROM ValidationRule "
              "WHERE EntityDefinition.QualifiedApiName='Opportunity' ORDER BY ValidationName", tooling=True)["records"]:
    md = rest(f"sobjects/ValidationRule/{r['Id']}", tooling=True)["Metadata"]
    rules.append({"id": r["Id"], "object": "Opportunity", "name": r["ValidationName"], "active": r["Active"],
                  "formula": md["errorConditionFormula"], "error_message": r.get("ErrorMessage") or "",
                  "error_display_field": r.get("ErrorDisplayField") or "", "description": r.get("Description") or ""})
sf_doc = {"exported_at": "2026-09-13", "org_api_version": os.environ.get("SF_API_VERSION","67.0"), "object": "Opportunity",
          "describe": {"name": "Opportunity", "fields": fields}, "rules": rules}
json.dump(sf_doc, open("fixtures/seed/salesforce.json","w",encoding="utf-8"), indent=2, ensure_ascii=False)
print("salesforce: fields", len(fields), "rules", [r["name"] for r in rules])

# ---- Airtable
api = Api(os.environ["AIRTABLE_PAT"]); base = api.base(os.environ["AIRTABLE_BASE_ID"])
raw = api.request("GET", f"https://api.airtable.com/v0/meta/bases/{os.environ['AIRTABLE_BASE_ID']}/tables")
tables = {}
for t in raw["tables"]:
    recs = base.table(t["id"]).all()
    tables[t["name"]] = {"id": t["id"], "primaryFieldId": t["primaryFieldId"],
                         "fields": [{"id": f["id"], "name": f["name"], "type": f["type"], "options": f.get("options")} for f in t["fields"]],
                         "records": [{"id": r["id"], "createdTime": r["createdTime"], "fields": r["fields"]} for r in recs]}
    print(f"airtable: {t['name']} fields={len(t['fields'])} records={len(recs)}")
at_doc = {"exported_at": "2026-09-13", "base_id": os.environ["AIRTABLE_BASE_ID"], "tables": tables}
json.dump(at_doc, open("fixtures/seed/airtable.json","w",encoding="utf-8"), indent=2, ensure_ascii=False)
