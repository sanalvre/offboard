# OffBoard

**Salesforce logic capture and Z3-verified migration to Airtable, with a receipt for every decision.**

**Demo video (2 min):** https://www.loom.com/share/b4adb53c35de442c896678b5116fc026

Multi-App Agent Hackathon, 13 September 2026. Solo build, Python 3.10 / FastAPI. Apps touched: Salesforce
(Tooling API and Metadata API), Airtable (Web API), OpenRouter (Claude Sonnet 5), Discord (webhooks).

## What the demo shows

1. **Inventory**: the whole configuration layer pulled from the Metadata API. 15 pieces of logic across 5 types, 12
   verifiable, 3 not, each with a reason.
2. **A proven migration**: the closed-won rule, the exact prompt the model saw, the solver's `unsat`, and the claims
   checked against Airtable after the write.
3. **A blocked migration**: the same rule without its blank guard. The model said 0.9; the solver found the record
   (Closed Won, Amount blank) and nothing was written. That gap between what the model believed and what could be
   proven is the failure this tool exists to catch.
4. **Eval**: 22 of 22 offline cases pass, 0 unsafe, and every trace **hashes identical** on a second run. The hash is a
   SHA-256 fingerprint of the whole trace with only timestamps and run ids stripped, so a match means the two runs were
   indistinguishable down to the counterexample record and the confidence numbers, not just "passed twice". Live: the
   model mapped the logic correctly 18 of 30 times; 8 wrong proposals were blocked with their counterexample; 4 could
   not be expressed in Airtable and were flagged for a human; 0 wrong proposals reached Airtable, checked by a
   before-and-after diff of the base on every run.
5. **Mapping**: the schema check that runs before any rule. It found a real precision loss (Discount at two decimals in
   percent units needs four as a fraction) and a field with no home (Region).
6. **Discord**: one embed per step as it happens, each carrying the run id, so the notification is one click from the
   receipt.

## What it does

Everyone knows companies hate their legacy systems, and Salesforce is the example here. So why do they stay if moving
data is easy? Because they have years of custom logic their organization runs on: validation rules, formula fields,
flows, assignment rules. Mapping every one of those, rebuilding it in the new system, and then checking it actually
behaves the same is tedious, expensive, and the failures are silent. A rule that says "a closed won deal can't have an
amount of zero or less" gets rebuilt so it fires on different records, and nobody notices until the numbers stop
matching.

OffBoard uses the Salesforce Metadata API to capture that configuration. It feeds each piece to an LLM, which proposes
the Airtable equivalent, and then it refuses to trust the LLM. The Z3 solver checks the proposal against the original for
every possible record, not a sample, and either proves the two are equivalent or hands back the exact record that breaks
it. If they're not identical, nothing gets written. If the logic can't be expressed in Airtable at all, it's flagged for a
human instead of guessed. And every run leaves a receipt: the exact prompt, the raw model response, every API call, the
solver's proof, the state of the base before and after, and every claim the agent made, checked against reality.

## Why this matters

The two systems don't even agree on what a blank means. In a Salesforce validation rule a blank Amount is null, so
`Amount <= 0` never fires on it. Airtable treats a blank as zero, so the word-for-word translation flags every won deal
with no amount. Percent is stored as 50 in Salesforce and 0.5 in Airtable. In today's live runs the model wrote the
obvious translation of the closed-won rule at 0.9 confidence, and the solver blocked it with that one record: Closed Won,
Amount blank. On another attempt the model added "and the amount isn't blank," the solver proved it equivalent, and that
version was written.

Nobody in the migration business checks logic this way. Consultancies describe Salesforce-to-HubSpot projects where
"moving records is the cheap part" and "every workflow is rebuilt by hand," and their verification is record counts, a
5% spot check, and a couple of weeks of running both systems side by side. OffBoard is a scoped demonstration of the
missing piece: proving, not sampling, that a piece of logic means the same thing after migration, and refusing to write
when it doesn't. The research behind that framing is in [`skills/problem-domain.md`](skills/problem-domain.md).

## Three things the solver checks

