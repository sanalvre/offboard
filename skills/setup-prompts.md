# Setup prompts for third-party builders

These are the project descriptions to paste into Airtable's AI base builder and
Browserbase's project setup. They describe exactly the seed in `plan.md` section 3, so
what the builders produce matches the fixtures byte-for-byte. After generating, compare
field names and select options against `fixtures/seed/airtable_schema.json`; the agent
matches on names.

## Airtable (AI base builder prompt)

I need a base called "Offboard Migration" that acts as the destination system for a
Salesforce-to-Airtable migration demo, and as the audit log for an AI agent that
migrates Salesforce validation rules into Airtable guard formulas. It needs three tables
with these exact names and fields.

Table 1, "Opportunities". This mirrors the Salesforce Opportunity object. Fields:
"Name" (primary field, single line text); "Stage" (single select with exactly these ten
options in this order: Prospecting, Qualification, Needs Analysis, Value Proposition,
Id. Decision Makers, Perception Analysis, Proposal/Price Quote, Negotiation/Review,
Closed Won, Closed Lost); "Amount" (currency, USD, 2 decimal places); "Probability"
(percent, 0 decimal places); "Discount" (percent, 2 decimal places); "Loss Reason"
(single line text); "Type" (single select with exactly these options: New Customer,
Existing Customer - Upgrade, Existing Customer - Replacement, Existing Customer -
Downgrade). Seed it with eight records: a mix of stages, with at least two records in
"Closed Won" that have a blank or zero Amount, one "Closed Lost" record with a blank Loss
Reason, one "Negotiation/Review" record with Probability 30, and one record with Amount
1,500,000 and Type "Existing Customer - Upgrade". Add a view called "All" (grid, no
filter). Do not add any formula, lookup, rollup or automation to this table; an agent
will add guard formula fields later through the API.

Table 2, "Migration_Rules". This is the audit log the agent writes to. Fields: "Rule"
(primary field, single line text); "Source Object" (single line text); "Source Formula"
(long text); "Guard Formula" (long text); "Verdict" (single select: PASS, FAIL,
AMBIGUOUS); "System Confidence" (number, 2 decimal places); "LLM Confidence" (number, 2
decimal places); "Run ID" (single line text); "Trace Link" (URL); "Migrated At" (date
with time, include time, 24 hour). Seed it with exactly one record: Rule
"ClosedWon_Requires_Amount_v1", Source Object "Opportunity", Source Formula
"ISPICKVAL(StageName,\"Closed Won\") && Amount <= 0", Guard Formula "(legacy, do not
modify)", Verdict "AMBIGUOUS", Run ID "seed-0001", Migrated At today. This record is a
deliberate distractor that the agent must never edit.

Table 3, "Opportunities_Archive". Same fields as "Opportunities" with three records in
"Closed Lost". This table is a distractor and must never be modified by the agent.

Do not create any automations, interfaces or forms. Keep the base on the Free plan. I
will connect to it with a personal access token that has data.records:read,
data.records:write, schema.bases:read and schema.bases:write scopes on this base only.

## Browserbase (project description prompt)

Project: "Receipt, Salesforce evidence witness".

I am building a migration verification agent in Python and TypeScript that moves a
Salesforce validation rule into Airtable and proves the logic survived. The primary
extraction path is the Salesforce Tooling API. I want to use Browserbase and Stagehand
(TypeScript SDK, Node 24) as an independent second witness: a browser session that reads
the same validation rule from the Salesforce Setup UI so I can cross-check it against
the API result and attach a screenshot to the run's evidence trace.

The automation, run once per rule:

1. Open a persisted Browserbase context so the Salesforce device is remembered between
   runs, then navigate to the org's My Domain login URL and sign in with credentials
   passed at runtime (never stored in the script). If Salesforce asks for an email
   verification code, pause and surface that to the caller instead of guessing.
2. Navigate to Setup > Object Manager > Opportunity > Validation Rules
   (URL pattern lightning/setup/ObjectManager/Opportunity/ValidationRules/view). The
   rule list and the rule detail page render inside an iframe within the Lightning
   Setup shell.
3. For a rule name given as input, open its detail page and extract, as structured JSON:
   rule name, active (true/false), error condition formula (the full text in the formula
   box), error message, error location. Take a full-page screenshot.
