"""Block-0 part 2: precision fix via REST PATCH, blank-Amount semantics via the guard formula. <= 6 API calls. Never prints secrets."""
import os, json, httpx
from dotenv import load_dotenv
load_dotenv()
from pyairtable import Api
PAT = os.environ["AIRTABLE_PAT"]; BASE = os.environ["AIRTABLE_BASE_ID"]
api = Api(PAT); base = api.base(BASE); schema = base.schema()
rules_t = schema.table("Migration_Rules"); opp_t = schema.table("Opportunities")
H = {"Authorization": f"Bearer {PAT}"}
for name in ("System Confidence", "LLM Confidence"):
    f = rules_t.field(name)
    if f.options.precision != 2:
        r = httpx.patch(f"https://api.airtable.com/v0/meta/bases/{BASE}/tables/{rules_t.id}/fields/{f.id}",
                        headers=H, json={"options": {"precision": 2}}, timeout=30)
        print(f"precision patch {name}:", r.status_code, r.json().get("options") if r.status_code == 200 else r.text[:200])
opp = base.table(opp_t.id); GUARD = "Guard: ClosedWon_Requires_Amount"
if not opp.all(formula="FIND('PROBE', {Name})", fields=["Name"]):
    opp.batch_create([{"Name": "PROBE blank amount", "Stage": "Closed Won"},
                      {"Name": "PROBE zero amount", "Stage": "Closed Won", "Amount": 0}])
for r in opp.all(formula="FIND('PROBE', {Name})", fields=["Name", "Stage", "Amount", GUARD]):
    print("  ", json.dumps(r["fields"]))
