# Plan, phase 2: configuration capture and verified reconfiguration

Written 2026-09-13 after phase 1 shipped (validation rules only). Phase 1 plan: `skills/plan.md`.

## 0. Thesis, sharpened

Mapping fields and scrubbing duplicates is commodity work. The hard, unsolved part of a migration is the
**configuration layer**: validation rules, flows, formula fields, assignment logic. Phase 2 makes OffBoard capture
that layer completely, re-express as much of it as the target can hold, and prove with an SMT solver that the meaning
survived, in three senses:

| SMT use | question the solver answers | phase 1 | phase 2 |
|---|---|---|---|
| constraint validation | do the source rule and the target guard fire on exactly the same records? | validation rules | plus record-triggered Flow entry conditions and decision outcomes, assignment rule criteria |
| transformation verification | does the rebuilt formula compute the same value for every input? | no | formula fields (arithmetic, IF/CASE over picklists, scale) |
| schema mapping | can every legal source value land in the target without loss? | option-set and unmapped-field checks | full mapping constraints: type, scale, nullability vs required, option sets, precision, text length, with counterexample values |

And one honest status for everything else: **inventoried, not verifiable**, with the reason.

## 1. Capture: the Metadata API, not a browser

`sf project retrieve start --metadata Flow,Workflow,ApexTrigger,ApexClass,CustomObject:Opportunity,AssignmentRules:Lead,ApprovalProcess --target-org dev`
returns every artefact as XML in one authenticated call we already have. What it gives, per type:

| type | what we get | phase 2 handling |
|---|---|---|
| ValidationRule | formula, message, description | verified (phase 1) |
| Flow (record-triggered) | trigger object, `start.filters` (entry conditions), `decisions[].rules[].conditions`, `recordUpdates`, `actionCalls`, labels and descriptions | entry + decision conditions **verified**; actions captured as a structured spec |
| CustomField type Formula | formula text, return type, `formulaTreatBlanksAs`, scale | **verified** as a transformation |
| AssignmentRules | criteria (field/operator/value) + assignee | criteria **verified**; assignee mapping captured |
| Workflow (rules, field updates, alerts) | criteria + actions | inventoried; new orgs cannot create them, ours has none |
| ApexTrigger / ApexClass | source | inventoried as not verifiable, with line counts and the objects they touch |
| ApprovalProcess | entry criteria + steps | criteria inventoried; steps not verifiable |
| RecordType, dependent picklists, FLS | metadata | inventoried as schema constraints for the mapping check |

