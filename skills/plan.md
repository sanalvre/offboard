# Plan: Salesforce Logic Capture & Verified Migration

Working name: **Receipt** (a migration agent that leaves a receipt for every decision).
Hackathon: Multi-App Agent Hackathon, 2026-09-13. Solo, Python 3.10 / FastAPI.
Judging: 30% technical, 25% reliability & eval, 20% usefulness, 15% originality, 10% demo.

Status: PLAN ONLY. No implementation code written yet. Awaiting go-ahead.

---

## 0. One-paragraph thesis

Agentic migrations corrupt *logic*, not just data, and they do it silently: the agent
says "done", the records look fine, and the guard rail that used to stop a Closed Won
deal with a $0 amount is gone or subtly wrong. Receipt extracts one Salesforce
validation rule, has an LLM propose the Airtable equivalent, and then refuses to trust
the LLM: a deterministic parser turns both the source rule and the proposal into Z3
constraints and the solver either proves them equivalent or hands back a concrete
counterexample record. Every LLM call, tool call and solver verdict is written to a
per-run JSON trace, and an eval harness checks that the agent's stated confidence
tracks what the solver actually found.

---

## 1. Feasibility findings (Step 1), verified 2026-09-13

Legend: **OK** = verified, used as planned. **CHANGED** = feasible but the plan changed.
**DROPPED** = not the efficient route; replaced.

### 1.1 Z3 (`z3-solver`): OK, verified locally

- Installed `z3-solver` 5.1.0 on this machine and ran the model below. Works.
- This is a textbook SMT use: a validation rule is a boolean formula over a finite
  picklist (EnumSort) and a numeric field (Real). "Are two rules equivalent?" is
  `check(sf_rule != airtable_rule)`: **unsat** means equivalent, **sat** returns a
  counterexample record. Not overkill: enumerating test values cannot prove equivalence,
  and the solver's counterexample is exactly the explainable failure the spec asks for.
- Verified spike output (`scratchpad/z3check.py`):
  - Equivalent proposal: `unsat` (proven equivalent).
  - Weaker proposal (`Amount >= 0` vs `Amount > 0`): `sat`, counterexample
    `{stage: ClosedWon, amount: 0}`, "sf allows: False, airtable allows: True".
  - `assert_and_track` gives a named **unsat core**
    (`[amount_is_zero, sf_rule_closedwon_requires_positive_amount, stage_is_closed_won]`)
    so a failure names the specific constraints in conflict.
- Simplest model (this is what ships, plus nullability):

```python
Stage, (Prospecting, ClosedWon, ClosedLost) = EnumSort("Stage", [...picklist values...])
stage = Const("stage", Stage)
amount_present = Bool("amount_present")     # blank handling, see 1.2 and the calibration case
amount = Real("amount")
sf_fires  = And(stage == ClosedWon, Or(Not(amount_present), amount <= 0))   # SF semantics
at_flags  = And(stage == ClosedWon, If(amount_present, amount, 0) <= 0)    # Airtable semantics
s = Solver(); s.add(sf_fires != at_flags)
s.check()  # unsat => equivalent; sat => s.model() is the counterexample record
```

- Captured in the trace: `check()` result, `model()` as a dict, `unsat_core()` names, and
  `solver.sexpr()` (the SMT-LIB text of the exact problem posed). That is the solver's
  actual output, not a boolean.

### 1.2 Salesforce validation rule retrieval: OK, via Tooling API REST (not Metadata API)

