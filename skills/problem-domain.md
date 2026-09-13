# Problem domain: why CRM migrations silently corrupt business logic

Researched 2026-09-13 from about 60 sources: Huble, Aptitude 8, Pedowitz Group, Vantage
Point, Xappex, Cloudsheer, RevOps Global, Campaign Creators, ClonePartner, Import2 help
center, HubSpot Knowledge Base and Community, Salesforce Help and Salesforce Ben,
Gearset, Elements.cloud, Clientell, Equals11, Airtable Community. Reddit and several
HubSpot Community thread bodies were unreachable; nothing here relies on them.

This file exists so every future agent on this project understands the real problem
Receipt is a scoped answer to. Read it before touching the parsers, the solver, or the
eval cases.

---

## 1. The shape of the problem

A Salesforce-to-HubSpot migration is an 8 to 16 week project costing $15k to $150k in
partner fees, plus 200 to 300 internal hours. Huble's summary: "Moving records is the
cheap part. Cleaning them is where the hours go." And: "Salesforce automation does not
transfer. Every workflow is rebuilt by hand."

Three layers move, and each fails differently:

| Layer | What moves | How it fails |
|---|---|---|
| Data | records, fields, associations, files | counts match but meaning drifts: scale, dates, enums, ownership, merges |
| Schema | objects, fields, picklists, record types, pipelines | target has no equivalent (Leads, record types, junctions, formula fields) |
| Logic | validation rules, flows, triggers, formulas, assignment, approvals | rebuilt by hand from an incomplete inventory into a system with weaker semantics; verified by parallel running and spot checks |

Industry numbers, all from consultancies rather than measured studies: 55% CRM
implementation failure rate; 83% of data migrations overrun or miss objectives;
automation rebuild time underestimated by 200 to 300%; "80 to 85% of business logic can
be replicated"; mid-market orgs carry 200 to 400 custom fields and migrate 60 to 80.

The line that defines our thesis, from Pedowitz: "A lazy field mapping produces a
HubSpot instance that looks complete and reports nothing useful." Everything below is a
way for a migration to look complete and be wrong.

---

## 2. Inventory: you cannot verify logic you never found

A single Opportunity save in Salesforce can run before-save flows, before triggers,
system validation, custom validation rules, duplicate rules, after triggers, assignment
rules, auto-response rules, workflow rules (with field updates that re-run validation),
escalation rules, after-save flows, roll-up recalculation, and criteria-based sharing.
Add the non-transactional layer: formula fields, approval processes, matching rules,
field-level security, record types with per-type picklists, dependent picklists, email
alerts, scheduled jobs, Apex.

Real orgs (Clientell audit medians): 418 unused custom fields, 1,844 inactive flows, 67%
of flows with no documented owner, 8 objects with multiple active triggers. Equals11:
"Three Flows are all set to Lead Status under different conditions. They run in
unspecified order."

Discovery tooling is weak. Setup's "Where is this used?" caps at 2,000 results and cannot
follow a flow that calls a flow. The Tooling API dependency object returns only direct
dependencies, excludes reports, and has been beta for years. Workflow Rules are not
queryable through the Tooling API at all. Consultancy checklists themselves truncate the
inventory: Pedowitz's says "Export every active workflow rule, process builder, and
Flow" and omits validation rules, formulas, roll-ups, assignment and approval processes.

**Commonly missed:** deprecated-but-active Workflow Rules and Process Builders, email
alerts and time-dependent actions, Apex written by people who left, validation rules on
rare record types, assignment and escalation rules, roll-up summaries (they look like
fields), scheduled jobs, approval processes.

**Consequence for Receipt:** a migration tool must produce a coverage receipt, not just
per-rule verdicts. "8 rules found, 4 migrated and proven, 2 blocked with counterexamples,
2 need a human" is the honest output. Silence about what was not attempted is the
failure mode.

---

## 3. Semantic gaps: the target cannot say what the source said

### 3.1 Validation

- **HubSpot** property validation covers only text, date, phone and number fields: min
  and max, uniqueness, character classes, regex on paid tiers, date ranges. Enumerations
  are excluded. There are no cross-field rules, no conditional rules, no formula
  expressions, no reference to associated records. Its own docs: "Not enforced:
  workflows, chatflows, submissions to meeting scheduling pages, or submissions to forms
  created via the legacy editor." Pipeline rules and required-by-stage properties "can be
  bypassed by Super Admins, records created or edited by workflows, records created or
  edited by API." RevOps Global: complex validation is "usually simplified into
  workflow-based validation so users are guided instead of blocked."
