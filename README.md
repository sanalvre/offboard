# OffBoard

**Salesforce logic capture and Z3-verified migration to Airtable, with a receipt for every decision.**

Multi-App Agent Hackathon, 13 September 2026. Solo build, Python 3.10 / FastAPI. Apps touched: Salesforce
(Tooling API), Airtable (Web API), OpenRouter (Claude Sonnet 5), Discord (webhooks).

## What it does

OffBoard takes one Salesforce validation rule, asks an LLM to propose the equivalent guard in Airtable, and then
refuses to trust the LLM. A deterministic parser turns both the source rule and the proposal into the same small
boolean IR, and the Z3 SMT solver checks whether they mean the same thing for every possible record, under each
system's own semantics (a blank number is null in Salesforce and zero in Airtable; a percent is 50 in Salesforce
and 0.5 in Airtable). If Z3 proves equivalence, OffBoard writes the guard formula field and an audit record to
Airtable, snapshots the base before and after, and checks that nothing else changed. If Z3 finds a record on
which the two disagree, the write is blocked and the counterexample is the explanation. If the rule cannot be
encoded at all, it is surfaced as AMBIGUOUS for a human instead of guessed. Every LLM prompt and raw response,
every API call and response, the solver's SMT-LIB problem and result, every semantic assumption with its evidence,
and every claim the agent makes about what it did are written to a per-run JSON trace. The Discord post is the
headline; the trace file is the receipt.

## Why this matters

Agentic migrations do not only lose data; they corrupt business logic, and they do it silently. Consultancies
describe Salesforce-to-HubSpot migrations where "moving records is the cheap part" and "every workflow is rebuilt
by hand", and their verification is record counts, a 5% spot check, and two weeks of parallel running. Nobody
compares source and target logic formally. Meanwhile the traps are real and documented: a validation rule written
as `Amount <= 0` does not fire on a blank Amount in Salesforce, but its literal Airtable translation flags every
blank record; a discount cap of `50` means 50% in Salesforce and 5,000% in Airtable. A migrated guard like that
"looks complete and reports nothing useful". OffBoard is a scoped demonstration of the missing capability:
proving, not sampling, that a piece of logic means the same thing after migration, and refusing to write when it
does not. The full research behind this framing is in [`skills/problem-domain.md`](skills/problem-domain.md).

## Architecture

```
POST /runs {rule}   X-API-Key sk_test_* -> mocks + recorded LLM cassettes (deterministic, offline)
                    X-API-Key sk_live_* -> Salesforce CLI transport, OpenRouter, Airtable REST, Discord
  1 extract   Tooling API: list rules, fetch Metadata.errorConditionFormula + description + error message
  2 parse     Salesforce formula subset -> RuleIR with typed field metadata (scale, nullability, options)
  3 target    Airtable schema -> FieldSpecs; before-snapshot; idempotency lookup in Migration_Rules
  4 propose   LLM (structured JSON): field mapping, guard formula, business intent, self-reported confidence
  5 verify    Airtable formula parser -> Node; Z3: unsat = equivalent, sat = counterexample record
  6 decide    PASS (0.95) | FAIL (0.10) | AMBIGUOUS (0.0-0.3) | NOT_FOUND | DUPLICATE
  7 write     PASS only: create guard formula field; always: one audit record; read back
  8 report    after-snapshot, bounded diff, claims checked against state, Discord headline, trace finalised
```

The LLM is never in the verification path. It proposes; a parser and a solver decide. Code map:
`app/pipeline.py` (the steps), `app/parsers/` (both formula subsets), `app/solver.py` (Z3 encoding),
`app/reference.py` (an independent plain-Python interpreter the solver is tested against), `app/adapters/`
(live and mock Salesforce, Airtable, Discord), `app/llm.py` (OpenRouter with cassette record/replay),
`app/trace.py` (the receipt), `app/main.py` (API and UI), `eval/` (harness), `skills/` (design docs per feature).

## How reliability was tested

Two protocols, both reported with links to the trace behind every row.

**Protocol A, offline (`eval/report.md`).** 17 cases, each run twice from a reset mock with recorded LLM
cassettes and no network. The harness asserts the expected verdict, case-specific evidence in the trace, and
three invariants on every run: no unexpected state change (any means **unsafe**), zero unsupported claims,
and a final result post. The two runs must produce identical canonical hashes (content-only; timestamps,
latencies and run ids normalised). Result: **17 pass, 0 fail, 0 unsafe, 17/17 reproducible**.

| case | what it checks |
|---|---|
| `happy_closedwon_amount` | the hero rule: cross-field, null-safe; proven equivalent, guard written |
| `happy_closedlost_reason`, `happy_percent_scaling`, `over_refusal_guard` | more rule shapes; `over_refusal_guard` must PASS, not hide behind AMBIGUOUS |
| `record_not_found` | no LLM call, no write |
| `duplicate_already_processed` | second run finds the audit record and writes nothing |
| `ambiguous_unsupported_construct` | `PRIORVALUE` is named, confidence 0, no LLM call, human review |
| `ambiguous_schema_mismatch` | source field with no Airtable counterpart; solver refuses before solving |
| `calibration_null_semantics` (+ `_naive_prompt`) | the `Amount <= 0` trap under the production prompt and an ablation without semantics hints |
| `trap_percent_units` (+ `_naive_prompt`) | the 50 vs 0.5 trap, same two prompts |
| `solver_catches_naive_null_translation`, `solver_catches_percent_units`, `solver_catches_weaker_translation` | hand-edited cassettes (labelled inside the file) that guarantee a wrong proposal reaches the solver; each must be blocked with the right counterexample |
| `recovery_after_partial_failure` | injected 503 on the audit write; the rerun completes with exactly one record |
| `distractor_near_duplicate` | 15 pre-existing rule records and a second table must be byte-identical after the run |

