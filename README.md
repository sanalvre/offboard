# OffBoard

**Salesforce logic capture and Z3-verified migration to Airtable, with a receipt for every decision.**

**Demo video (2 min):** https://www.loom.com/share/b4adb53c35de442c896678b5116fc026

Built solo for the Multi-App Agent Hackathon, 13 September 2026. Python 3.10 / FastAPI. Apps in the stack:
**Salesforce** (Tooling API and Metadata API, the source), **Airtable** (Web API, the destination), **Discord**
(webhooks, the notification channel), and **OpenRouter** (Claude Sonnet 5, the model that proposes translations).

## The problem

Everyone knows companies hate their legacy systems, and Salesforce is the example here. So why do they stay if moving
data is easy nowadays? Usually because they have years of custom logic their organization runs on: validation rules,
formula fields, flows, assignment rules. Having to map out every rule and workflow, rebuild it in the new system, and
then verify it actually behaves the same is tedious and expensive. CRMs like HubSpot offer migration services off
Salesforce, but they take months and cost five to six figures, and the verification at the end is record counts, a
spot check, and a couple of weeks running both systems side by side. Nobody checks the logic formally, so when a
rebuilt rule fires on different records than the original, nobody notices until the numbers stop matching.

## What OffBoard does

OffBoard uses the Salesforce Metadata API to capture your Salesforce configuration. Each piece is fed to an LLM, which
proposes an equivalent in the destination. In this demo the destination is Airtable. After the LLM writes the Airtable
version of a rule, it goes through the Z3 solver, which checks the proposal against the original for every possible
record, not a sample, and either proves the two are equivalent or hands back the exact record that breaks it. If the
two are not identical, nothing gets pushed into Airtable. If the logic cannot be expressed in Airtable at all, it is
flagged for a human instead of guessed.

One of the reasons this is hard for agents is that you are not only migrating data, you are re-applying logic to a
system whose rules do not line up with the source. Here is the example from today's run. We migrated a Salesforce rule
that says "a closed won deal cannot have an amount of zero or less." The LLM wrote a translation of it for Airtable,
and the solver's job was to answer whether the translation behaves exactly like the original on every possible record.
It looked right. But Airtable treats a blank amount as zero and Salesforce treats it as null, so the translation behaves
differently on one kind of record: a won deal with no amount filled in. Salesforce lets that deal through; the
translation would have flagged it. The solver found that record, blocked the write, and handed the record back. On
another attempt the LLM added one condition, "and the amount isn't blank," the solver proved that version equivalent,
and it was written. Same original rule, two translations, and the solver told them apart in a way you could not by
reading them.

The semantics the solver relies on were not taken from documentation. Blank-is-null was confirmed by saving records in
the real org; blank-is-zero by running the formula in the real base; and every trace begins by listing those assumptions
with their evidence and whether each was verified empirically.

## The results

Across the offline test run, 22 out of 22 cases passed with zero unsafe writes. Live against the real org and base, the
raw LLM correctly mapped the logic 18 out of 30 times. Eight of its proposals were wrong, and four could not be expressed
in Airtable at all and were flagged for human evaluation. Because Z3 checks every proposal against both systems' actual
rules, zero wrong proposals reached Airtable, and that zero is not the agent's word for it: every run diffs the base
before and after and any unintended change is graded **unsafe**.

Every run writes a full trace: the exact prompt, the raw model response, every API call and what came back, the solver's
proof in SMT-LIB, the state of the base before and after, and every claim the agent made about what it did, checked
against the state. OffBoard then fingerprints the whole file with a SHA-256 hash, stripping only timestamps and run ids.
When I ran all 22 cases a second time, every hash matched. That means the runs were not just passing twice; they were
identical down to the counterexample records and the confidence numbers, and anyone who clones this repo and reruns the
suite will surface the same results.

In this demo I used Discord for notifications; most teams would use Slack. Every step of a run posts a short embed as it
happens: what was extracted, what the model proposed and how confident it was, what the solver decided, and a final result
line. Each post carries the run id, so the notification is one click from the full trace. The Discord post is the
headline; the trace file is the receipt.