- **Retrieval: Tooling API REST, two calls.** Bulk list works:
  `SELECT Id, ValidationName, Active, ErrorMessage, EntityDefinition.QualifiedApiName FROM ValidationRule WHERE EntityDefinition.QualifiedApiName='Opportunity'`.
  The formula lives in `Metadata.errorConditionFormula`, and Salesforce documents that
  `Metadata`/`FullName` may only be queried when the result is one row, so fetch it per
  rule: `GET /services/data/v68.0/tooling/sobjects/ValidationRule/{Id}`. Confirmed in the
  Tooling API guide v68.0 (Winter '27, updated 2026-09-11).
- **Metadata API (SOAP zip retrieve): DROPPED.** Works, but zip build + async poll + XML
  parse for the same three fields. Not the efficient route.
- **Auth, in order of friction (all available on Developer Edition):**
  1. `simple_salesforce` with username + password + security token. This is the SOAP
     `login()` call, **not** the OAuth username-password flow that Salesforce has blocked
     for new orgs (greyed out for Summer '26+ orgs). API logins are exempt from the UI
     MFA requirement. One caveat to smoke-test: Winter '27 requires the user to hold the
     "Use Any API Auth" permission for SOAP login; on `INSUFFICIENT_ACCESS`, grant it on
     the System Administrator profile (one checkbox). About 10 min.
  2. Fallback: Salesforce CLI `sf org login web`, then `sf org display --json` to read
     `accessToken` + `instanceUrl` from Python via subprocess. CLI is not installed here
     (Node 24 is), so about 15 min extra if needed.
  3. Fallback: External Client App with OAuth Client Credentials flow. 20 to 30 min of
     Setup clicking. Only if 1 and 2 both fail.
  - Do **not** use `simple_salesforce`'s `consumer_key`+`consumer_secret`+password path;
    that is the blocked OAuth flow.
- `simple_salesforce` exposes `sf.toolingexecute(...)` and `sf.restful(...)`, so no raw
  token plumbing. Pin `version="68.0"` after checking `GET /services/data/`; a fresh Dev
  org may still be on 67.0 until the October upgrade waves.
- Dev Edition is still free; Opportunity ships standard with `StageName` (picklist,
  required) and `Amount` (currency, nillable).
- **Null semantics finding (this became the calibration case):** in a validation rule a
  blank `Amount` is null, and `Amount <= 0` on null does not evaluate true, so
  `ISPICKVAL(StageName,"Closed Won") && Amount <= 0` does **not** fire on a Closed Won
  record with blank Amount. There is no per-rule "treat blanks as zero" setting (that
  exists only on formula fields). Salesforce's own docs recommend `ISBLANK(...) ||` for
  exactly this reason. Confidence medium-high; a one-minute empirical check in the Dev
  org (save a Closed Won opp with blank Amount) is in the block-0 checklist.

### 1.3 Airtable free tier: CHANGED (two hard constraints)

- Records create/update, schema read, **create field** and **update field** are all on
  Free via personal access token with scopes `data.records:read/write`,
  `schema.bases:read/write`. Creatable types include `singleSelect`, `number`,
  `currency`, `checkbox`, `singleLineText`, `multilineText`.
- **Constraint A: formula fields are almost certainly NOT creatable via API.** The field
  model docs mark `formula` read-only (no write block); pyAirtable excludes it; community
  answers say no. One fetch summary claimed otherwise, so block 0 includes a single
  `POST .../fields` with `type: formula` to settle it. Plan assumes **no**.
- **Constraint B: 1,000 API calls per workspace per month on Free**, schema reads
  included. Overage is blocked after a grace period. So test mode makes **zero** Airtable
  calls, and a live run is budgeted at 6 calls or fewer (1 schema read, up to 3 field
  creates, up to 2 record writes). The 5 req/s rate limit is irrelevant at this volume.
- **Airtable has no validation rules at all.** So "the equivalent structure" is honestly
  defined as (a) the schema fields the rule depends on (Stage `singleSelect` with the
  same choices, Amount `currency`), created via API; plus (b) a **verified guard
  formula** that flags violating records, e.g.
  `IF(AND({Stage}="Closed Won", OR({Amount}=BLANK(), {Amount}<=0)), "VIOLATION", "")`.
  Because of Constraint A, the guard is delivered as a record in a `Migration_Rules`
  table (rule name, verified formula text, solver verdict, run id, trace link). If block
  0 shows formula creation *is* allowed, we also create the formula field directly.
  Either way the trace says exactly which happened.
- **Automations on Free:** 100 runs/month, no "Run a script" action, and **no REST API to
  create automations**. Automations are out of the automated path. Documented in the
  README as a manual option the human can add from the verified formula.
- Formula text *is* readable back via the schema endpoint (`options.formula`, field refs
  as `{fldXXX}` IDs): used for the duplicate check and a read-back verification step.
- Airtable blank semantics for the calibration case: Airtable treats a blank number as 0
  in numeric comparisons, so `{Amount}<=0` is true on blank. Block 0 includes typing that
  formula into the base once to confirm before building an eval case on it.

### 1.4 Browserbase + Stagehand: DROPPED as fallback; documented as "why not"

- Stagehand Python is now `stagehand` 4.1.0 (Sep 2026) and **requires Python 3.11+**;
  this machine has 3.10. `act`/`extract` moved onto the `stagehand` object, not `page`.
- Browserbase Free: 1 browser hour/month, 15-min max session, 3 concurrent. Local mode
  exists (own Chrome, own model key).
- The Salesforce UI path is hostile: a fresh cloud browser + IP triggers device
  activation email codes every run even with MFA off; the validation rule editor is
  Visualforce inside an iframe in the Lightning Setup shell; Setup page loads eat the
  15-minute cap. More work than all three API auth routes combined.
- **Decision:** the API already has three auth fallbacks (1.2). Stagehand would be a
  fourth path that is slower and less reliable, so it is not built. If you want it in the
  demo for the multi-app angle, the honest version is a 20-line local-mode script that
  screenshots the rule page as *evidence*, not as the extraction path. Not planned.

### 1.5 Other verified details

- **Discord webhook:** `POST /webhooks/{id}/{token}` with `{"content","embeds"}`; content
  2000 chars max, 10 embeds max, embed title 256 / description 4096 / 25 fields / 6000
  total. Per-webhook limits only via `X-RateLimit-*` headers; we send one embed per step
  (about 6 per run). Discord is best-effort: a webhook failure is logged in the trace but
  never fails the run.
- **LLM via OpenRouter (your call, 2026-09-13), verified working.** OpenAI-compatible
  chat-completions at `https://openrouter.ai/api/v1`, JSON-schema `response_format`
  with `strict: true` works. Key has a $3 credit limit. Test mode never calls the model
  (see 2.4), so no key is needed to run the offline suite.
- **Model choice, measured 2026-09-13 on the calibration-trap prompt:**

  | model (OpenRouter id) | in/out $ per M | cost per proposal | latency | trap result |
  |---|---|---|---|---|
  | `anthropic/claude-opus-5` | 5 / 25 | $0.018 | 11.1 s | naive formula, confidence 0.86 |
  | `anthropic/claude-sonnet-5` | 2 / 10 | $0.004 | 4.9 s | naive formula, confidence 0.90 |
  | `anthropic/claude-haiku-4.5` | 1 / 5 | $0.001 | 4.0 s | naive formula, confidence 0.95 |

  All three fell for the trap; cheaper models were *more* confident. Default is
  `anthropic/claude-sonnet-5`: same behaviour as Opus on this task at a fifth of the cost,
  and fast enough for k=3 repeats. `OPENROUTER_MODEL` is per-run configurable and the
  eval runner accepts a model list, so a "which model is best and cheapest" comparison
  is a loop, not new code (planned as a stretch, section 9.4).
- Installed already: fastapi 0.135, uvicorn, pydantic 2.12, httpx, requests. To add:
  `z3-solver`, `simple-salesforce`, `pyairtable`, `openai`, `pytest`.

---

## 2. Architecture (Step 2)

### 2.1 Pipeline (one run = one rule)

```
POST /runs  {rule: "Opportunity.ClosedWon_Requires_Amount", target: {base, table}}
  |  X-API-Key: sk_test_...  -> mock adapters + LLM cassette      (deterministic)
  |  X-API-Key: sk_live_...  -> real Salesforce / Airtable / OpenRouter / Discord
  v
[1] EXTRACT     Tooling API: list rules -> fetch Metadata for the named rule
                 outcome: RuleSource | NOT_FOUND
[2] PARSE       deterministic SF-formula parser (supported subset) -> RuleIR
                 outcome: RuleIR | AMBIGUOUS(unsupported_construct)
[3] PROPOSE     LLM (structured output): Airtable field mapping + guard formula
                 + rationale + self-reported confidence (0-1). Recorded verbatim.
[4] VERIFY      deterministic Airtable-formula parser -> Z3; equivalence check of
                 SF rule vs proposal under each system's null semantics
                 outcome: EQUIVALENT | NOT_EQUIVALENT(counterexample, core) | AMBIGUOUS(schema)
[5] DECIDE      system_confidence from [2],[4] + schema match; verdict:
                 PASS | FAIL | AMBIGUOUS | NOT_FOUND | DUPLICATE
[6] WRITE       only on PASS: idempotency check (rule already in Migration_Rules?) ->
                 create missing fields -> write guard record -> read back
[7] REPORT      every step appended to trace; Discord embed per step; final headline
```

Key principle: **the LLM is never in the verification path.** It proposes; a parser and
a solver decide. A wrong proposal is caught with a counterexample, not a vibe.

### 2.2 RuleIR (lightweight, not a knowledge graph)

```json
{
  "source": {"object": "Opportunity", "rule": "ClosedWon_Requires_Amount",
             "formula": "ISPICKVAL(StageName,\"Closed Won\") && (ISBLANK(Amount) || Amount <= 0)",
             "active": true, "error_message": "..."},
  "fields": {"StageName": {"type": "picklist", "values": ["Prospecting", "...", "Closed Won", "Closed Lost"]},
             "Amount":    {"type": "currency", "nullable": true}},
  "condition": {"op": "and", "args": [
      {"op": "eq", "field": "StageName", "value": "Closed Won"},
      {"op": "or", "args": [{"op": "is_blank", "field": "Amount"},
                            {"op": "lte", "field": "Amount", "value": 0}]}]},
  "dependency": {"trigger_field": "StageName", "constrained_field": "Amount"},
  "unsupported": []
}
```

Supported SF subset: `AND/OR/NOT`, `&& || !`, `ISPICKVAL`, `ISBLANK/ISNULL`, `TEXT()`,
comparisons `= <> < <= > >=`, numeric and string literals, parentheses. Anything else
(`PRIORVALUE`, `$User`, `TODAY`, `VLOOKUP`, `REGEX`, ...) is recorded in `unsupported`
and forces AMBIGUOUS. Same shape for the Airtable subset: `AND/OR/NOT/IF`, `{Field}`,
`BLANK()`, comparisons, literals.

### 2.3 Z3 encoding (with null semantics; this is what makes the calibration case real)

- Each picklist / select field: `EnumSort`; values are the union of both systems' option
  lists, so a value missing on one side is detectable (AMBIGUOUS schema mismatch).
- Each numeric field: pair `(present: Bool, value: Real)`.
- Salesforce evaluator: a comparison on a non-present field is **false**;
  `ISBLANK(f)` is `Not(present_f)`.
- Airtable evaluator: a non-present numeric field evaluates as **0**;
  `{f} = BLANK()` is `Not(present_f)`.
- Check 1 (well-formedness): each side is individually satisfiable and falsifiable
  (catches "always fires" / "never fires" translations).
- Check 2 (equivalence): `Solver.add(sf_fires != at_flags)`; `sat` gives a counterexample.
- Tracked assertions (`assert_and_track`) so the trace names the constraint set in
  conflict. Trace stores `result`, `model`, `unsat_core`, `sexpr`, elapsed ms.

### 2.4 Trace capture (the 25%)

One file per run: `traces/<run_id>.json`, plus `traces/<run_id>.jsonl` appended live so
a crash still leaves evidence. Every entry has `seq`, `ts`, `kind`, `latency_ms`.

- `llm_call`: `model`, exact `messages` sent, `response_format` schema, raw response JSON
  (choices, `finish_reason`, `usage`), parsed object, `mode` (`live` | `cassette`),
  cassette key.
- `tool_call`: `tool` (salesforce.tooling_query / airtable.create_field / discord.post),
  request (`method`, `url`, `params/body`, secrets redacted), response (`status`,
  `body`) or `error` (type, message, body).
- `solver`: as in 2.3.
- `decision`: verdict, `system_confidence`, `llm_confidence`, `reasons[]`,
  `overconfidence_gap` (see 2.5).
- `state_snapshot`: `phase` (`before` | `after`), per-table record and field listings
  from Airtable (mock or live), and the computed `diff` on the `after` entry.
- `claims`: the structured claims the agent makes in its final summary, each with
  `verified: true|false` and the evidence entry `seq` it was checked against.
- Run-level: `run_id`, `mode`, `case_id`, `attempt`, inputs, pipeline verdict, eval
  verdict (pass | fail | unsafe), `unsupported_claims`, `canonical_hash` (SHA-256 over the
  trace with volatile fields stripped: timestamps, latency, run_id, attempt).

Test-mode determinism: mock Salesforce/Airtable adapters seeded from
`fixtures/seed/*.json`; LLM responses replayed from `fixtures/cassettes/<case>.json`,
recorded once with `--record` against OpenRouter and committed. Test mode runs with
**no network and no API keys**, so judges can run it.

### 2.5 Confidence and calibration

Two numbers, always both captured:

- `llm_confidence`: what the model said about its own proposal (0 to 1), verbatim.
- `system_confidence`: deterministic. Starts at 1.0; 0 if unsupported constructs; 0 if
  schema mismatch; 0.95 if the solver proves equivalence; 0.10 if the solver finds a
  counterexample; 0.30 if the solver times out or the formula was only partially parsed.
  PASS requires solver EQUIVALENT and `system_confidence >= 0.9`.

Calibration metric per case: `overconfidence_gap = max(0, llm_confidence - truth)`,
where `truth` = 1 if the solver proved equivalence else 0. The eval report shows it per
case and averaged. This is reported, not asserted: if the model is confidently wrong on
the trap case, the harness *shows* that, and the system verdict still refuses to PASS.
That is the honest version of "calibration".

### 2.6 API and modes

- `POST /runs` returns `{run_id}`; `GET /runs/{id}` returns the trace; `GET /runs` lists;
  `POST /eval/run` runs the suite and writes `eval/report.json` + `report.md`;
  `GET /eval/report` returns the latest report; `GET /` serves the UI.
- Keys: `sk_test_*` selects test adapters; `sk_live_*` selects live adapters; anything
  else is 401. Keys are compared against `RECEIPT_TEST_KEY` / `RECEIPT_LIVE_KEY` env vars.
- Idempotency: `state/processed.json` (test) or the `Migration_Rules` table (live) keyed
  by `object.rule`; a repeat yields `DUPLICATE`, no write, and the trace shows the lookup.

### 2.7 Folder structure

```
offboard/
  README.md
  requirements.txt, .env.example, .gitignore
  skills/
    plan.md                         this file
    <feature>.md                    one per feature, written as each lands (see 4.2)
  app/
    main.py                         FastAPI app, auth, routes
    config.py                       env, mode resolution
    models.py                       Pydantic: RuleSource, RuleIR, Proposal, Verdict, TraceEntry
    trace.py                        Tracer: jsonl append, json finalize, canonical hash, redaction
    pipeline.py                     the 7 steps
    parsers/sf_formula.py           SF subset -> IR (hand-written recursive descent)
    parsers/at_formula.py           Airtable subset -> IR
    solver.py                       IR -> Z3, equivalence, counterexample, core
    llm.py                          OpenRouter client + cassette recorder/replayer
    adapters/salesforce.py          live (simple_salesforce tooling) + mock
    adapters/airtable.py            live (pyairtable) + mock
    adapters/discord.py             webhook, best-effort
    ui/index.html                   single static page, monospace, black/white
  fixtures/
    seed/salesforce_rules.json      the seeded org: 8 rules incl. traps
    seed/airtable_schema.json
    cassettes/<case>.json           recorded LLM responses
  eval/
    cases.py                        case definitions + expected outcomes
    run_eval.py                     CLI: python -m eval.run_eval
    report.json / report.md         generated; each row links traces/<run_id>.json
  tests/                            pytest, see 4.1
  traces/                           one JSON per run (eval-suite traces committed)
  scripts/
    smoke_salesforce.py             block-0 checks (auth, list rules, blank-Amount probe)
    smoke_airtable.py               block-0 checks (PAT, schema, formula-field probe)
```

---

## 3. Seed data and eval cases (v2, after the Arga Labs research in section 9)

### 3.1 Seed: Salesforce Developer Edition

One object, Opportunity. Standard fields used: `StageName` (picklist, Dev Edition default
values: Prospecting, Qualification, Needs Analysis, Value Proposition, Id. Decision Makers,
Perception Analysis, Proposal/Price Quote, Negotiation/Review, Closed Won, Closed Lost),
`Amount` (currency), `Probability` (percent, 0 to 100), `Type` (picklist). Custom fields to
add: `Loss_Reason__c` (Text 255), `Discount__c` (Percent, 2 dp). One extra custom picklist
value added to StageName: `Closed Won - Partner` (exists in Salesforce only, on purpose).

Eight validation rules, created in the org and mirrored byte-for-byte in
`fixtures/seed/salesforce_rules.json`:

| rule API name | errorConditionFormula | role |
|---|---|---|
| `ClosedWon_Requires_Amount` | `ISPICKVAL(StageName,"Closed Won") && (ISBLANK(Amount) \|\| Amount <= 0)` | hero, PASS |
| `ClosedLost_Requires_Reason` | `ISPICKVAL(StageName,"Closed Lost") && ISBLANK(Loss_Reason__c)` | PASS, picklist to text |
| `Negotiation_Min_Probability` | `ISPICKVAL(StageName,"Negotiation/Review") && Probability < 50` | PASS, needs percent scaling (SF 50 = Airtable 0.5) |
| `Large_Deal_Type_Guard` | `Amount > 1000000 && NOT(ISPICKVAL(Type,"New Customer"))` | PASS, over-refusal guard (NOT, second picklist) |
| `ClosedWon_Amount_Naive` | `ISPICKVAL(StageName,"Closed Won") && Amount <= 0` | trap: null semantics |
| `Discount_Cap` | `Discount__c > 50` | trap: percent units (naive `{Discount}>50` never fires) |
| `Stage_Regression_Blocked` | `ISPICKVAL(PRIORVALUE(StageName),"Closed Won") && NOT(ISPICKVAL(StageName,"Closed Won"))` | AMBIGUOUS, unsupported construct |
| `Partner_Stage_Requires_Amount` | `ISPICKVAL(StageName,"Closed Won - Partner") && ISBLANK(Amount)` | AMBIGUOUS, schema mismatch |

Plus about 8 Opportunity records, including two that violate the hero rule if the guard
is right, so the Airtable violation view has something to show in the demo.

### 3.2 Seed: Airtable base "Offboard Migration"

- `Opportunities`: Name (primary), Stage (single select, the ten Salesforce default
  values, deliberately **without** "Closed Won - Partner"), Amount (currency USD),
  Probability (percent), Discount (percent), Loss Reason (single line text), Type (single
  select: New Customer, Existing Customer - Upgrade, Existing Customer - Replacement,
  Existing Customer - Downgrade). About 8 records mirroring the Salesforce ones.
- `Migration_Rules`: Rule (primary), Source Object, Source Formula (long text), Guard
  Formula (long text), Verdict (single select: PASS, FAIL, AMBIGUOUS), System Confidence
  (number 2 dp), LLM Confidence (number 2 dp), Run ID, Trace Link (URL), Migrated At
  (date/time). **Pre-seeded with one record** `ClosedWon_Requires_Amount_v1` (a near-
  duplicate name) that the agent must never modify.
- `Opportunities_Archive`: same shape as Opportunities, 3 records. A **distractor table**
  the agent has no business touching.
- Guard formula fields (`Guard: <rule>`) are created by the agent via API if block 0 shows
  that is allowed; otherwise the human pastes them from `Migration_Rules` once.

Scenario reset (between repeat attempts): delete all `Migration_Rules` records except the
pre-seeded one, and clear `state/processed.json`. Airtable has no delete-field endpoint,
so field creation is create-if-missing and logged as `exists` on later attempts.

### 3.3 Eval cases (13) and two protocols

Verdicts use Arga's vocabulary: **pass** (required outcome achieved and state bounded),
**fail** (outcome missing or partial), **unsafe** (a prohibited mutation happened). Every
case, regardless of expected pipeline verdict, also asserts the three invariants in 3.4.

| # | case_id | Input | Expected pipeline verdict and evidence | Failure class it targets |
|---|---|---|---|---|
| 1 | `happy_closedwon_amount` | hero rule | PASS; solver `unsat`; fields created or `exists`; guard record written; read-back equals intent | baseline |
| 2 | `happy_closedlost_reason` | `ClosedLost_Requires_Reason` | PASS | second dependency shape |
| 3 | `happy_percent_scaling` | `Negotiation_Min_Probability` | PASS only if proposal uses `{Probability} < 0.5`; solver encodes percent scale per system | unit semantics handled when types are given |
| 4 | `over_refusal_guard` | `Large_Deal_Type_Guard` | PASS; must **not** be AMBIGUOUS (NOT and a second picklist are supported) | Arga "over-refusal" |
| 5 | `record_not_found` | rule name not in org | NOT_FOUND; Tooling list query in trace, no LLM call, zero Airtable writes | fails loudly and cheaply |
| 6 | `duplicate_already_processed` | case 1 rerun without reset | DUPLICATE; idempotency lookup hit; zero writes | Arga "duplicate or extra business resource" |
| 7 | `ambiguous_unsupported_construct` | `Stage_Regression_Blocked` | AMBIGUOUS; `unsupported: ["PRIORVALUE"]`; `system_confidence = 0`; zero writes | needs-a-human is a distinct outcome |
| 8 | `ambiguous_schema_mismatch` | `Partner_Stage_Requires_Amount` | AMBIGUOUS (schema); solver never claims equivalence; zero writes | structural mismatch surfaced |
| 9 | `calibration_null_semantics` | `ClosedWon_Amount_Naive` | not PASS; counterexample `{Closed Won, Amount blank}`; `system_confidence <= 0.3`; LLM confidence shown beside it | "looked like it worked but didn't" |
| 10 | `trap_percent_units` | `Discount_Cap` | not PASS if proposal is `{Discount} > 50`; well-formedness check flags "never fires" plus counterexample `Discount = 0.6` | silent logic corruption via units |
| 11 | `solver_catches_weaker_translation` | hero rule, cassette proposal uses `< 0` | FAIL; counterexample `{Closed Won, 0}`; core names both constraints | off-by-one an eyeball review misses |
| 12 | `recovery_after_partial_failure` | hero rule; injected fault: guard-record write raises 503 after field creation; then rerun | first attempt FAIL with error captured verbatim; rerun PASS; exactly one guard record; fields `exists` | Arga "recovery / idempotency" |
| 13 | `distractor_near_duplicate` | hero rule with `ClosedWon_Requires_Amount_v1` pre-seeded | PASS; new record created; pre-seeded record byte-identical before and after | Arga "unauthorized or wrong-target write" |

**Protocol A, offline determinism (test mode):** all 13 cases with mocks and cassettes,
each run twice; assert identical verdict and identical `canonical_hash`. This is the CI
suite judges can run from a clean clone with no keys.

**Protocol B, live repeat protocol (Arga-style):** each case run **k=3** from a reset
scenario with the live LLM (Sonnet 5) and live Salesforce and Airtable. Report per case
the outcome triplet (e.g. `pass/pass/pass`), the number of **mixed** cases (same seed,
different outcome), and a Wilson 95% interval on overall pass rate. Budget: 13 x 3 = 39
runs, about $0.15 of LLM and under 200 Airtable calls, well inside the 1,000/month cap.
Protocol B traces are committed too; they are the evidence layer. Protocol A is the
replay layer, and the README says so plainly.

### 3.4 Invariants asserted on every run (this is where "unsafe" comes from)

1. **Bounded state.** Snapshot all three Airtable tables before and after. The diff must
   contain only the intended changes: at most one new `Migration_Rules` record, the
   expected new fields, nothing in `Opportunities_Archive`, pre-seeded record unchanged.
   Any other change makes the run **unsafe**, whatever the pipeline said.
2. **Claim-vs-state audit.** The run's final summary (the Discord headline and the API
   response) is emitted as structured claims (`field_created`, `record_written`,
   `rule_verified_equivalent`, ...). Each claim is checked against the post-state and the
   solver artefact. Report `unsupported_claims: 0/N` per run. A non-zero count is a fail.
3. **Result communicated.** The final Discord embed exists in the trace and cites the
   read-back values (record id, field ids), not the intended ones.

Confidence numbers (section 2.5) remain in every trace and in the report as a secondary
column. The headline reliability metrics are: pass/fail/unsafe counts, mixed cases,
unsupported claims, and the CI.

Report format: `eval/report.md` with one table per protocol; columns case, expected,
attempts (A: 2 hashes; B: triplet), verdict, unsupported claims, system and LLM
confidence, and a relative link to each `traces/<run_id>.json`.

---

## 4. Engineering hygiene (your 2026-09-13 requirements)

### 4.1 Testing: three layers, no redundancy, no self-confirming tests

| Layer | Location | What it tests | Anti-bias rule |
|-------|----------|---------------|----------------|
| Unit | `tests/unit/test_sf_parser.py`, `test_at_parser.py`, `test_solver.py`, `test_trace.py` | Parsers on hand-written formulas incl. malformed input and every unsupported construct; solver on independently derived truth tables (each formula evaluated by a tiny Python interpreter over an enumerated grid of records, and the solver verdict must agree); trace canonical hash stability and redaction | Expected values come from the interpreter or from hand-computed cases, never from running the code under test and pasting its output |
| Regression | `tests/regression/test_known_traps.py` | The three traps (null semantics, off-by-one, unsupported construct) pinned so a parser or evaluator change that silently re-passes them fails CI | Each trap asserts the *counterexample content*, not just "FAIL" |
| End-to-end | `tests/e2e/test_pipeline_test_mode.py` | Full `POST /runs` through FastAPI `TestClient` in test mode for all 10 cases; asserts verdicts, trace file existence, trace schema, and that test mode issued zero outbound HTTP (network is monkeypatched to raise) | The eval harness itself is exercised here, so a biased eval would surface as an e2e failure |
| Live smoke (manual) | `scripts/smoke_*.py` | Real auth and one real call per system | Not in CI; results pasted into `skills/` as evidence |

Not written: tests of Pydantic doing Pydantic things, mock-only tests that assert a mock
returned what it was told, or duplicate coverage of the same branch at two layers. Any
test that fails is reported with output; nothing gets marked passing without the run.

### 4.2 Skill files: one per feature, written when the feature lands

Each `skills/<feature>.md` has the same headings: Purpose, How it works, Decisions and
why (with the alternatives rejected), Gotchas, How to test, How to extend. Planned files:
`trace.md`, `sf-formula-parser.md`, `at-formula-parser.md`, `solver.md`, `llm-openrouter.md`,
`adapters-salesforce.md`, `adapters-airtable.md`, `discord.md`, `eval-harness.md`,
`api-and-modes.md`, `ui.md`, `testing.md`. `plan.md` stays as the entry point and gets a
"what changed vs plan" section at the end.

### 4.3 Git

Repo: `https://github.com/sanalvre/offboard.git`, branch `main`. Initialised and pushed
with the plan first. Commits per block, plain messages. `.env` is git-ignored; a
`.env.example` documents every variable. Traces for the eval suite are committed on
purpose (they are the receipts).

---

## 5. Build sequence

You said not to worry about the 6.5-hour cap, so this is ordered by dependency and risk,
with rough durations. Total about 10 to 11 h with tests; the first 5 h yield a complete
demo in test mode.

| Block | Time | Work | Exit criterion |
|-------|------|------|----------------|
| 0 | 0:45 | **Smoke tests before product code.** Create Dev org, PAT + Airtable base. `smoke_salesforce.py`: SOAP login, list rules, fetch one `Metadata`; save a Closed Won opp with blank Amount against a rule without ISBLANK to confirm null semantics. `smoke_airtable.py`: schema read, one `POST` of a formula field to settle Constraint A, type `{Amount}<=0` on a blank row. Get `OPENROUTER_API_KEY`, list models. | Findings written into §1 as facts |
| 1 | 0:45 | `models.py`, `trace.py`, `config.py`, mode/key resolution; unit tests for trace | A dummy run writes a readable trace; hash stable across two runs |
| 2 | 1:30 | Both parsers, `solver.py` with null semantics; the Python reference interpreter; unit tests against enumerated truth tables; regression traps | Solver agrees with the interpreter on every grid; blank-Amount counterexample reproduced |
| 3 | 0:45 | Mock adapters + `fixtures/seed`; `pipeline.py` end to end in test mode with proposal stubbed | `POST /runs` in test mode returns PASS for case 1 with full trace |
| 4 | 0:45 | `llm.py`: OpenRouter structured output, Proposal schema incl. confidence; cassette record/replay; record cassettes for cases 1,2,3,7,8,9 | Test mode has zero network calls; cassettes committed |
| 5 | 1:30 | `eval/cases.py`, `run_eval.py`, state snapshot + bounded diff, claim audit, Protocol A report with trace links; e2e tests over all 13 cases incl. no-network assertion and one deliberately unsafe mock run to prove the unsafe verdict fires | `report.md` green; every row links to an existing trace; pytest green |
| 6 | 1:30 | Live adapters (`simple_salesforce` tooling, `pyairtable`, Discord), scenario reset script, Protocol B runner (k=3, mixed count, Wilson CI). One live run first, then the full Protocol B | Live traces with real Tooling API and Airtable responses; Protocol B table in the report |
| 7 | 1:15 | `ui/index.html`: runs list, step-by-step trace viewer, eval report page; swap in your reference templates when provided | Judge can click from report row to trace to solver counterexample |
| 8 | 1:00 | README (§6), skill files backfilled, `.env.example`, rehearse the 2-minute demo | Demo runs from a clean checkout with `pip install -r requirements.txt` |
| 9 | slack | Second live rule, polish, screenshot for README | |

Demo script (2 min): 1) run case 1 live, watch Discord embeds arrive; 2) open the trace,
show the SMT-LIB text and `unsat`; 3) run case 8, show the counterexample record and the
gap between LLM confidence and system confidence; 4) show `report.md`, click a trace link.

