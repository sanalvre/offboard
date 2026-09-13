"""Salesforce transport via the Salesforce CLI (`sf api request rest`). The CLI holds the refresh token from
`sf org login web --alias dev` and refreshes it itself; Python never sees or stores a credential.
Why not simple_salesforce with the CLI access token: `sf org display` returns a token the REST API rejects
with INVALID_AUTH_HEADER (CLI 2.150.6, 2026-09-13), and SOAP login() is disabled on new orgs."""
import json, os, shutil, subprocess, tempfile, urllib.parse

SF_BIN = shutil.which("sf") or r"C:\Users\User\AppData\Roaming\npm\sf.cmd"
ALIAS = os.environ.get("SF_CLI_ALIAS", "dev")
API = os.environ.get("SF_API_VERSION", "67.0")

class SfCliError(RuntimeError):
    def __init__(self, status, body, path): super().__init__(f"{status} {path}: {body[:300]}"); self.status, self.body = status, body

def rest(path, method="GET", body=None, tooling=False):
    """Call /services/data/v{API}/{path}. Returns parsed JSON (or None for 204). Raises SfCliError on non-2xx."""
    base = f"/services/data/v{API}/" + ("tooling/" if tooling else "")
    cmd = [SF_BIN, "api", "request", "rest", base + path, "--target-org", ALIAS, "--method", method]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8"); json.dump(body, tmp); tmp.close()
        cmd += ["--body", "@" + tmp.name, "--header", "Content-Type: application/json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    finally:
        if tmp: os.unlink(tmp.name)
    out = (p.stdout or "").strip()
    if p.returncode != 0 or out.startswith('[{"message"') or '"errorCode"' in out[:200]:
        raise SfCliError(p.returncode, out or p.stderr, path)
    return json.loads(out) if out else None

def soql(query, tooling=False):
    return rest("query?q=" + urllib.parse.quote(query), tooling=tooling)

def describe(sobject): return rest(f"sobjects/{sobject}/describe")

def delete_record(sobject, record_id):
    """`sf api request rest --method DELETE` fails client-side ("No 'mode' found in 'body' entry", CLI 2.150.6); use the data command."""
    p = subprocess.run([SF_BIN, "data", "delete", "record", "--sobject", sobject, "--record-id", record_id, "--target-org", ALIAS, "--json"],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0: raise SfCliError(p.returncode, p.stdout or p.stderr, f"delete {sobject}/{record_id}")