- **Airtable** has none. Community: "there is no real field validation mechanism in
  Airtable." The workaround is a formula flag field. Automations run asynchronously and
  flag rather than block.
- So every Salesforce validation rule with two fields becomes advisory in either target.
  A migration that reports "validation rule migrated" without saying "as an advisory
  flag, bypassable by API and import" has changed the meaning and hidden it.

### 3.2 Automation

- HubSpot workflow enrollment is once-only unless re-enrollment is configured per
  trigger, and several operators are prohibited for re-enrollment. "Every time X
  changes" degrades to "first time X changes." There is no PRIORVALUE or ISCHANGED
  equivalent with access to the old value.
- Delays freeze the timezone at entry and do not re-check criteria. Salesforce
  time-dependent actions re-evaluate at fire time.
- Calculation properties: about 40 functions, number-only equations within one object,
  one non-number property, no cross-object references, nulls propagate to empty unless
  guarded with `is_known`. Formula fields are snapshotted as static values that then go
  stale.
- Record types, per-type picklists and page layouts collapse into UI-only conditional
  logic. Filtered roll-ups need helper properties fed by workflows.

### 3.3 Null, type and value semantics that flip meaning silently

| Trap | Source | Target | Detection today |
|---|---|---|---|
| Blank number | validation rule: `Amount <= 0` on blank is not true (null); formula fields have a per-field "treat blanks as zeroes" setting | Airtable: blank is 0 in comparisons. HubSpot calc: any null input makes the result empty | only by testing records with blanks |
| Percent scale | stored and exported as 50 for 50% | HubSpot and Airtable store 0.5 | "any value above 1 in a percentage property is a red flag" |
| Picklist label vs API name | ISPICKVAL compares the API name; comparisons are case-sensitive | HubSpot has label and internal value; import matches either; copying enum to text copies the label | import error "does not match an existing option"; workflow filters on wrong value fail silently |
| Unknown enum values | inactive picklist values exist | HubSpot has no inactive values; unknown values import as **blank** while the record succeeds | null-rate diff per property |
| Dates | Date/Time stored GMT, displayed in user zone; TODAY() in user zone | HubSpot date picker is midnight UTC; a datetime interpretation shifts a day and can ping-pong syncs | spot-check known dates for minus one day |
| Currency | multi-currency with corporate conversion | native sync copies "only the number value", deals inherit portal default currency; 100k USD becomes 100k EUR | sums by currency |
| Boolean | strict true/false, but Boolean formulas can be null | HubSpot checkbox on/off yet "unknown" in filters, a de facto third state | not detected |
| Text length | Text 255, Long Text 131k; **report exports truncate long text to 255** | text 65k | max length per field pre and post |
| Rounding | 18 digits | decimal-place validation and `round_nearest` | sample-based |

### 3.4 Ordering and side effects

Salesforce: workflow field updates re-run system validation and triggers but "custom
validation rules, flows, duplicate rules, processes, and escalation rules aren't run
again." A record can legally end a save in a state that would fail validation.
Recreating N flows as N workflows in another system with no ordering guarantee changes
which one wins. Automations fire during import in both systems: Data Loader "fires
workflow rules and processes" and "it is currently not possible to disable this
behavior"; HubSpot has "no built-in feature which prevents enrollments due to imports."
Every consultancy says: pause all automation, migrate, validate, then unpause and
"monitor for the first 48 hours."

---

## 4. Data and mapping failures (the ones that look like success)

- **Leads and Contacts collapse.** HubSpot has no Lead object. Same email as Lead and
  Contact becomes one record "and if you didn't plan for it, you don't find out until the
  counts don't match." Converted leads must be excluded or double-counted.
- **Dedup merges the wrong survivor.** HubSpot dedups contacts on email and companies on
  domain, deals not at all. "The surviving field values may come from the wrong record,
  with teams discovering this weeks later when a contact's deal history looks wrong."
- **Junctions evaporate.** Opportunity Contact Roles become association labels or
  nothing: "it quietly evaporates."
- **Attribution zeroes.** Campaign membership does not map. "Six weeks later the CMO runs
  a pipeline attribution report and marketing appears to have contributed nothing to
  revenue in the past 18 months."
- **History collapses.** Create date, stage-entered dates and created-by are set to
  import time. "Every migrated deal will show a single stage entry on the date of
  import." Sales-cycle reports go to zero.