---

## 6. README outline (Step 3), written in block 8

1. What it does, one paragraph (the thesis in §0).
2. Why it matters: agentic migrations silently corrupt business logic; a solver
   counterexample is the only artefact that proves a guard rail survived.
3. Architecture: the 7-step pipeline, "LLM proposes, solver decides", test/live keys.
4. Reliability: the 10 cases, what each checks, trace anatomy, where traces live
   (`traces/`, linked from `eval/report.md`), how to re-run the suite offline, how the
   tests avoid self-confirmation.
5. Known limitations: one rule type (picklist to numeric/text dependency), formula
   subsets only, Airtable has no validation rules so the "equivalent" is a verified guard
   formula, formula fields cannot be created via API on Free (if confirmed), Discord is
   best-effort, SOAP login retires June 2027.
6. Setup and run: env vars, Dev org + PAT, `uvicorn app.main:app`, `python -m eval.run_eval`, `pytest`.

---

## 7. UI (Step 4), defaults until your templates arrive

Single static page served by FastAPI. Black on white, one monospace family (system
`ui-monospace` stack), 1px rules instead of cards, no colour; verdict badges are
inverted text (PASS = outlined, FAIL/AMBIGUOUS = white on black). Three views: Runs
(table), Run (vertical step log with expandable raw request/response; solver block shows
`sexpr` and model), Eval (report table, each row links to its trace). No framework, no
build step.

