"""Trace layer: append-on-write, redaction, and hash stability.

Anti-bias rule: expected values are computed by hand or by an independent path (reading the jsonl back),
never by calling the function under test twice and comparing it with itself, except where identity
across runs IS the property (hash stability), in which case the differing volatile inputs are asserted
to actually differ first.
"""
import json
import time

from app.trace import Tracer, canonical_hash, redact, strip_volatile


def test_entries_are_written_incrementally_to_jsonl(tmp_path):
    t = Tracer(mode="test", rule="Opportunity.X", traces_dir=tmp_path)
    t.note("first")
    t.tool_call("salesforce.query", {"q": "SELECT 1"}, response={"size": 0})
    # read the jsonl back BEFORE finalize: a crash after these two lines must still leave both entries
    lines = t.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # header + 2 entries
    assert json.loads(lines[0])["header"]["rule"] == "Opportunity.X"
    e1, e2 = json.loads(lines[1]), json.loads(lines[2])
    assert (e1["seq"], e1["kind"], e1["message"]) == (1, "note", "first")
    assert (e2["seq"], e2["kind"], e2["tool"], e2["response"]) == (2, "tool_call", "salesforce.query", {"size": 0})


def test_span_records_latency_and_reraises_with_error_captured(tmp_path):
    t = Tracer(mode="test", rule="r", traces_dir=tmp_path)
    try:
        with t.span("tool_call", tool="airtable.write"):
            time.sleep(0.01)
            raise RuntimeError("503 upstream")
    except RuntimeError:
        pass
    else:
        raise AssertionError("span must re-raise")
    entry = t.entries[-1]
    assert entry["error"] == {"type": "RuntimeError", "message": "503 upstream"}
    assert entry["latency_ms"] >= 5  # slept 10ms; allow timer coarseness


def test_redaction_removes_known_secret_shapes_and_configured_values():
    secrets = ["super-secret-pat-value"]
    s = ("Authorization: Bearer 00Dg700000Fkiv3EAB!AQEAQabcdefghijklmnop key=sk-or-v1-abcdefghijklmnopqrstuvwxyz0123 "
         "pat=patABCDEFGHIJKLMN.abcdefghijklmnopqrstuvwxyz0123456789 "
         "hook=https://discord.com/api/webhooks/1548736153039937590/NCfVM99c5ahj19uI173vvBLhoD8fEp0nOFRV "
         "val=super-secret-pat-value")
    out = redact(s, secrets)
    for leaked in ("00Dg700000Fkiv3EAB!AQEAQ", "sk-or-v1-abcdefghijkl", "patABCDEFGHIJKLMN.abcdef", "NCfVM99c5ahj19uI173vv", "super-secret-pat-value"):
        assert leaked not in out, leaked
    assert "webhooks/1548736153039937590/<redacted>" in out  # webhook id kept, token gone
    assert out.count("<redacted>") == 5
    # nested structures
    assert redact({"a": ["x sk-or-v1-abcdefghijklmnopqrstuvwxyz0123"]}, secrets) == {"a": ["x <redacted>"]}


def test_redaction_leaves_ordinary_text_untouched():
    plain = 'ISPICKVAL(StageName,"Closed Won") && Amount <= 0'
    assert redact(plain, ["nothing-here"]) == plain


def test_strip_volatile_removes_only_volatile_keys_at_any_depth():
    doc = {"ts": 1, "kind": "x", "nested": [{"latency_ms": 3, "keep": {"run_id": "r", "value": 7}}]}
    assert strip_volatile(doc) == {"kind": "x", "nested": [{"keep": {"value": 7}}]}


def test_canonical_hash_ignores_volatile_fields_but_not_content(tmp_path):
    def make(note: str, sleep: float) -> Tracer:
        t = Tracer(mode="test", rule="r", case_id="c", traces_dir=tmp_path)
        time.sleep(sleep)
        t.note(note)
        t.finalize({"verdict": "PASS"})
        return t

    a, b = make("same", 0.0), make("same", 0.02)
    # the volatile inputs really differ ...
    assert a.run_id != b.run_id and a.entries[0]["ts"] != b.entries[0]["ts"]
    # ... and the hashes do not
    ha = json.loads(a.json_path.read_text(encoding="utf-8"))["canonical_hash"]
    hb = json.loads(b.json_path.read_text(encoding="utf-8"))["canonical_hash"]
    assert ha == hb
    # content change -> different hash
    c = make("different", 0.0)
    assert json.loads(c.json_path.read_text(encoding="utf-8"))["canonical_hash"] != ha


def test_canonical_hash_is_sha256_of_sorted_json():
    # independent recomputation of the hash for a tiny fixed input
    import hashlib
    entries = [{"seq": 1, "ts": "T", "kind": "note", "message": "m"}]
    header = {"run_id": "x", "mode": "test"}
    expected = hashlib.sha256(json.dumps({"header": {"mode": "test"}, "entries": [{"kind": "note", "message": "m", "seq": 1}]},
                                         sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert canonical_hash(entries, header) == expected


def test_finalize_writes_json_with_summary_and_entries(tmp_path):
    t = Tracer(mode="live", rule="Opportunity.R", case_id="case1", attempt=2, traces_dir=tmp_path)
    t.assumption("blank_is_null", "blank number is null in SF validation rules", "seed probe 2026-09-13", True)
    doc = t.finalize({"verdict": "AMBIGUOUS", "unsupported_claims": 0})
    on_disk = json.loads(t.json_path.read_text(encoding="utf-8"))
    assert on_disk["summary"] == {"verdict": "AMBIGUOUS", "unsupported_claims": 0}
    assert on_disk["header"]["attempt"] == 2 and on_disk["header"]["mode"] == "live"
    assert on_disk["entries"][0]["kind"] == "assumption" and on_disk["entries"][0]["verified"] is True
    assert on_disk["canonical_hash"] == doc["canonical_hash"] and len(doc["canonical_hash"]) == 64
