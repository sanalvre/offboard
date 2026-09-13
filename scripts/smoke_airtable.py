"""Block-0 smoke test: PAT + base id, schema read, blank-comparison and formula-field-creation probes.
Never prints secrets. Makes at most 3 API calls."""
import os, re, sys, json
from dotenv import load_dotenv
load_dotenv()
from pyairtable import Api
raw = os.environ["AIRTABLE_BASE_ID"]
m = re.search(r"(app[A-Za-z0-9]{14})", raw)
if not m: print("AIRTABLE_BASE_ID does not contain an app... id (got", len(raw), "chars)"); sys.exit(1)
base_id = m.group(1)
if base_id != raw: print(f"note: extracted base id from a longer value ({len(raw)} chars); set AIRTABLE_BASE_ID={base_id}")
api = Api(os.environ["AIRTABLE_PAT"])
base = api.base(base_id)
schema = base.schema()                      # call 1
print("tables:", [t.name for t in schema.tables])
for t in schema.tables:
    print(f"  {t.name}:")
    for f in t.fields:
        opts = getattr(f, "options", None)
        extra = ""
        if f.type in ("singleSelect","multipleSelects") and opts: extra = " choices=" + str([c.name for c in opts.choices])
        if f.type == "formula" and opts: extra = " formula=" + repr(opts.formula)
        if f.type in ("number","percent","currency") and opts: extra = f" precision={opts.precision}"
        print(f"    - {f.name} ({f.type}){extra}")
opp = next((t for t in schema.tables if t.name.lower().startswith("opportunit") and "archive" not in t.name.lower()), None)
if not opp: print("no Opportunities table found; skipping probes"); sys.exit(0)
# Probe A: can a formula field be created via API? (Constraint A in plan.md 1.3). Uses the hero guard so it is useful if it works.
guard = 'IF(AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<=0)), "VIOLATION", "")'
existing = {f.name for f in opp.fields}
if "Guard: ClosedWon_Requires_Amount" in existing:
    print("probe A: guard field already exists")
else:
    try:
        f = base.table(opp.id).create_field("Guard: ClosedWon_Requires_Amount", "formula", options={"formula": guard})   # call 2
        print("probe A: formula field CREATED via API ->", f.id, "(Constraint A does not hold)")
    except Exception as e:
        print("probe A: formula field creation REJECTED:", str(e)[:300])
# Probe B: blank-number semantics: fetch records and show Stage/Amount so we can compare the guard's output
recs = base.table(opp.id).all(fields=["Name","Stage","Amount"] + (["Guard: ClosedWon_Requires_Amount"] if "Guard: ClosedWon_Requires_Amount" in existing else []), max_records=12)  # call 3
print(f"probe B: {len(recs)} records")
for r in recs: print("   ", json.dumps(r["fields"]))