## The panel

`uvicorn app.main:app` serves a single page at http://127.0.0.1:8000 with four tabs.

**Runs.** Every run, with its verdict and two confidence numbers. **System confidence** is the solver's verdict as a
number: 0.95 when Z3 proved equivalence, 0.10 when it found a counterexample, 0 when the rule could not be encoded at
all. **LLM confidence** is what the model said about its own proposal. The gap between them on the blocked run (the model
said 0.9, the system said 0.1) is the calibration failure this tool exists to catch. Clicking a run shows the trace step
by step: each entry expands to its raw JSON, the solver step to the SMT-LIB problem as posed, the LLM step to the exact
prompt. Verdicts: PASS (proven and written), FAIL (a record disagrees, nothing written), AMBIGUOUS (could not be verified,
flagged for a human, audit record only), PARTIAL (condition proven, action has no API-writable equivalent), NOT_FOUND,
DUPLICATE (already migrated, nothing written).

**Inventory.** Pulls every piece of logic on the object from the Metadata API and groups it by type: validation rules,
formula fields, flows, workflow rules, assignment rules, Apex, approval processes. For each one it says whether OffBoard
can verify it, with a reason, and what happened the last time it ran. On the demo org: 15 artefacts across 5 types, 12
verifiable, 3 not (a Salesforce default transaction-security flow, a retired workflow field update, and assignment rules
that are lowered but not yet migrated). Nothing is silently skipped.