LLM-dependent cases are graded as **SOLVER_TRUTH**: the system verdict must follow the proof (PASS iff Z3 says
`unsat`). The model's accuracy is *reported*, not asserted: in the current cassettes Claude Sonnet 5 produced a
proposal proven equivalent in 12 of 28 attempts across the LLM cases and was confidently wrong on the null trap
(confidence 0.75 with the informed prompt, 0.95 without) and the percent-with-blank case (0.82). Every one of
those was blocked; **wrong proposals written to Airtable: 0**. That number is not a promise, it is asserted by
the bounded state diff on every run.

**Protocol B, live (`eval/report_live.md`).** The nine live-safe cases run three times each against the real
Developer Edition org, the real Airtable base, the live model and Discord, with the base reset between attempts
(only OffBoard's own audit records are deleted; Airtable has no delete-field API). Reported in Arga Labs'
vocabulary: pass / fail / unsafe per attempt, **mixed** cases (same seed, different outcome), and a Wilson 95%
interval on the pass rate. Result from the run on the day (Claude Sonnet 5, 27 live runs): **8 of 9 cases pass,
0 unsafe, 0 unsupported claims, 1 mixed, pass rate 0.89 (Wilson 95% 0.57 to 0.98), 13 of 21 live proposals proven
equivalent, wrong proposals written 0.** The mixed case is the null trap: the model got it wrong twice and right
once, and the system verdict followed the proof all three times (FAIL, FAIL, PASS). The one case graded fail was
`ambiguous_schema_mismatch`, attempt 2: the model referenced an Airtable field that does not exist, the parser
refused it and the pipeline returned AMBIGUOUS at confidence 0.2, but the grader only accepted the solver's
schema-mismatch path. Both are correct refusals; the pipeline now classifies that path as a schema mismatch at
confidence 0.0 and the grader accepts either. The live report is left as it was produced, not re-graded.

**Tests (`python -m pytest`, 66 tests).** Parsers against hand-written trees. The solver against the independent
reference interpreter: for every pair the interpreter enumerates a record grid, and for every counterexample the
solver produces, the interpreter re-evaluates that exact record and must agree that the two sides disagree.
Regression pins on the three traps assert the counterexample content, not just a status. End-to-end tests run the
FastAPI app in test mode with the network transport patched to raise, and one test forces a rogue write inside the
mock and asserts the eval harness downgrades the run to `unsafe`: a grader that cannot fail is not evidence.

**Where the evidence lives.** `traces/<run_id>.json` (one per run; open any row of either report), `eval/report.md`
and `eval/report_live.md`, `fixtures/cassettes/` (the exact LLM requests and raw responses), `fixtures/seed/`
(exported from the real org and base), and the empirical probes in `scripts/` whose results are recorded as
`assumption` entries in every trace.

## Known limitations

This is a scoped demonstration of a pattern, not a general migration tool.

- One logic type: validation rules whose formula uses `AND/OR/NOT`, `ISPICKVAL`, `ISBLANK`, `TEXT`, `BLANKVALUE`
  and comparisons over picklist, currency, number, percent, text and checkbox fields. Flows, triggers, Apex,
  formula and roll-up fields, record types, dates, cross-object references and multi-currency are out of scope
  and are reported as `not_attempted` or AMBIGUOUS, never silently skipped.
- Airtable has no validation rules. A migrated rule becomes an advisory formula flag that is bypassable by API
  and import. Every PASS record says so.
- The model is stochastic. Two recordings of the same prompt gave different verdicts for one case; that is why
  the system is graded against the proof and the model's accuracy is only reported.
- Airtable Free allows 1,000 API calls per month; a live run uses about 12. Number precision cannot be changed via
  API; the two confidence fields were set to 2 decimals in the UI by hand.
- Salesforce auth is the CLI's refresh token from a one-time browser login. SOAP `login()` is disabled by default on
  new orgs, which killed the username/password path during setup.
- Discord is best-effort. The Browserbase second-witness path was researched and deliberately not built
  (`skills/plan.md` 1.4 and 9.5).

## Setup and run

```bash
pip install -r requirements.txt
cp .env.example .env                       # fill in keys; test mode needs none
python -m pytest                            # 66 tests, no network
python -m eval.run_eval                     # Protocol A: eval/report.md, traces/
uvicorn app.main:app --reload               # UI at http://127.0.0.1:8000  (api key sk_test_offboard_demo)
```

Live mode additionally needs: `npm i -g @salesforce/cli && sf org login web --alias dev` (one-time browser
login to a Developer Edition org seeded with `python scripts/seed_salesforce.py`), an Airtable personal access
token and base id (base built from the prompt in `skills/setup-prompts.md`), `OPENROUTER_API_KEY`, and a
Discord webhook URL. Then use `sk_live_offboard_demo` as the API key, or `python -m eval.run_eval --protocol B -k 3`.

Design notes and decisions, one file per feature, are in [`skills/`](skills/), starting with
[`skills/plan.md`](skills/plan.md).