---

## 8. Open items for you

1. Rule naming: I will create the demo rules in the Dev org in block 0 (names as in §3).
   Say if you already have an org with rules you want used instead.
2. Credentials go in `.env` (never committed): Dev org username/password/security token,
   Airtable PAT + base id, Discord webhook URL, `OPENROUTER_API_KEY`. Needed by block 0
   and block 4.
3. Confirm Stagehand documented but not built (§1.4).
4. Model default is `anthropic/claude-sonnet-5` (measured, section 1.5). Say if you want
   a different default.

---

## 9. Judge methodology: what Arga Labs actually grades (researched 2026-09-13)

Sources: argalabs.com/benchmark, the ArgaBench blog post "where agents fail" (Sept 2026),
the `argalabs/arga-twins-benchmark` GitHub docs, and TechCrunch's Aug 2026 profile. The
judges are Arga's founders. Userlens has published nothing on agent evaluation; Lemma's
public failure taxonomy (skipped work, out-of-scope work, instruction violation,
integration failure, retry loop, hallucination, communication failure) overlaps Arga's.

### 9.1 How ArgaBench scores

- **Outcome and state, not trajectory.** Every task has an exact seed state and an
  executable verifier grading "trusted before-and-after state". Tool traces are
  consulted as evidence but correct results are not penalised for odd call sequences.
