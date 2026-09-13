"""Block-0 part 3: does Airtable treat a blank Amount as 0 in the NAIVE formula {Amount}<=0? Uses filterByFormula (same engine), 2 calls, no schema change."""
import os, json
from dotenv import load_dotenv
load_dotenv()
from pyairtable import Api
base = Api(os.environ["AIRTABLE_PAT"]).base(os.environ["AIRTABLE_BASE_ID"])
opp = base.table("Opportunities")
naive = "AND({Stage}='Closed Won', {Amount}<=0)"
hits = opp.all(formula=naive, fields=["Name", "Stage", "Amount"])
print("naive formula", naive, "matches:", [r["fields"]["Name"] for r in hits])
strict = "AND({Stage}='Closed Won', {Amount}<0)"
print("strict formula", strict, "matches:", [r["fields"]["Name"] for r in opp.all(formula=strict, fields=["Name"])])
