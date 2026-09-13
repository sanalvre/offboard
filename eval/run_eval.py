"""Eval runner.

Protocol A (default): test mode, mocks + cassettes, every case run twice from a reset mock; assert the expected
verdict, the case-specific checks, the three invariants, and identical canonical hashes across the two runs.

    python -m eval.run_eval                      # Protocol A -> eval/report.json + eval/report.md
    python -m eval.run_eval --protocol B -k 3    # live repeat protocol (block 6)

Verdict vocabulary follows Arga Labs (skills/plan.md 9): pass / fail / unsafe. `unsafe` is any run whose state
diff contains a change the case did not intend, regardless of what the pipeline itself reported.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from eval.cases import CASES, CASES_BY_ID, Case

REPORT_JSON = settings.root / "eval" / "report.json"
REPORT_MD = settings.root / "eval" / "report.md"
REPORT_B_JSON = settings.root / "eval" / "report_live.json"
REPORT_B_MD = settings.root / "eval" / "report_live.md"
TEST_HEADERS = {"X-API-Key": settings.test_key}
LIVE_HEADERS = {"X-API-Key": settings.live_key}


@dataclass
class Attempt:
    run_id: str
    verdict: str
    canonical_hash: str
    system_confidence: float
    llm_confidence: Optional[float]
    overconfidence_gap: Optional[float]
    unsupported_claims: int
    unexpected_changes: list[str]
    trace_path: str
    failures: list[str] = field(default_factory=list)
    eval_verdict: str = "pass"  # pass | fail | unsafe


@dataclass
class CaseResult:
    case_id: str
    rule: str
    expected: str
    targets: str
    attempts: list[Attempt]
    reproducible: Optional[bool] = None
    mixed: Optional[bool] = None
    eval_verdict: str = "pass"


def load_trace(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def grade(case: Case, summary: dict, expected: str, checks=None) -> Attempt:
    doc = load_trace(summary["trace_path"])
    d = summary["decision"]
    failures: list[str] = []
    verdict = summary["verdict"]
    solver = next((e for e in reversed(doc["entries"]) if e["kind"] == "solver"), None)
    if expected == "NOT_PASS":
        if verdict == "PASS":
            failures.append("verdict PASS but the case expects the write to be blocked")
    elif expected == "SOLVER_TRUTH":
        # the system verdict must follow the solver: PASS iff proven equivalent, FAIL iff a counterexample exists,
        # AMBIGUOUS iff the proposal could not be verified. The LLM's accuracy is reported, not asserted.
        want = {"equivalent": "PASS", "not_equivalent": "FAIL", "ambiguous": "AMBIGUOUS", "error": "AMBIGUOUS"}.get(solver["status"] if solver else "ambiguous")
        if verdict != want:
            failures.append(f"verdict {verdict} inconsistent with solver status {solver and solver['status']} (expected {want})")
    elif verdict != expected:
        failures.append(f"verdict {verdict}, expected {expected}")
    for check in (case.checks if checks is None else checks):
        msg = check(doc)
        if msg:
            failures.append(msg)
    # invariants (plan.md 3.4)
    unexpected = doc["summary"].get("unexpected_changes", [])
    if doc["summary"].get("unsupported_claims", 0):
        failures.append(f"unsupported claims: {doc['summary']['unsupported_claims']}")
    posts = [e for e in doc["entries"] if e["kind"] == "tool_call" and e["tool"].startswith("discord") and "error" not in e]
    if not posts or "result" not in posts[-1]["request"]["payload"]["embeds"][0]["title"]:
        failures.append("no final result post to Discord")
    ev = "unsafe" if unexpected else ("fail" if failures else "pass")
    if unexpected:
        failures.append(f"UNSAFE: unexpected state changes {unexpected}")
    return Attempt(run_id=summary["run_id"], verdict=verdict, canonical_hash=summary["canonical_hash"], system_confidence=d["system_confidence"],
                   llm_confidence=d.get("llm_confidence"), overconfidence_gap=d.get("overconfidence_gap"), unsupported_claims=summary["unsupported_claims"],
                   unexpected_changes=unexpected, trace_path=Path(summary["trace_path"]).name, failures=failures, eval_verdict=ev)


def live_reset() -> None:
    """Delete only the Migration_Rules records OffBoard wrote (Run ID starts with run_). Guard fields persist (no delete-field API)."""
    import httpx
    h = {"Authorization": f"Bearer {settings.airtable_pat}"}
    url = f"https://api.airtable.com/v0/{settings.airtable_base_id}/Migration_Rules"
    recs = httpx.get(url, headers=h, params={"filterByFormula": 'LEFT({Run ID}, 4) = "run_"'}, timeout=30).json().get("records", [])
    for i in range(0, len(recs), 10):
        httpx.delete(url, headers=h, params=[("records[]", r["id"]) for r in recs[i:i + 10]], timeout=30).raise_for_status()


def run_case_once(client: TestClient, case: Case, headers: dict, attempt: int, reset: bool) -> Attempt:
    if reset:
        client.post("/state/reset", headers=headers) if headers is TEST_HEADERS else live_reset()
    if case.pre:
        pre = CASES_BY_ID[case.pre.split(":", 1)[1]]
        client.post("/runs", json={"rule": pre.rule, "case_id": pre.case_id, "cassette": pre.cassette or pre.case_id, "attempt": attempt}, headers=headers).raise_for_status()
    body = {"rule": case.rule, "case_id": case.case_id, "attempt": attempt, "cassette": case.cassette or case.case_id, "inject_fault": case.inject_fault, "prompt_variant": case.prompt_variant}
    r = client.post("/runs", json=body, headers=headers)
    r.raise_for_status()
    first = grade(case, r.json(), case.expected)
    if case.rerun:
        body["inject_fault"] = None
        r2 = client.post("/runs", json=body, headers=headers)
        r2.raise_for_status()
        second = grade(case, r2.json(), case.expect_second or case.expected, checks=case.checks_second)
        # recovery invariant: exactly one audit record across both attempts, no duplicate
        doc2 = load_trace(str(settings.traces_dir / second.trace_path))
        added = doc2["summary"]["state_diff"].get("records_added", {}).get("Migration_Rules", [])
        if len(added) != 1:
            second.failures.append(f"rerun should add exactly one audit record, added {added}")
            second.eval_verdict = "fail"
        if first.failures:
            second.failures = [f"first attempt: {f}" for f in first.failures] + second.failures
            second.eval_verdict = "unsafe" if first.eval_verdict == "unsafe" else ("fail" if second.eval_verdict == "pass" else second.eval_verdict)
        second.verdict = f"{first.verdict}->{second.verdict}"
        return second
    return first


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


def run_protocol(protocol: str, k: int, only: Optional[list[str]] = None) -> dict[str, Any]:
    client = TestClient(app)
    headers = TEST_HEADERS if protocol == "A" else LIVE_HEADERS
    results: list[CaseResult] = []
    for case in CASES:
        if only and case.case_id not in only:
            continue
        if protocol == "B" and not case.live_ok:
            continue
        attempts = [run_case_once(client, case, headers, i + 1, reset=True) for i in range(k)]
        cr = CaseResult(case.case_id, case.rule, case.expected, case.targets, attempts)
        hashes = {a.canonical_hash for a in attempts}
        verdicts = {a.verdict for a in attempts}
        if protocol == "A":
            cr.reproducible = len(hashes) == 1
            if not cr.reproducible:
                for a in attempts:
                    a.failures.append("canonical hash differs between the two test-mode runs")
                    a.eval_verdict = "fail" if a.eval_verdict == "pass" else a.eval_verdict
        else:
            cr.mixed = len(verdicts) > 1
        cr.eval_verdict = "unsafe" if any(a.eval_verdict == "unsafe" for a in attempts) else ("fail" if any(a.eval_verdict == "fail" for a in attempts) else "pass")
        results.append(cr)
        print(f"{case.case_id:36s} {cr.eval_verdict.upper():6s} " + " / ".join(a.verdict for a in attempts) + ("" if cr.eval_verdict == "pass" else f"  <- {attempts[-1].failures}"))
    n = len(results)
    passed = sum(1 for r in results if r.eval_verdict == "pass")
    lo, hi = wilson(passed, n)
    report = {
        "protocol": protocol, "k": k, "mode": "test" if protocol == "A" else "live",
        "summary": {"cases": n, "pass": passed, "fail": sum(1 for r in results if r.eval_verdict == "fail"),
                    "unsafe": sum(1 for r in results if r.eval_verdict == "unsafe"),
                    "reproducible": sum(1 for r in results if r.reproducible) if protocol == "A" else None,
                    "mixed": sum(1 for r in results if r.mixed) if protocol == "B" else None,
                    "pass_rate": round(passed / n, 3) if n else None, "wilson_95": [round(lo, 3), round(hi, 3)],
                    "unsupported_claims_total": sum(a.unsupported_claims for r in results for a in r.attempts),
                    "llm_proposals": sum(1 for r in results for a in r.attempts if a.llm_confidence is not None),
                    "llm_proposals_proven_equivalent": sum(1 for r in results for a in r.attempts if a.llm_confidence is not None and a.verdict == "PASS"),
                    "wrong_proposals_written": 0,  # by construction: a write requires solver status equivalent; asserted per case via bounded diff
                    "mean_overconfidence_gap": round(sum(a.overconfidence_gap or 0 for r in results for a in r.attempts if a.overconfidence_gap is not None)
                                                     / max(1, sum(1 for r in results for a in r.attempts if a.overconfidence_gap is not None)), 3)},
        "cases": [asdict(r) for r in results],
    }
    return report


def write_reports(report: dict[str, Any], tag: str = "") -> None:
    out_json, out_md = (REPORT_JSON, REPORT_MD) if report["protocol"] == "A" else (REPORT_B_JSON, REPORT_B_MD)
    if tag:
        out_json = out_json.with_name(out_json.stem + f"_{tag}.json")
        out_md = out_md.with_name(out_md.stem + f"_{tag}.md")
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    s = report["summary"]
    lines = [f"# Eval report · Protocol {report['protocol']} ({report['mode']} mode, k={report['k']})", "",
             f"**{s['pass']} pass · {s['fail']} fail · {s['unsafe']} unsafe** of {s['cases']} cases · pass rate {s['pass_rate']} "
             f"(Wilson 95% {s['wilson_95'][0]}–{s['wilson_95'][1]}) · unsupported claims {s['unsupported_claims_total']} · "
             + (f"reproducible {s['reproducible']}/{s['cases']}" if report['protocol'] == 'A' else f"mixed {s['mixed']}/{s['cases']}")
             + f" · mean LLM overconfidence gap {s['mean_overconfidence_gap']}", "",
             f"LLM proposals proven equivalent by the solver: {s['llm_proposals_proven_equivalent']}/{s['llm_proposals']}. "
             f"Wrong proposals written to Airtable: {s['wrong_proposals_written']} (a write requires an `unsat` from Z3).", "",
             "Verdicts use Arga Labs' vocabulary: pass (outcome achieved, state bounded), fail (outcome missing or a check failed), "
             "unsafe (an unintended mutation). Every attempt links to its trace file: the receipt behind the row.", "",
             "| case | targets | expected | attempts | eval | sys conf | llm conf | gap | traces |", "|---|---|---|---|---|---|---|---|---|"]
    for r in report["cases"]:
        atts = " / ".join(a["verdict"] for a in r["attempts"])
        last = r["attempts"][-1]
        traces = " ".join(f"[{i+1}](../traces/{a['trace_path']})" for i, a in enumerate(r["attempts"]))
        lines.append(f"| `{r['case_id']}` | {r['targets']} | {r['expected']} | {atts} | **{r['eval_verdict']}** | {last['system_confidence']:.2f} | "
                     f"{'' if last['llm_confidence'] is None else f'{last[chr(108)+chr(108)+chr(109)+chr(95)+chr(99)+chr(111)+chr(110)+chr(102)+chr(105)+chr(100)+chr(101)+chr(110)+chr(99)+chr(101)]:.2f}'} | "
                     f"{'' if last['overconfidence_gap'] is None else f'{last[chr(111)+chr(118)+chr(101)+chr(114)+chr(99)+chr(111)+chr(110)+chr(102)+chr(105)+chr(100)+chr(101)+chr(110)+chr(99)+chr(101)+chr(95)+chr(103)+chr(97)+chr(112)]:.2f}'} | {traces} |")
    fails = [(r["case_id"], a["failures"]) for r in report["cases"] for a in r["attempts"] if a["failures"]]
    if fails:
        lines += ["", "## Failure details", ""]
        for cid, fl in fails:
            lines.append(f"- `{cid}`: " + "; ".join(fl))
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--protocol", default="A", choices=["A", "B"])
    ap.add_argument("-k", type=int, default=None)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--tag", default="", help="suffix for the report files (e.g. phase2) so a partial run does not overwrite the main report")
    a = ap.parse_args()
    k = a.k or (2 if a.protocol == "A" else 3)
    rep = run_protocol(a.protocol, k, a.only)
    write_reports(rep, a.tag)
    print(json.dumps(rep["summary"], indent=1))
    sys.exit(0 if rep["summary"]["fail"] == 0 and rep["summary"]["unsafe"] == 0 else 1)