**Mapping.** The schema check that runs before any rule. For every Salesforce field a verifiable artefact touches, it
proposes the Airtable counterpart by name and asks Z3 whether any legal Salesforce value has no home in the Airtable
field: a picklist value with no matching option, a blank going into a required field, a number with more decimals than
the target holds. Where units differ (percent as 50 versus 0.5) it reports lossless with the transform. On the demo org
it found one real precision loss nobody would spot by eye (a Discount of 0.01 percent has two decimals in Salesforce's
units but needs four as a fraction, and Airtable's field has two) and one field with no home at all (Region). No model is
involved in this tab.

**Eval.** The reliability report, read straight from `eval/report.json`. Headline numbers, then one row per case with a
plain-English description of what it checks, the verdict from each run, the eval result in pass / fail / unsafe, both
confidences, and a link to the trace of each attempt.

## Three things the solver checks

| SMT use | question Z3 answers | artefacts |
|---|---|---|
| constraint validation | do the source rule and the target guard fire on exactly the same records? | validation rules, flow entry and decision conditions, assignment criteria |
| transformation verification | does the rebuilt formula compute the same value (or the same blank) for every input? | numeric and checkbox formula fields |
| schema mapping | can every legal source value land in the target? | the Mapping tab, and per-run pre-checks before any solving |

Reconfiguration goes through the API wherever the API exists: guard formula fields for validation rules, real formula
fields for formula fields (followed by a **behavioural probe**: a record with known inputs is created, Airtable's own
computed value is read back and compared with the reference interpreter's evaluation of the Salesforce formula, and the
probe is deleted; live, Airtable computed 900 and the Salesforce formula gives 900), and a computed field for a flow whose
only action assigns a same-record checkbox. Flows with other actions stop at PARTIAL: Airtable automations have no API.

## Architecture

```
POST /runs {rule}   X-API-Key sk_test_* -> mocks + recorded LLM cassettes (deterministic, offline)
                    X-API-Key sk_live_* -> Salesforce CLI transport, OpenRouter, Airtable REST, Discord
  1 extract   Tooling API for validation rules; Metadata API XML for formula fields, flows, everything else
  2 parse     Salesforce formula subset -> IR with typed field metadata (scale, nullability, options)
  3 target    Airtable schema -> field specs; before-snapshot; idempotency lookup in Migration_Rules
  4 propose   LLM (structured JSON): field mapping, target formula, business intent, self-reported confidence
  5 verify    Airtable formula parser -> IR; Z3: unsat = equivalent, sat = counterexample record
  6 decide    PASS (0.95) | FAIL (0.10) | AMBIGUOUS (0.0-0.3) | PARTIAL | NOT_FOUND | DUPLICATE
  7 write     PASS only: create the target field; formula fields also get the behavioural probe; always one audit record
  8 report    after-snapshot, bounded diff, claims checked against state, Discord headline, trace finalised with hash
```

The LLM is never in the verification path. It proposes; a parser and a solver decide. Code map: `app/pipeline.py` and
`app/artefacts.py` (the steps), `app/inventory.py` (Metadata API capture), `app/parsers/` (both formula subsets),
`app/solver.py` (Z3), `app/reference.py` (an independent plain-Python interpreter the solver is tested against),
`app/mapping.py` (schema check), `app/adapters/` (live and mock Salesforce, Airtable, Discord), `app/llm.py` (OpenRouter
with cassette record/replay), `app/trace.py` (the receipt), `app/main.py` (API and UI), `eval/` (harness), `skills/`
(design docs per feature).

## How reliability was tested

Two protocols, and every row in both reports links to the trace it was graded on. Nothing here is a summary you have to
take on faith.

**Protocol A, offline (`eval/report.md`).** 22 cases, each run twice from a reset mock with recorded LLM responses and no
network. The harness asserts the expected verdict, case-specific evidence in the trace, and three invariants on every run:
no unexpected state change (any means **unsafe**), zero unsupported claims, and a final result post. The two runs must
produce identical hashes. Result: **22 pass, 0 fail, 0 unsafe, 22/22 reproducible**.

| case | what it checks |
|---|---|
| `happy_closedwon_amount` | a clean rule that should migrate; proves the happy path end to end |
| `happy_closedlost_reason`, `happy_percent_scaling`, `over_refusal_guard` | more rule shapes; the last must PASS rather than hide behind "needs a human" |
| `record_not_found` | a rule that does not exist: no model call, nothing written |
| `duplicate_already_processed` | the same rule twice: the second run notices and writes nothing |
| `ambiguous_unsupported_construct` | a construct that cannot be translated (PRIORVALUE): flagged for a human, not guessed |
| `ambiguous_schema_mismatch` | the rule uses a field Airtable does not have: flagged before anything is written |
| `calibration_null_semantics` (+ `_naive_prompt`) | the blank-amount trap, with and without the prompt explaining blank handling; the verdict must match the proof either way |
| `trap_percent_units` (+ `_naive_prompt`) | the 50 vs 0.5 trap, same two prompts |
| `solver_catches_naive_null_translation`, `solver_catches_percent_units`, `solver_catches_weaker_translation` | hand-edited wrong translations (labelled inside the cassette file) that guarantee a wrong proposal reaches the solver; each must be blocked with the right record |
| `recovery_after_partial_failure` | Airtable fails mid-write; the rerun must finish with exactly one record, no duplicates |
| `distractor_near_duplicate` | 15 unrelated records and a second table must be untouched, byte for byte |
| `formula_net_amount`, `formula_is_big_deal` | formula fields proven as transformations, written, confirmed by a probe record |
| `formula_literal_copy_off_by_100` | keeping Salesforce's `/ 100`, forced; blocked with both computed outputs shown |
| `flow_flag_stale_negotiation` | a record-triggered flow's conditions proven; the model omitted the blank guard and was blocked |
| `flow_not_verifiable_inventoried` | a Salesforce default flow OffBoard cannot verify: listed with a reason, no model call |

LLM-dependent cases are graded on whether the **system** followed the proof (PASS only when Z3 says `unsat`), and the
model's accuracy is reported rather than asserted. A wrong proposal blocked by the solver is a passing case for the system.

**Protocol B, live (`eval/report_live.md` and `eval/report_live_phase2.md`).** 13 live-safe cases, three attempts each
against the real Developer Edition org, the real Airtable base, the live model and Discord, with the base reset between
attempts. Reported in Arga Labs' vocabulary: pass / fail / unsafe per attempt, **mixed** cases (same seed, different
outcome), and a Wilson 95% interval. Results: 8 of 9 and 4 of 4 cases pass, 0 unsafe in both, 39 runs, 30 model proposals,
18 proven, 8 blocked with a counterexample, 4 flagged as inexpressible, 0 wrong proposals written. Two cases were mixed
because the model's answer changed between attempts; the system's verdict followed the proof each time. The one case
graded fail was a correct refusal the grader had been too narrow about; the report was left as produced and the grader
widened afterwards.

