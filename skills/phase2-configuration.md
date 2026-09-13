# Feature: configuration capture and verified reconfiguration (phase 2)

Files: `app/inventory.py`, `app/artefacts.py`, `app/parsers/numeric.py`, `app/reference.py` (`evaluate_expr`),
`app/solver.py` (`check_transformation`), `app/pipeline.py` (`_run_artefact` and shared helpers).
Tests: `tests/unit/test_inventory.py`, `tests/unit/test_transformations.py`, eval cases `formula_*`, `flow_*`.
Plan and rationale: `skills/plan-phase2.md`.

## Purpose
Capture the whole configuration layer of the org, not just validation rules; verify what the solver can verify
(conditions, numeric transformations); reconfigure what the target can hold through its API; and say plainly what
was not verifiable. Three solver uses: constraint validation, transformation verification, schema mapping.

## Capture: the Metadata API
`sf project retrieve start --metadata Flow Workflow ApexTrigger ApexClass AssignmentRules ApprovalProcess
CustomObject:Opportunity` into `state/sfproj` (live, cached 5 minutes) or the committed copy in
`fixtures/seed/metadata` (test). `parse_metadata()` turns the XML into `Artefact`s with a `verifiable` flag and a
`reason`, and `attach_status()` reads the latest trace per artefact key. On 2026-09-13 the org held 15 artefacts:
8 validation rules, 2 formula fields, 2 flows (ours plus a Salesforce default transaction-security flow), 1 workflow
field update (Case), 2 assignment rules (Lead, Case). 12 verifiable.

Flow entry filters and decision-outcome conditions, and assignment-rule criteria, are lowered to the same `Node` IR
as validation rules (`flow_condition_node`, `assignment_criteria_node`), so the condition solver verifies them
unchanged. Unsupported operators (`IsChanged`, `contains`), `$User` references and custom condition-logic strings
are reported by name.

## Verify
- **Formula fields** (`run_formula`): numeric formulas parse to `Expr` (`+ - * /`, `IF`, `BLANKVALUE`, `MIN`, `MAX`,
  Airtable `BLANK()` as a result); checkbox formulas parse to `Node`. `check_transformation` asks Z3 whether any
  record makes the two formulas compute different canonical values or differ in blankness. Salesforce
  `formulaTreatBlanksAs` and percent scale (operands and result) are honoured; Airtable blank operands are 0.
  Division by zero is blank on both sides.
- **Flows** (`run_flow`): source condition = entry filters AND the decision outcome; the model proposes
  `IF(cond, 1, 0)`; `check_equivalence` decides.

## Reconfigure and confirm
- A proven formula field is created in Airtable as a real formula field (API-creatable), then a **behavioural
  probe**: a record with known inputs is created, Airtable's own computed value is read back and compared with the
  reference interpreter's evaluation of the Salesforce formula, and the probe is deleted. Live on 2026-09-13:
  Airtable computed 900 for Amount 1000 and Discount 10%; the Salesforce formula gives 900. The mock evaluates
  formula fields with the project's own parsers, so in test mode this check exercises the plumbing, not Airtable.
- A flow whose only action assigns a checkbox on the same record becomes a computed formula field (behaviourally
  equivalent, API-writable). Flows with other actions (email, record creation) get their proven condition plus an
  action spec recorded and the verdict `PARTIAL`: Airtable automations have no API.
- Non-verifiable artefacts (non-record flows, workflow field updates, Apex) get an audit record and AMBIGUOUS with
  the reason; no model call.

## Schema mapping report (`app/mapping.py`, `GET /mapping`, Mapping tab)
For every source field that a verifiable artefact references, a target is proposed by label or alias (no model) and
checked: types must be the same family; picklist options are checked with Z3 over the union enum (a source option with
no target option is the counterexample); a nullable source into a required target is lossy with the blank as the
counterexample; numeric decimals are checked with Z3 using `IsInt` (a source value with `scale` decimals in source
units whose value in target units needs more than the target's precision). Scale differences (percent 100 vs 1) are
lossless with a recorded transform. On the demo org: 5 lossless, 1 lossy (`Discount__c`: 0.01 in Salesforce percent
units is 0.0001 as a fraction, four decimals against Airtable's two), 1 blocked (`Region__c`, no target field).
The first version used a quantified `Exists` for the decimal check and did not terminate; `IsInt` is decidable.

## What the recordings and live runs showed
| artefact | model (Sonnet 5, informed prompt) | solver |
|---|---|---|
| `Net_Amount__c` (blanks as blanks) | first recording: no blank result, conf 0.90 → blocked (source blank, target 0). After the prompt said `BLANK()` may be returned: correct, conf 0.82, proven; live probe 900 = 900 | |
| `Is_Big_Deal__c` (checkbox formula) | `{Type} != "New Customer"` for `NOT(ISPICKVAL(...))`, conf 0.85 | proven equivalent |
| `Flag_Stale_Negotiation` flow | omitted the blank guard on Probability, conf 0.80 to 0.90 | blocked: `{Negotiation/Review, Amount 100001, Probability blank}` |
| hand-edited `/100` copy of Net Amount | | blocked with both outputs |

## Decisions and why
- **Fresh Z3 context per check.** Global sort names made counterexamples depend on how many checks had run before;
  a fresh `Context` gives stable names, deterministic models and reproducible traces.
- **Fields created during a run are excluded from the records-changed diff.** A new computed field changes every
  record's snapshot; that is the intended change. Everything else still trips the unsafe verdict.
- **Computed field instead of automation for single-assignment flows.** It is provable and writable; an automation
  is neither. Where the action is not a same-record assignment, we stop at PARTIAL rather than pretend.
- **The model may return BLANK().** Without it the model could not express Salesforce's blank-as-blank result and
  every such formula would fail verification for a reason the model could not fix.

## Gotchas
- Fields created via the Tooling API need `FieldPermissions` for the admin profile or `describe` hides them.
- `shutil.rmtree` on the retrieve directory fails under OneDrive; the retrieve overwrites in place instead.
- Formula fields returning text or dates are inventoried as not verifiable.
- The Salesforce default `sfdc_default_ReportExport_Protection_Flow` is a real artefact in every new org; it is a
  good demo of "found, not verifiable, here is why".

## How to extend
New artefact type: add a branch in `parse_metadata`, a lowering to `Node`/`Expr`, and a `run_<type>` in
`artefacts.py` using the Pipeline helpers (`prepare_target`, `decide_from_solver`, `_ensure_formula_field`,
`_write_audit`, `_finish`).
