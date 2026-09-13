"""Block-0 smoke test: auth (SOAP login, falling back to the Salesforce CLI refresh token), API version,
list Opportunity validation rules, fetch one formula, describe the fields the IR needs. Never prints secrets."""
import os, json, urllib.parse, sys, subprocess, shutil
from dotenv import load_dotenv
load_dotenv()
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed

def via_soap():
    # Winter '27 (68.0) rolls out in waves through Oct 2026; fall back to 67.0 if the org is not upgraded yet.
    last = None
    for ver in ("68.0", "67.0"):
        try:
            return Salesforce(username=os.environ["SF_USERNAME"], password=os.environ["SF_PASSWORD"],
                              security_token=os.environ["SF_SECURITY_TOKEN"], version=ver)
        except SalesforceAuthenticationFailed as e:
            last = e
            if "UNSUPPORTED_API_VERSION" not in str(e): raise
    raise last

def via_cli(alias=os.environ.get("SF_CLI_ALIAS", "dev")):
    sf_bin = shutil.which("sf") or r"C:\Users\User\AppData\Roaming\npm\sf.cmd"
    out = subprocess.check_output([sf_bin, "org", "display", "--target-org", alias, "--json"], text=True)
    r = json.loads(out)["result"]
    inst = r["instanceUrl"].replace("https://", "")
    return Salesforce(instance=inst, session_id=r["accessToken"], version="68.0"), r.get("username"), r.get("apiVersion")

sf = None
if os.environ.get("SF_AUTH", "cli") in ("auto", "soap"):
    try:
        sf = via_soap(); print("auth: SOAP login ok")
    except SalesforceAuthenticationFailed as e:
        print("auth: SOAP login failed ->", str(e)[str(e).find("code"):str(e).find(")")+1])
if sf is None:
    try:
        sf, user, ver = via_cli(); print("auth: CLI token ok; user", user, "; org api version", ver)
    except Exception as e:
        print("auth: CLI fallback failed:", type(e).__name__, str(e)[:200]); sys.exit(1)
print("instance:", sf.sf_instance)
soql = ("SELECT Id, ValidationName, Active, ErrorMessage, Description, EntityDefinition.QualifiedApiName "
        "FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = 'Opportunity'")
res = sf.toolingexecute("query/?q=" + urllib.parse.quote(soql))
print("validation rules on Opportunity:", res["size"])
for r in res["records"]:
    print("  -", r["ValidationName"], "active=" + str(r["Active"]), "| msg:", (r.get("ErrorMessage") or "")[:60])
if res["records"]:
    rid = res["records"][0]["Id"]
    md = sf.toolingexecute(f"sobjects/ValidationRule/{rid}")["Metadata"]
    print("first rule formula:", md["errorConditionFormula"])
desc = sf.Opportunity.describe()
want = {"StageName", "Amount", "Probability", "Type", "Loss_Reason__c", "Discount__c"}
for f in desc["fields"]:
    if f["name"] in want:
        vals = [p["value"] for p in f.get("picklistValues", []) if p["active"]]
        print(f"  field {f['name']}: type={f['type']} nillable={f['nillable']} precision={f.get('precision')} scale={f.get('scale')}" + (f" values={vals}" if vals else ""))
missing = want - {f["name"] for f in desc["fields"]}
print("missing fields:", sorted(missing) or "none")