| SMT use | question Z3 answers | artefacts |
|---|---|---|
| constraint validation | do the source rule and the target guard fire on exactly the same records? | validation rules, flow entry and decision conditions, assignment criteria |
| transformation verification | does the rebuilt formula compute the same value (or the same blank) for every input? | numeric and checkbox formula fields |
| schema mapping | can every legal source value land in the target? | `GET /mapping` and the Mapping tab: per field, Z3 looks for a legal source value with no target representation (a picklist value with no option, a blank into a required field, a value with too many decimals); scale differences are lossless with a transform. On the demo org it found that a Discount of 0.01 percent (two decimals in Salesforce's percent units) has no representation in Airtable's two-decimal percent field, and that Region has no target at all |

And one honest status for everything else. `GET /inventory` is the coverage receipt: on the demo org it lists 15
logic artefacts across 5 types, 12 verifiable, and gives a reason for each of the 3 that are not (a Salesforce
default transaction-security flow, a retired Workflow field update, and a formula returning text would all land here).

Reconfiguration goes through the API wherever the API exists: guard formula fields for validation rules, real
formula fields for formula fields (then a **behavioural probe**: a record with known inputs is created, Airtable's own
computed value is read back and compared with the reference interpreter's evaluation of the Salesforce formula, and
the probe is deleted), and a computed field for a flow whose only action assigns a same-record checkbox. Flows with
other actions stop at `PARTIAL`: the condition is proven, the action spec is recorded, and Airtable automations have no
API to write to. Details and the reasoning behind each choice: [`skills/plan-phase2.md`](skills/plan-phase2.md) and
[`skills/phase2-configuration.md`](skills/phase2-configuration.md).

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

Two protocols, and every row in both reports links to the trace it was graded on. Nothing here is a summary you
have to take on faith.

**Protocol A, offline (`eval/report.md`).** 22 cases, each run twice from a reset mock with recorded LLM
cassettes and no network. The harness asserts the expected verdict, case-specific evidence in the trace, and
three invariants on every run: no unexpected state change (any means **unsafe**), zero unsupported claims,
and a final result post. The two runs must produce identical canonical hashes (content-only; timestamps,
latencies and run ids normalised). Result: **22 pass, 0 fail, 0 unsafe, 22/22 reproducible**.

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
| `formula_net_amount`, `formula_is_big_deal` | formula fields proven as transformations, written as real formula fields, confirmed by a behavioural probe |
| `formula_literal_copy_off_by_100` | hand-edited cassette keeping Salesforce's `/ 100`; blocked with both computed outputs on the counterexample |
| `flow_flag_stale_negotiation` | record-triggered flow: entry filters AND decision outcome verified; the model omitted the blank guard on Probability and was blocked |
| `flow_not_verifiable_inventoried` | Salesforce's default transaction-security flow: inventoried with a reason, no model call |

LLM-dependent cases are graded as **SOLVER_TRUTH**: the system verdict must follow the proof (PASS iff Z3 says
`unsat`). The model's accuracy is *reported*, not asserted: in the current cassettes Claude Sonnet 5 produced a
proposal proven equivalent in 12 of 28 attempts across the LLM cases and was confidently wrong on the null trap
(confidence 0.75 with the informed prompt, 0.95 without) and the percent-with-blank case (0.82). Every one of
those was blocked; **wrong proposals written to Airtable: 0**. That number is not a promise, it is asserted by
the bounded state diff on every run.

**Protocol B, live (`eval/report_live.md`, phase 2 additions in `eval/report_live_phase2.md`).** The nine live-safe cases run three times each against the real
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

Phase 2 live run (`eval/report_live_phase2.md`, 12 runs): **4 of 4 cases pass, 0 unsafe, 0 unsupported claims, 1 mixed.** The net amount formula passed all three attempts, each with a real probe record whose value Airtable computed itself (900 for Amount 1000 at 10 percent discount) matching the Salesforce formula; the flow translation was blocked all three times on the blank-probability counterexample at model confidence 0.8 to 0.9; the checkbox formula went PASS, PASS, AMBIGUOUS because the model's third proposal used a construct the parser does not accept, and the system refused rather than guessed. Live proposals proven equivalent: 5 of 9; wrong proposals written: 0.

**Tests (`python -m pytest`, 100 tests).** Parsers against hand-written trees. The solver against the independent
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

- Verified logic types: validation rules, record-triggered flow conditions with a single same-record assignment,
  assignment-rule criteria (lowered, not yet migrated), and numeric or checkbox formula fields over `+ - * /`, `IF`,
  `BLANKVALUE`, `MIN`, `MAX`. Apex, screen and scheduled flows, flows with side-effect actions (PARTIAL), text and
  date formulas, roll-ups, record types, cross-object references and multi-currency are inventoried with a reason,
  never silently skipped.
- Each artefact is verified on its own. The composed system (one automation's write changing what another
  automation matches) is not verified; that is the stated next step.
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