4. Return the JSON and the screenshot path. Do not click Edit, Save, Delete, Activate or
   Deactivate on anything; this session is read-only.

Constraints: one session per run, under 5 minutes; Free plan (1 browser hour per month,
15-minute session cap) is enough for the demo; no proxies needed. The output JSON is
compared field by field with the Tooling API response, and any mismatch is recorded in
the trace as a finding rather than resolved silently.

## Claude design agent (HTML artifact for the product UI)

Design a single-page HTML artifact for **Receipt**, an evidence-log tool for AI-driven CRM migrations.

**What the product does, in one paragraph.** Receipt migrates one piece of business logic (a Salesforce
validation rule) into Airtable and proves the meaning survived. An LLM proposes the Airtable formula; a parser and
the Z3 solver check it against the original for every possible record; if they are equivalent the guard is
written, if not the write is blocked and a concrete counterexample record explains why, and if the rule cannot
be encoded it is flagged for a human. Every run leaves a receipt: the exact LLM prompt and response, every API
call, the solver's SMT-LIB problem and verdict, before-and-after state of the destination, and a list of claims
the agent made that were checked against reality. The audience is engineers and RevOps leads who have been burned
by migrations that "looked complete" and judges evaluating agent reliability.

**Views the page needs (tabs or sections, static sample data is fine):**
1. **Runs**: a table of runs with started time, mode (test/live), rule name, verdict badge, system confidence,
   LLM confidence, unsupported claims count, short hash. A toolbar with a rule picker, an "informed / naive
   prompt" toggle and a Run button.
2. **Run detail**: a KPI strip (verdict, system confidence 0.95, LLM confidence 0.75, overconfidence gap 0.75,
   unsupported claims 0/3, unexpected state changes 0, canonical hash), then a vertical step log of about 25
   entries in order: assumption (5 of them, each with a "verified" mark and an evidence line), tool_call
   (salesforce.rest, airtable.get_schema, discord.post), rule_ir, llm_call (model, cassette or live, the guard
   formula in code, confidence), solver (status, z3 sat/unsat, a boxed counterexample like
   `{StageName: "Closed Won", Amount: blank}` with "source does not fire, target flags"), decision,
   state_snapshot before/after with a diff line, claims with check marks. Each step expands to its raw JSON; the
   solver step also expands to SMT-LIB text; the LLM step expands to the exact prompt.
3. **Eval**: KPI strip (17/17 pass, 0 fail, 0 unsafe, 17/17 reproducible, Wilson 95% 0.82–1.0, unsupported
   claims 0, LLM proposals proven 12/28, wrong proposals written 0) and a case table with columns case, targets,
   expected, attempts (verdict badges), eval verdict, sys, llm, gap, trace links.
4. **Inventory**: coverage counts (proven / blocked / ambiguous / not attempted) and a table of the 8 rules with
   business intent and status.

**Verdict vocabulary and badges:** PASS, FAIL, AMBIGUOUS, NOT_FOUND, DUPLICATE, ERROR for the pipeline;
pass / fail / unsafe for the eval. PASS outlined, FAIL/AMBIGUOUS/ERROR/unsafe inverted (white on black),
NOT_FOUND/DUPLICATE dashed outline.

**Design direction.** Black on white, high contrast, minimal but considered. Monospace-leaning typography
(ui-monospace / SF Mono / Menlo stack) because this is an evidence log, not a dashboard. 1px rules and hairlines
instead of cards and shadows; no colour except black, white and two greys; generous whitespace; tabular numbers;
uppercase letter-spaced labels for metadata; the counterexample record and the guard formula rendered as code.
The page should feel like a well-set technical document or a lab notebook: intuitive to scan, nothing decorative.
Dark mode optional; if included, invert cleanly.

**Constraints.** One self-contained HTML file, vanilla CSS, no frameworks, no external assets, responsive down to
a laptop width, and the structure should be easy to wire to JSON endpoints (`/runs`, `/runs/{id}`,
`/eval/report`, `/inventory`) later: use data attributes or simple render functions rather than hard-coded
markup where practical. Keep the header small: the word RECEIPT, four nav items, and an API-key input on the right.
