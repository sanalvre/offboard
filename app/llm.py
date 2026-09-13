"""LLM proposer via OpenRouter (OpenAI-compatible chat completions, JSON-schema structured output).

Modes:
  live     : call OpenRouter, record the exact request and raw response in the trace
  cassette : replay fixtures/cassettes/<key>.json (no network, deterministic); used by test mode
  record   : call OpenRouter AND save the cassette (developer step: `python -m eval.record_cassettes`)

The prompt gives the model the rule formula, the business-intent description, the error message, the
Salesforce field metadata (types, scale, options) and the Airtable field metadata, and asks for a guard
formula plus a self-assessed confidence. The pipeline never trusts the answer: the parser and solver do.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from .config import settings
from .models import FieldSpec, Proposal, RuleIR
from .trace import Tracer

LLMMode = Literal["live", "cassette", "record"]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a CRM migration engineer translating ONE Salesforce validation rule into an Airtable guard formula.

Airtable has no validation rules. The guard is a formula field that returns the text "VIOLATION" when a record
violates the business rule and "" otherwise. Use exactly this shape: IF(<condition>, "VIOLATION", "").
Inside <condition> use only: AND(), OR(), NOT(), {Field} = "text", {Field} = BLANK(), {Field} != BLANK(),
numeric comparisons (= != < <= > >=) against literals, TRUE(), FALSE(). Nothing else (no FIND, LEN, IF nesting, dates).

Be precise about semantics; they differ between systems:
- Salesforce validation rules: a blank number is null; any numeric comparison on a blank is FALSE. ISPICKVAL compares
  the picklist API name. Percent fields store 50 for 50%.
- Airtable: a blank number is treated as 0 in comparisons; {Percent} stores 0.5 for 50%; a single select compares
  against its option label.
Field names differ between systems; map each Salesforce field to the Airtable field with the same meaning, using
only the Airtable fields listed. If a Salesforce field has no Airtable counterpart, say so in caveats and still
produce your best mapping.

Return JSON only, matching the schema. Your confidence is the probability that your guard is EXACTLY equivalent
to the Salesforce rule for every possible record. Do not round up.
"""


NAIVE_SYSTEM_PROMPT = SYSTEM_PROMPT.split("Be precise about semantics")[0] + """Field names differ between systems; map each Salesforce field to the Airtable field with the same meaning, using
only the Airtable fields listed.

Return JSON only, matching the schema. Your confidence is the probability that your guard is EXACTLY equivalent
to the Salesforce rule for every possible record.
"""


def proposal_schema() -> dict[str, Any]:
    schema = Proposal.model_json_schema()
    # OpenRouter/OpenAI strict mode needs additionalProperties false on every object
    def tighten(s: Any) -> None:
        if isinstance(s, dict):
            if s.get("type") == "object":
                s["additionalProperties"] = False
                s["required"] = list(s.get("properties", {}).keys())
            for v in s.values():
                tighten(v)
        elif isinstance(s, list):
            for v in s:
                tighten(v)
    tighten(schema)
    return schema


def _fields_block(fields: dict[str, FieldSpec], system: str, variant: str = "informed") -> str:
    lines = []
    for f in fields.values():
        extra = ""
        if f.type == "picklist":
            extra = " options=" + json.dumps([o.label if system == "airtable" else o.api_name for o in f.options])
        if f.type == "percent" and variant == "informed":
            extra += " (stores 50 for 50%)" if f.scale == 100 else " (stores 0.5 for 50%)"
        lines.append(f"- {f.name}: {f.type}{'' if f.nullable else ', required'}{extra}")
    return "\n".join(lines)


KIND_INSTRUCTIONS = {
    "rule": "",
    "formula": """This artefact is a FORMULA FIELD, not a validation rule. Produce an Airtable formula that COMPUTES THE SAME VALUE
for every record (a number, in Airtable's units: currency as the amount, percent as 0.5 for 50%), not a guard.
Put it in guard_formula and the Airtable field name (usually the Salesforce label) in guard_field_name. Salesforce
formula fields with "treat blanks as blanks" return blank when any operand is blank unless BLANKVALUE guards it;
Airtable treats blank operands as 0. To return a blank RESULT (as Salesforce does when an operand is blank under
"treat blanks as blanks"), use IF({F} = BLANK(), BLANK(), <expression>). Use only + - * / parentheses,
IF(<condition>, a, b), IF({F} = BLANK(), d, {F}) as the operand guard, BLANK(), MIN(a,b), MAX(a,b), numbers and
{Field} references.

""",
    "flow": """This artefact is a RECORD-TRIGGERED FLOW whose only action assigns a checkbox on the same record. The equivalent in
Airtable is a computed formula field that is 1 exactly when the flow's entry conditions AND the decision outcome hold,
else 0. Produce that formula in guard_formula using IF(<condition>, 1, 0) and name the field after the flow's target
field in guard_field_name. The condition is given to you as a Salesforce-style boolean expression.

""",
}


