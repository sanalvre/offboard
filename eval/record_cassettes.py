"""Record LLM cassettes for the eval cases: mocks for Salesforce/Airtable, LIVE OpenRouter, save the raw response.

    python -m eval.record_cassettes                # all cases that need a cassette and lack one
    python -m eval.record_cassettes happy_closedwon_amount --force

The `weaker_translation` cassette is derived by hand from the hero cassette (see make_weaker_cassette) so the
solver's off-by-one catch is demonstrable regardless of what the model happens to produce.
"""
from __future__ import annotations

import json
import sys

from app.adapters.airtable import MockAirtable
from app.adapters.discord import MockDiscord
from app.adapters.salesforce import MockSalesforce
from app.config import settings
from app.llm import Proposer
from app.models import RunRequest
from app.pipeline import Pipeline
from app.trace import Tracer
from eval.cases import CASES

CASSETTES = settings.fixtures_dir / "cassettes"


def record(case_id: str, rule: str, variant: str = "informed") -> None:
    tracer = Tracer(mode="test", rule=rule, case_id=f"record:{case_id}", traces_dir=settings.root / "state" / "record_traces")
    at = MockAirtable(tracer)
    at.reset()
    p = Pipeline(tracer, MockSalesforce(tracer), at, MockDiscord(tracer), Proposer(tracer, "record"), mode="test")
    summary = p.run(RunRequest(rule=rule, case_id=case_id, cassette=case_id, prompt_variant=variant))
    at.reset()
    d = summary.decision
    print(f"{case_id:36s} {summary.verdict.value:10s} system={d.system_confidence:.2f} llm={d.llm_confidence} gap={d.overconfidence_gap}")


def derive(src_key: str, dst_key: str, new_formula: str, note: str) -> None:
    """Hand-edited cassette: same recorded response, guard formula replaced. Labelled inside the file."""
    cas = json.loads((CASSETTES / f"{src_key}.json").read_text(encoding="utf-8"))
    content = json.loads(cas["raw_response"]["choices"][0]["message"]["content"])
    original = content["guard_formula"]
    content["guard_formula"] = new_formula
    content["rationale"] = f"[HAND-EDITED CASSETTE: {note}; original guard was {original!r}] " + content["rationale"]
    cas["raw_response"]["choices"][0]["message"]["content"] = json.dumps(content)
    cas.update(key=dst_key, derived_from=src_key, hand_edited=True, hand_edit_note=note)
    (CASSETTES / f"{dst_key}.json").write_text(json.dumps(cas, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{dst_key}: {original}  ->  {new_formula}")


def make_derived_cassettes() -> None:
    derive("happy_closedwon_amount", "weaker_translation", 'IF(AND({Stage} = "Closed Won", {Amount} < 0), "VIOLATION", "")',
           "boundary weakened from <= 0 to < 0 for eval case solver_catches_weaker_translation")
    derive("calibration_null_semantics", "naive_null_translation", 'IF(AND({Stage} = "Closed Won", {Amount} <= 0), "VIOLATION", "")',
           "literal translation without a blank guard for eval case solver_catches_naive_null_translation")
    derive("trap_percent_units", "percent_units_literal", 'IF({Discount} > 50, "VIOLATION", "")',
           "Salesforce literal copied without rescaling for eval case solver_catches_percent_units")


def make_weaker_cassette() -> None:
    src = CASSETTES / "happy_closedwon_amount.json"
    dst = CASSETTES / "weaker_translation.json"
    cas = json.loads(src.read_text(encoding="utf-8"))
    content = json.loads(cas["raw_response"]["choices"][0]["message"]["content"])
    original = content["guard_formula"]
    if "<=0" in original.replace(" ", ""):
        content["guard_formula"] = original.replace("<= 0", "< 0").replace("<=0", "<0")
    else:
        raise SystemExit(f"hero guard has no '<= 0' to weaken: {original}")
    content["rationale"] = "[HAND-EDITED CASSETTE for eval case solver_catches_weaker_translation: '<= 0' changed to '< 0'] " + content["rationale"]
    cas["raw_response"]["choices"][0]["message"]["content"] = json.dumps(content)
    cas["key"] = "weaker_translation"
    cas["derived_from"] = "happy_closedwon_amount"
    cas["hand_edited"] = True
    dst.write_text(json.dumps(cas, indent=2, ensure_ascii=False), encoding="utf-8")
    print("weaker_translation cassette written from", original, "->", content["guard_formula"])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    need = [c for c in CASES if c.cassette is None and c.needs_llm]
    for c in need:
        if args and c.case_id not in args:
            continue
        if (CASSETTES / f"{c.case_id}.json").exists() and not force:
            print(f"{c.case_id}: cassette exists, skipping")
            continue
        record(c.case_id, c.rule, c.prompt_variant)
    if not args or "derived" in args:
        make_derived_cassettes()