- **Owners vanish.** Unmatched owners import as unowned, which silently breaks routing.
- **Encoding and format drift.** "Café" becomes "CafÃ©"; "KY" and "Kentucky"; a Salesforce
  workflow regenerating country codes after a merge.
- **Orphans from load order.** "Activities migrate before deals exist to associate them
  to. The result is orphaned records that look complete in the object view and are
  broken in every report."
- **Hidden ID dependencies.** "23 separate applications referencing specific Salesforce
  IDs that would break during the transition."

---

## 5. How parity is verified today

Record counts per object. Null-rate and distinct-value diffs. A 5% manual spot check, or
100 records per object. VLOOKUP by legacy ID in Excel. "Reopen ten old cases." Power
users checking 20 to 30 key accounts. Parallel running for two to four weeks, sometimes
60 days. Hypercare for 30 days. Rollback thresholds of 1% corruption or 5% count
discrepancy.

For logic specifically: one smoke test ("create a test contact, run through a workflow"),
UAT tiers with no reconciliation method, and "unpause all automation on go-live day,
monitor for the first 48 hours."

**No source describes formal comparison of source and target logic. No truth tables, no
generated test cases, no model checking, no tool that compares a Salesforce rule to a
HubSpot workflow semantically.** Data reconciliation tools compare data. Elements.cloud,
Gearset and Salto analyse the Salesforce side only. Huble on the stakes: "the numbers
don't match the old dashboard is the fastest way to lose executive trust."

What slips through anyway: counts match but associations don't; silent blanks from enum
mismatches; percent and currency scale errors that look plausible; minus-one-day dates;
wrong merge survivors; attribution zeroing found at quarter end; workflows emailing
customers during import.

---

## 6. What this means for Receipt

Receipt is a scoped demonstration of one missing capability: **proving, not sampling,
that a piece of business logic means the same thing after migration, and refusing to
write when it does not.** The catalogue above tells us exactly which traps a one-rule
demo must handle to be credible, and which to name as out of scope.

### 6.1 Handled in the current scope (and which item above each answers)

| Design element | Answers |
|---|---|
| Type-aware IR: every field carries type, nullability, scale (percent 100 vs 1), option list, and whether comparison is on label or API name | 3.3 blank, percent, label vs API name |
| Two evaluators in the solver, one per system's null semantics | 3.3 blank number |
| Enum union check: a value on one side missing on the other is AMBIGUOUS, never a silent blank | 3.3 unknown enum values |
| Well-formedness check: each side individually satisfiable and falsifiable, so a "never fires" translation (percent scale) is caught even before equivalence | 3.3 percent scale |
| Equivalence check with counterexample record and named unsat core | 5 (no formal comparison exists) |
| "Migrated as advisory guard, bypassable by API and import" stated in every PASS record and in the README | 3.1 |
| Unsupported constructs (PRIORVALUE, cross-object, dates) force AMBIGUOUS with the construct named | 3.2, 2 |
| Coverage receipt: `GET /inventory` lists every validation rule on the object with its status: proven, blocked, ambiguous, not attempted | 2 |
| Pre/post state snapshots, bounded diff, unsafe verdict, distractor table and near-duplicate record | 4 wrong-target writes and duplicates |
| Idempotency by rule name; recovery case after injected failure | 4 orphans and reruns |
| Claim-versus-state audit on the final summary | 5 "looks complete" |
| Explicit `assumption` trace entries: each semantic assumption (blank handling, percent scale, case sensitivity) recorded with its evidence source and whether it was verified empirically in the live org | 3.3 detection column, which is otherwise "not detected" |

### 6.2 Named as out of scope in the README

Flows, triggers, Apex, assignment and approval processes; formula and roll-up fields;
record types and dependent picklists; date and timezone semantics; multi-currency;
data migration itself (dedup, associations, history, ownership); anything touching
HubSpot (the target here is Airtable). Each of these is a further instance of the same
pattern: an IR, a pair of evaluators, and a solver check in front of the write.

### 6.3 Design rules derived from this research

1. Never translate a rule without its field metadata. The formula text alone does not
   determine meaning (blank handling, scale, label vs API name).
2. Treat "cannot encode" as a first-class result with a name attached, never as a pass.
3. Verify semantics empirically where the docs are ambiguous, and record that you did.
   Block 0 checks blank Amount in the live org and blank comparison in Airtable.
4. Say what the target cannot enforce. A guard formula is not a validation rule.
5. Report coverage, not just verdicts.
6. Snapshot state before and after every write, and grade the diff.
7. Pause nothing silently: the demo never runs automations during writes, and the trace
   says so.