def build_messages(ir: RuleIR, at_fields: dict[str, FieldSpec], variant: str = "informed", kind: str = "rule") -> list[dict[str, str]]:
    src = ir.source
    user = f"""{KIND_INSTRUCTIONS.get(kind, "")}Salesforce object: {src.object}
Rule API name: {src.name}
{"Error condition formula (the rule FIRES, i.e. blocks the save, when this is TRUE)" if kind == "rule" else "Formula / condition"}:
{src.formula}
Error message shown to users: {src.error_message or "(none)"}
Admin description (business intent): {src.description or "(none)"}
Active: {src.active}

Salesforce fields referenced or available:
{_fields_block({k: v for k, v in ir.fields.items() if k in (ir.condition.fields() if ir.condition else set())} or ir.fields, "salesforce", variant)}

Airtable table "Opportunities" fields available:
{_fields_block(at_fields, "airtable", variant)}

Produce the Airtable guard."""
    return [{"role": "system", "content": SYSTEM_PROMPT if variant == "informed" else NAIVE_SYSTEM_PROMPT}, {"role": "user", "content": user}]


class Proposer:
    def __init__(self, tracer: Tracer, mode: LLMMode, model: Optional[str] = None, cassette_dir: Optional[Path] = None):
        self.tracer = tracer
        self.mode = mode
        self.model = model or settings.openrouter_model
        self.cassette_dir = Path(cassette_dir or settings.fixtures_dir / "cassettes")

    def _cassette_path(self, key: str) -> Path:
        return self.cassette_dir / f"{key}.json"

    def propose(self, ir: RuleIR, at_fields: dict[str, FieldSpec], cassette_key: str, variant: str = "informed", kind: str = "rule") -> Proposal:
        messages = build_messages(ir, at_fields, variant, kind)
        rf = {"type": "json_schema", "json_schema": {"name": "proposal", "strict": True, "schema": proposal_schema()}}
        request = {"model": self.model, "messages": messages, "response_format": rf, "max_tokens": 4000}
        t0 = time.perf_counter()
        if self.mode == "cassette":
            path = self._cassette_path(cassette_key)
            if not path.exists():
                self.tracer.llm_call(self.model, messages, rf, None, None, "cassette", cassette_key, 0.0,
                                     error={"type": "CassetteMissing", "message": str(path)})
                raise FileNotFoundError(f"no cassette for {cassette_key}: {path}")
            cas = json.loads(path.read_text(encoding="utf-8"))
            raw = cas["raw_response"]
            model = cas.get("model", self.model)
        else:
            if not settings.openrouter_api_key:
                raise RuntimeError("OPENROUTER_API_KEY not configured")
            r = httpx.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {settings.openrouter_api_key}",
                                                     "HTTP-Referer": "https://github.com/sanalvre/offboard", "X-Title": "OffBoard"},
                           json=request, timeout=120)
            raw = r.json()
            model = self.model
            if r.status_code >= 400 or "choices" not in raw:
                self.tracer.llm_call(model, messages, rf, raw, None, self.mode, cassette_key, round((time.perf_counter() - t0) * 1000, 1),
                                     error={"type": "HTTPError", "status": r.status_code})
                raise RuntimeError(f"OpenRouter {r.status_code}: {json.dumps(raw)[:300]}")
            if self.mode == "record":
                self.cassette_dir.mkdir(parents=True, exist_ok=True)
                self._cassette_path(cassette_key).write_text(json.dumps(
                    {"key": cassette_key, "model": model, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "request": request, "raw_response": raw}, indent=2, ensure_ascii=False), encoding="utf-8")
        content = raw["choices"][0]["message"]["content"]
        try:
            parsed = Proposal.model_validate_json(content)
        except Exception as e:  # noqa: BLE001
            self.tracer.llm_call(model, messages, rf, raw, None, self.mode, cassette_key, round((time.perf_counter() - t0) * 1000, 1),
                                 error={"type": "ParseError", "message": str(e)})
            raise
        self.tracer.llm_call(model, messages, rf, raw, parsed.model_dump(), self.mode, cassette_key, round((time.perf_counter() - t0) * 1000, 1))
        self.tracer.note(f"prompt variant: {variant}", prompt_variant=variant)
        return parsed
