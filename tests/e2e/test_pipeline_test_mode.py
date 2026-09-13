"""End-to-end through the FastAPI app in test mode.

What this proves that the unit tests cannot:
  - test mode makes ZERO outbound network calls (httpx and subprocess are patched to raise)
  - the mode router rejects unknown keys
  - verdicts, claims and the bounded-state invariant hold across a realistic sequence (PASS, then DUPLICATE)
  - the eval harness's `unsafe` verdict actually fires when the mock performs an unintended write
    (a graded eval must be shown to fail on a bad run, or its passes mean nothing)
"""
import json
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import airtable as at_mod
from app.main import app
from eval.cases import CASES_BY_ID
from eval.run_eval import grade

H = {"X-API-Key": "sk_test_offboard_demo"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError(f"network call attempted in test mode: {a[:2]}")
    # TestClient is an httpx.Client over an ASGI transport, so block only the real network transport
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    # isolate mock state
    monkeypatch.setattr(at_mod.MockAirtable, "STATE", tmp_path / "mock_airtable.json")
    c = TestClient(app)
    c.post("/state/reset", headers=H)
    return c


def run(client, **body):
    r = client.post("/runs", json=body, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_or_missing_key_is_rejected(client):
    assert client.post("/runs", json={"rule": "Opportunity.X"}).status_code == 401
    assert client.post("/runs", json={"rule": "Opportunity.X"}, headers={"X-API-Key": "sk_live_not_the_configured_one"}).status_code == 401


def test_hero_then_duplicate_with_bounded_state(client):
    first = run(client, rule="Opportunity.ClosedWon_Requires_Amount", case_id="e2e_hero", cassette="happy_closedwon_amount")
    assert first["verdict"] == "PASS" and first["unsupported_claims"] == 0
    kinds = [c["kind"] for c in first["claims"]]
    assert "field_created" in kinds and "record_written" in kinds and "rule_verified_equivalent" in kinds
    doc = json.load(open(first["trace_path"], encoding="utf-8"))
    assert doc["summary"]["unexpected_changes"] == []
    assert doc["summary"]["state_diff"]["fields_added"] == {"Opportunities": ["Guard: ClosedWon_Requires_Amount"]}
    assert list(doc["summary"]["state_diff"]["records_added"]) == ["Migration_Rules"]
    # the LLM call is replayed, never live
    llm = [e for e in doc["entries"] if e["kind"] == "llm_call"]
    assert len(llm) == 1 and llm[0]["mode"] == "cassette"

    second = run(client, rule="Opportunity.ClosedWon_Requires_Amount", case_id="e2e_dup", cassette="happy_closedwon_amount")
    assert second["verdict"] == "DUPLICATE"
    doc2 = json.load(open(second["trace_path"], encoding="utf-8"))
    assert not [e for e in doc2["entries"] if e["kind"] == "llm_call"]
    assert doc2["summary"]["state_diff"]["records_added"] == {} and doc2["summary"]["state_diff"]["fields_added"] == {}


def test_not_found_and_unsupported_make_no_llm_call(client):
    nf = run(client, rule="Opportunity.Nope", case_id="e2e_nf")
    assert nf["verdict"] == "NOT_FOUND"
    amb = run(client, rule="Opportunity.Stage_Regression_Blocked", case_id="e2e_amb")
    assert amb["verdict"] == "AMBIGUOUS" and amb["decision"]["system_confidence"] == 0.0
    for s in (nf, amb):
        doc = json.load(open(s["trace_path"], encoding="utf-8"))
        assert not [e for e in doc["entries"] if e["kind"] == "llm_call"]


def test_inventory_reports_coverage(client):
    run(client, rule="Opportunity.ClosedWon_Requires_Amount", case_id="e2e_inv", cassette="happy_closedwon_amount")
    inv = client.get("/inventory", headers=H).json()
    by = {a["key"]: a for a in inv["artefacts"]}
    assert by["Opportunity.ClosedWon_Requires_Amount"]["status"] == "proven"
    assert inv["by_type"]["ValidationRule"] == 8 and inv["by_type"]["Flow"] == 2 and inv["by_type"]["FormulaField"] == 2
    assert sum(inv["counts"].values()) == inv["found"] and inv["verifiable"] < inv["found"]
    assert all(a["reason"] for a in inv["artefacts"])  # every artefact says why it is or is not verifiable


def test_eval_grader_flags_unsafe_when_mock_writes_something_unintended(client, monkeypatch):
    """Force a rogue write inside the mock and prove the harness downgrades the run to `unsafe`."""
    orig = at_mod.MockAirtable._create_field

    def rogue(self, table, body):
        res = orig(self, table, body)
        # unintended side effect: touch a distractor table
        self._t("Migration_Agents")["records"].append({"id": "recROGUE", "createdTime": "2026-01-01T00:00:00.000Z", "fields": {"Agent Name": "oops"}})
        self._save()
        return res

    monkeypatch.setattr(at_mod.MockAirtable, "_create_field", rogue)
    summary = run(client, rule="Opportunity.ClosedWon_Requires_Amount", case_id="e2e_rogue", cassette="happy_closedwon_amount")
    doc = json.load(open(summary["trace_path"], encoding="utf-8"))
    assert doc["summary"]["unexpected_changes"], "pipeline must report the unintended change"
    attempt = grade(CASES_BY_ID["distractor_near_duplicate"], summary, "PASS")
    assert attempt.eval_verdict == "unsafe"
    assert any("UNSAFE" in f for f in attempt.failures)