The inventory endpoint becomes a coverage receipt across all of these: found / verifiable / verified / blocked /
ambiguous / not verifiable, per type, with the reason for every "not". That number on screen ("19 logic artefacts,
11 verifiable, 9 proven, 2 blocked, 8 not verifiable: Apex") is the honest answer to "did you capture the
configuration".

Browserbase is not used for capture. Flow Builder is a canvas; the XML is the truth.

## 2. Verify: one IR, three solver modes

### 2.1 Conditions (extend phase 1)
Flow entry filters and decision conditions are `field operator value` triples joined by AND/OR (or a custom
condition logic string like `1 AND (2 OR 3)`). Parse them into the existing `Node` IR. Assignment rule criteria are
the same shape. The solver and reference interpreter are unchanged; only a new parser (`parsers/flow_xml.py`) is
needed. The target expression is an Airtable automation trigger condition ("when record matches conditions"),
proposed by the LLM as a formula and verified exactly as today.

### 2.2 Transformations (new)
Formula fields return numbers, text, booleans or dates. Phase 2 covers **numeric and boolean** formulas over the
supported field types: `+ - * /`, `IF`, `CASE` on picklists, `BLANKVALUE`, `MIN/MAX`, `ROUND` to a fixed scale, with
the source's `formulaTreatBlanksAs` honoured. The IR gains an arithmetic `Expr` alongside `Node`. Z3 checks
`source_expr != target_expr` under both systems' blank semantics and scales; `sat` yields the input record and the
two different outputs. Airtable formula fields are API-creatable, so on PASS the field is written and read back.
Example that matters: `Amount * Discount__c / 100` (Salesforce percent stores 50) versus `{Amount} * {Discount}`
(Airtable stores 0.5): the literal copy is off by 100x and the solver says so with a record.

### 2.3 Schema mapping (new)
For each proposed source-to-target field mapping, encode as constraints: value domains (option sets, numeric range
from precision and scale, text length), nullability against target `required`, type compatibility, scale
transform. Z3 finds a legal source value with no legal target representation, e.g. picklist value
`Closed Won - Partner` with no option, Discount 150 exceeding a 0..1 percent, a 300-character loss reason into a
255-character field (reverse direction), a nullable Amount into a required target. Result per mapping:
`lossless`, `lossy(counterexample)`, `blocked(reason)`. This runs before any write and feeds the inventory.

## 3. Reconfigure: what the target can hold, and how we know it landed

| source artefact | target expression | write path | how we verify it landed |
|---|---|---|---|
| validation rule | guard formula field | Airtable API (phase 1) | read back the field and its formula |
| formula field | formula field | Airtable API | read back; behavioural check: create a probe record, read the computed value, compare with the source formula evaluated by the reference interpreter |
| flow / assignment condition + action | Airtable automation: trigger "record matches conditions" + action (update record, send email) | **no API exists**; two options below | behavioural: create a probe record that matches, poll the action's effect through the API (a checkbox or owner field), then delete the probe |
| everything else | none | none | reported as not verifiable |

Automation write path options, in the order we try them:
1. **Verified spec, human applies it.** OffBoard writes the exact trigger formula and action as a record in a
   `Migration_Automations` table, posts it to Discord, and then runs the behavioural verification once the human
   has clicked it in. This is honest, cheap, and already a full verify-after-configure loop.
2. **Browserbase + Stagehand (TypeScript) drives the Airtable automation builder** from the verified spec: open the
   base, create automation, choose trigger, set the conditions field by field, add the action, turn it on,
   screenshot. Time-boxed to 2 hours. It is the one place a browser adds capability the API lacks, so it earns the
   slot. If it is too brittle in the box, ship option 1 and record the attempt in the trace and the README.
   Login friction is real (Airtable password login, possible email code); a persisted Browserbase context reduces it.

Either way the proof that the configuration works is the behavioural check through the API, not a screenshot.
The screenshot is evidence of the click-through, attached to the trace.

## 4. Seed additions to the Dev org

- Formula field `Net_Amount__c` = `Amount - (Amount * BLANKVALUE(Discount__c, 0) / 100)` (scale 2, blanks as blanks):
  exercises arithmetic, percent scale and blank handling in one transformation.
- Formula field `Is_Big_Deal__c` (checkbox) = `Amount > 1000000 && NOT(ISPICKVAL(Type,"New Customer"))`: boolean.
- Record-triggered Flow `Flag_Stale_Negotiation`: entry `StageName = Negotiation/Review AND Probability < 50`,
  decision `Amount > 100000`, action: update `Needs_Review__c` checkbox (new field). Deployed as XML via
  `sf project deploy start`.
- Lead assignment rule with two criteria entries (region based), if time allows.
- Airtable: `Needs Review` checkbox and `Net Amount` will be created by the agent; `Migration_Automations` table
  created by hand from a short prompt.

Workflow Rules cannot be created in new orgs; the inventory will show zero and say why.

## 5. Eval additions

| case | proves |
|---|---|
| `formula_net_amount` | transformation proven equivalent, field written, probe record computed value matches |
| `formula_percent_literal_copy` | hand-edited target `{Amount} * {Discount}`: counterexample with the two outputs |
| `formula_blank_handling` | `formulaTreatBlanksAs` difference produces a counterexample on a blank Discount |
| `flow_entry_condition` | Flow entry filters parsed and verified against the proposed trigger formula |
| `flow_decision_not_verifiable_action` | condition proven, action captured as spec, verdict `PARTIAL` with the reason |
| `mapping_lossy_option_set` | schema mapping counterexample: `Closed Won - Partner` |
| `mapping_required_vs_nullable` | nullable source into required target blocked before write |
| `inventory_coverage` | inventory counts equal the retrieved metadata; every artefact has a status and a reason |
| `automation_behavioural_check` | probe record triggers the configured automation; effect read back; probe deleted; state bounded |

Protocol A and B as before; the coverage receipt becomes a headline number in the report.

## 6. Build order and estimates

| block | work | hours | depends on |
|---|---|---|---|
| P2-1 | Metadata retrieve via CLI, XML parsing into an `Artefact` model per type, inventory endpoint and UI across all types, seed the Flow and formula fields | 2.5 | nothing |
| P2-2 | Flow condition parser to `Node`; verify entry and decision conditions; action spec capture; `PARTIAL` verdict | 2 | P2-1 |
| P2-3 | Arithmetic `Expr` IR, formula parser (both systems), solver mode for transformations, reference interpreter extension, tests against enumerated inputs | 3 | nothing |
| P2-4 | Schema mapping constraints in Z3 with counterexample values; mapping report per field | 1.5 | nothing |
| P2-5 | Behavioural verification: probe record, poll effect, delete, bounded diff; `Migration_Automations` spec record | 1.5 | P2-2 |
| P2-6 | Eval cases above, cassettes, Protocol A, one Protocol B pass | 2 | P2-2..5 |
| P2-7 | Browserbase automation write path, time-boxed | 2 | P2-5 |
| P2-8 | README and skill docs update; model comparison table if time remains | 1 | all |

Total about 15 hours; P2-1, P2-3 and P2-4 are independent and can be built in any order. The demo after P2-6
shows: the coverage receipt, a formula field proven and written, a flow condition proven and its automation
verified by behaviour, and a lossy mapping caught with a value.

## 7. Risks and what we do about them

- **Flow XML variety.** Only record-triggered flows with filters, decisions, record updates and simple actions are
  parsed; screen flows, loops, subflows and Apex actions are inventoried as not verifiable. Stated, not hidden.
- **Formula function coverage.** Numeric and boolean subset only; text and date formulas are not verifiable in
  phase 2. Every unsupported function is named in the trace.
- **Airtable API budget.** Behavioural checks add about 4 calls per run; still far inside 1,000 per month.
- **Browserbase brittleness.** Time-boxed; option 1 is complete without it.
- **Deploying a Flow by XML** may need a few iterations against deploy errors; budgeted in P2-1.
