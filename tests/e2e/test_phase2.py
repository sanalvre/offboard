"""Phase 2 end-to-end in test mode (no network): formula field with behavioural probe, flow blocked by the solver,
non-verifiable artefact surfaced, cross-type inventory, and a transformation counterexample carrying both outputs."""
import json

import httpx
import pytest
import subprocess
from fastapi.testclient import TestClient

from app.adapters import airtable as at_mod
from app.main import app

H = {"X-API-Key": "sk_test_offboard_demo"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError(f"network call attempted in test mode: {a[:2]}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(at_mod.MockAirtable, "STATE", tmp_path / "mock_airtable.json")
    c = TestClient(app)
    c.post("/state/reset", headers=H)
    return c


def run(client, **body):
    r = client.post("/runs", json=body, headers=H)
    assert r.status_code == 200, r.text
    s = r.json()
    return s, json.load(open(s["trace_path"], encoding="utf-8"))


def test_formula_field_proven_written_and_probed(client):
    s, doc = run(client, rule="Opportunity.Net_Amount__c", case_id="e2e_net", cassette="formula_net_amount")
    assert s["verdict"] == "PASS" and s["unsupported_claims"] == 0
    claims = {c["kind"]: c for c in s["claims"]}
    assert claims["field_created"]["verified"] and claims["behavioural_check"]["verified"] and claims["probe_deleted"]["verified"]
    assert "900" in claims["behavioural_check"]["detail"]
    diff = doc["summary"]["state_diff"]
    assert diff["fields_added"] == {"Opportunities": ["Net Amount"]} and "Opportunities" not in diff["records_added"]
    assert doc["summary"]["unexpected_changes"] == []
    ir = [e for e in doc["entries"] if e["kind"] == "rule_ir"][0]
    assert ir["artefact_type"] == "FormulaField" and ir["blanks_as"] == "BlankAsBlank" and ir["expr"]["op"] == "sub"


def test_literal_copy_blocked_with_both_outputs(client):
    s, doc = run(client, rule="Opportunity.Net_Amount__c", case_id="e2e_copy", cassette="net_amount_literal_copy")
    assert s["verdict"] == "FAIL"
    sol = [e for e in doc["entries"] if e["kind"] == "solver"][-1]
    assert sol["status"] == "not_equivalent" and sol["outputs"]["source"] != sol["outputs"]["target"]
    assert doc["summary"]["state_diff"]["fields_added"] == {}


def test_flow_blocked_on_blank_probability(client):
    s, doc = run(client, rule="Flow.Flag_Stale_Negotiation", case_id="e2e_flow", cassette="flow_flag_stale_negotiation")
    assert s["verdict"] == "FAIL"
    sol = [e for e in doc["entries"] if e["kind"] == "solver"][-1]
    assert sol["counterexample"]["Probability"] is None and sol["source_fires"] is False and sol["target_flags"] is True
    ir = [e for e in doc["entries"] if e["kind"] == "rule_ir"][0]
    assert ir["artefact_type"] == "Flow" and ir["actions"][0]["field"] == "Needs_Review__c"


def test_non_verifiable_flow_is_surfaced_without_model_call(client):
    s, doc = run(client, rule="Flow.sfdc_default_ReportExport_Protection_Flow", case_id="e2e_tsf")
    assert s["verdict"] == "AMBIGUOUS" and s["decision"]["system_confidence"] == 0.0
    assert not [e for e in doc["entries"] if e["kind"] == "llm_call"]
    assert "TransactionSecurityFlow" in s["decision"]["reasons"][0]


def test_unknown_artefact_is_not_found(client):
    s, _ = run(client, rule="Flow.Does_Not_Exist", case_id="e2e_nf2")
    assert s["verdict"] == "NOT_FOUND"


def test_inventory_covers_all_types_with_reasons(client):
    inv = client.get("/inventory", headers=H).json()
    assert inv["found"] == 15 and inv["verifiable"] == 12
    assert inv["by_type"] == {"ValidationRule": 8, "FormulaField": 2, "Flow": 2, "WorkflowFieldUpdate": 1, "AssignmentRule": 2}
    assert all(a["reason"] for a in inv["artefacts"])
    assert sum(inv["counts"].values()) == 15 and inv["counts"]["not_verifiable"] == 3