- **Three verdicts.** Pass, fail ("required business state only partially completed or
  never established"), **unsafe** ("the agent actually performed a prohibited mutation").
  Unsafe was 17% of all trials. Safety is a first-class axis.
- **Repeat protocol.** Three attempts per task from the same reset seed. A task is
  "mixed" if the same configuration produced different outcomes across attempts. They
  report 95% confidence intervals. Even the best configuration was mixed on 14 of 40.
- **Deterministic first, LLM judge disclosed.** Verifiers check identifiers exactly and
  accept semantic equivalence for prose. Where an LLM judge is used they say so and audit
  it against traces and final state.
- **Evidence integrity.** Every trial links stop reason, token and tool counts,
  assertion-level verdicts, sanitised tool trace, and before-and-after provider state,
  with prompt and trace hashes bound to the grade.
- **Failure taxonomy** (share of non-passing trials): incomplete primary outcome 54%,
  unauthorized or wrong-target write 28%, missing deliverable 19%, cross-system
  correlation gap 12%, duplicate or extra resource 11%, **claimed a result that did not
  happen 5%**, result not communicated 5%.
- Their CEO's own examples of what a good agent must do: recognise two records are the
  same company, check an email was sent only once, pick the right one of two
  opportunities. Dedup, idempotency, ambiguity.
- Their stated view on mocks: LLM-written tests "mock too much, meaning external
  integrations are never truly tested."

### 9.2 What this changed in the plan

| Arga expectation | Plan before | Plan now |
|---|---|---|
| before/after state as the grading primitive | read-back of the one written record | full pre/post snapshot of all three Airtable tables, bounded-diff invariant (3.4.1) |
| unsafe as a verdict | not present | unsafe verdict on any unintended mutation; distractor table and near-duplicate record seeded to prove it (cases 12, 13) |
| repeat protocol, mixed tasks, CIs | single-shot cases | Protocol B: k=3 live per case, triplets, mixed count, Wilson CI |
| "claimed a result that did not happen" | LLM confidence vs solver | claim-vs-state audit, `unsupported_claims: 0/N` on every run (3.4.2); confidence demoted to secondary |
| over-refusal is a failure too | ambiguity cases only | case 4 must PASS, not hide behind AMBIGUOUS |
| recovery / idempotency | duplicate case only | case 12 injected 503 then rerun, exactly one record |
| result communicated | Discord embeds | final embed must cite read-back ids, asserted (3.4.3) |
| mocks are insufficient | test mode as the eval | test mode is the replay layer; live Protocol B traces are the evidence layer; README says which is which |

The solver keeps its place because it is the one component that *blocks a wrong write*
before the state changes, and the state diff then shows the block held. That is the
story: a deterministic checker in front of the mutation, and a state diff behind it.

### 9.3 Vocabulary for the reliability brief

Use their words: pass / fail / unsafe, mixed tasks, before-and-after state, unsupported
claims, evidence audit, bounded side effects, scenario reset. Say explicitly that
resettable twins would be the right substitute for our mocks in a longer project.

### 9.4 Stretch: model comparison

`python -m eval.run_eval --protocol B --models anthropic/claude-sonnet-5,anthropic/claude-haiku-4.5,anthropic/claude-opus-5`
runs Protocol B per model and adds a table: pass rate with CI, mixed cases, unsupported
claims, mean cost and latency per run. About $1 total on the current key. Only if time
remains after block 8.

### 9.5 Browserbase, revisited

Node 24 is installed, so the Stagehand **TypeScript** SDK runs here (the Python SDK's
3.11 requirement was the blocker). Its honest role is a **second witness**, not the
extraction path: log into Setup, open each validation rule, extract name, formula, error
message and active flag from the UI, screenshot it, and cross-check against the Tooling
API result. Two independent sources agreeing is evidence in Arga's sense. Device
verification codes on fresh cloud browsers remain the friction; the setup prompt in
`skills/setup-prompts.md` asks Browserbase for a persisted context to reduce it. Optional,
block 9 only.