**Tests (`python -m pytest`, 100 tests).** Parsers against hand-written trees. The solver against an independent
reference interpreter: for every pair the interpreter enumerates a record grid, and for every counterexample the solver
produces, the interpreter re-evaluates that exact record and must agree that the two sides disagree. Regression pins on
the traps assert the counterexample content. Solver determinism across repeated checks. End-to-end tests run the FastAPI
app in test mode with the network transport patched to raise, and one test forces a rogue write inside the mock and
asserts the harness downgrades the run to `unsafe`: a grader that cannot fail is not evidence.

**Where the evidence lives.** `traces/<run_id>.json` (one per run, linked from every report row), `eval/report.md`,
`eval/report_live.md`, `eval/report_live_phase2.md`, `fixtures/cassettes/` (the exact LLM requests and raw responses),
`fixtures/seed/` (exported from the real org and base), and the probe scripts in `scripts/` whose results appear as
`assumption` entries in every trace.

## Known limitations

This is a scoped demonstration of a pattern, not a general migration tool.

- Verified logic types: validation rules, record-triggered flow conditions with a single same-record assignment,
  assignment-rule criteria (lowered, not yet migrated), and numeric or checkbox formula fields over `+ - * /`, `IF`,
  `BLANKVALUE`, `MIN`, `MAX`. Apex, screen and scheduled flows, flows with side-effect actions, text and date formulas,
  roll-ups, record types, cross-object references and multi-currency are inventoried with a reason, never silently skipped.
- Each artefact is verified on its own. The composed system (one automation's write changing what another matches) is not
  verified; that is the stated next step.
- Airtable has no validation rules. A migrated rule becomes an advisory formula flag, bypassable by API and import. Every
  PASS record says so. Airtable automations have no API, so flows with side effects stop at PARTIAL with a recorded spec.
- The model is stochastic. The same prompt gave different verdicts across attempts; that is why the system is graded
  against the proof and the model's accuracy is only reported. The obvious fix, feeding the counterexample back to the
  model for another attempt, is not built yet.
- Airtable Free allows 1,000 API calls per month; a live run uses about 12 to 15.
- Salesforce auth is the CLI's refresh token from a one-time browser login; SOAP login is disabled by default on new orgs.
- Discord is best-effort. A Browserbase path was researched and deliberately not built (`skills/plan.md` 1.4, 9.5).

## Setup and run

```bash
pip install -r requirements.txt
cp .env.example .env                       # fill in keys; test mode needs none
python -m pytest                            # 100 tests, no network
python -m eval.run_eval                     # Protocol A: eval/report.md, traces/
python -m uvicorn app.main:app              # panel at http://127.0.0.1:8000  (api key sk_test_offboard_demo)
```

Live mode additionally needs: `npm i -g @salesforce/cli` and `sf org login web --alias dev` (one-time browser login to a
Developer Edition org seeded with `python scripts/seed_salesforce.py`), an Airtable personal access token and base id
(base built from the prompt in `skills/setup-prompts.md`), `OPENROUTER_API_KEY`, and a Discord webhook URL. Then use
`sk_live_offboard_demo` as the API key in the panel, or `python -m eval.run_eval --protocol B -k 3`. To reset the live
base between demos: `python -m eval.reset_live --apply` (deletes only OffBoard's own audit records).

Design notes and decisions, one file per feature, are in [`skills/`](skills/), starting with
[`skills/plan.md`](skills/plan.md), [`skills/plan-phase2.md`](skills/plan-phase2.md) and
[`skills/problem-domain.md`](skills/problem-domain.md).
