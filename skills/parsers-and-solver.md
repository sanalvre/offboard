# Feature: formula parsers, reference interpreter and Z3 solver

Files: `app/parsers/common.py`, `app/parsers/sf_formula.py`, `app/parsers/at_formula.py`,
`app/reference.py`, `app/solver.py`. Tests: `tests/unit/test_sf_parser.py`, `test_at_parser.py`,
`test_solver.py`, `tests/regression/test_known_traps.py`.

## Purpose
Turn a Salesforce validation-rule formula and a proposed Airtable guard formula into the same small IR,
then let Z3 decide whether they mean the same thing for every possible record, under each system's own
semantics. The LLM proposes; this layer decides. A wrong proposal comes back with a concrete record on
which the two disagree.

## How it works
1. **IR** (`models.Node`): boolean tree of `and/or/not` over leaves `eq/ne/lt/lte/gt/gte(field, literal)`
   and `is_blank(field)`. Field metadata (`FieldSpec`) carries type, nullability, `scale` (Salesforce
   percent literal 50 means 50%, so scale 100; Airtable stores 0.5, scale 1), option lists and
   `compare_on` (Salesforce ISPICKVAL compares API names, Airtable selects compare labels).
2. **Salesforce parser**: recursive descent over `AND/OR/NOT`, `&& || !`, `ISPICKVAL`, `ISBLANK/ISNULL`,
   `TEXT(f) = "v"`, `BLANKVALUE(f, d) op v` (rewritten to a null-safe OR/AND), comparisons, literals,
   bare checkbox fields. Every other function, `$Variables`, unknown fields, unknown picklist values
   and field-to-field comparisons are collected in `unsupported` and the rule is not encodable.
3. **Airtable parser**: accepts guard shapes `IF(cond, "VIOLATION", "")`, `IF(cond, 1, 0)` or a bare
   condition; `{Field}`, `AND/OR/NOT`, comparisons, `{f}=BLANK()` / `!=BLANK()`, literals.
4. **Reference interpreter** (`reference.py`): plain-Python evaluation of a Node on a concrete record
   under `sf` or `at` semantics. No Z3. It is the oracle the solver is tested against.
5. **Solver** (`solver.py`): shared Z3 variables per source field (`present: Bool` plus a `Real` in
   canonical units, or an `EnumSort` over the union of option keys and literals plus `__other__`, or a
   `Bool` for checkboxes). The Airtable node is renamed onto source field names via the LLM's field map.
   Salesforce numeric leaves encode as `And(present, value*scale op lit)`; Airtable as
   `If(present, value*scale, 0) op lit`. Then `Solver.add(sf != at)`: `unsat` is equivalence, `sat`
   yields the counterexample record, both sides' truth values, and a leaf-by-leaf evaluation.
   Schema checks run before any solving and raise `SchemaMismatch` (unmapped field, source picklist value
   missing in the target select, type mismatch): the solver must never claim equivalence over a schema
   that cannot express the rule.

## Semantics encoded, with evidence
| Fact | Evidence |
|---|---|
| Salesforce: numeric comparison on a blank field is false (null) | Dev org probe 2026-09-13: blank Amount saved under `Amount <= 0` rule, Amount 0 rejected |
| Airtable: blank number is 0 in comparisons | Real base probe 2026-09-13: `{Amount}<=0` matched the blank record, `{Amount}<0` did not |
| Salesforce percent literal 50 means 50% | Salesforce percent field describe: precision 3 scale 0 on Probability; Discount__c created 5,2 |
| Airtable percent stores 0.5 for 50% | Airtable field model docs; base schema shows `percent` precision 2 |
| ISPICKVAL / select equality on blank is false; `<>` is its negation | Salesforce docs; recorded as an assumption in every trace |

## Decisions and why
- **Two evaluators, one variable set.** The trap cases are exactly the places where the same record
  means different things in the two systems. Sharing variables and differing only in the leaf encoding
  makes the disagreement record meaningful.
- **Equivalence, not implication.** A guard that flags more than the source rule (the null trap) is as
  wrong as one that flags less (the off-by-one). Both directions are reported with `source_fires` and
  `target_flags`.
- **Unsupported is a first-class result.** `PRIORVALUE`, dates, cross-object references never reach the
  solver; the pipeline turns them into AMBIGUOUS with the construct named.
- **Well-formedness is reported, not asserted.** Each side's `can_fire` / `can_pass` is recorded. Without
  domain bounds on numeric fields it rarely fails, so it is evidence, not a gate.
- **Unsat core is nominal.** For the equivalence problem the core is `sides_disagree` plus domain
  constraints; the human-readable "why" is the counterexample and the leaf evaluation, not the core.

## Gotchas
- Z3 `EnumSort` names are global per context; each check uses a unique prefix or later checks in the
  same process silently reuse the first sort (the tests caught this: pass alone, fail in the suite).
- The counterexample only lists fields that appear in either rule.
- Airtable `!=` and `<>` both parse; Salesforce `==` and `=` both parse.
- The Airtable parser treats `IF(cond, "ok", "VIOLATION")` (truthy in the else branch) as unsupported
  rather than negating it. Deliberate: guards should be written positively.

## How to test
`python -m pytest tests/unit tests/regression`. Solver tests enumerate a record grid with the reference
interpreter and, for every `not_equivalent`, re-evaluate the solver's own counterexample with the
interpreter to confirm the disagreement is real. Regression tests pin the counterexample content of the
three traps.

## How to extend
- New Salesforce function: add a branch in `_Parser.func` in `sf_formula.py`; if it needs new semantics,
  add the leaf op to `Node.Op`, `reference.evaluate` and `solver._encode` together, then add a grid test.
- New target system (HubSpot): write a parser producing the same Node, a `FieldSpec` mapper for its
  schema, and a third evaluator branch in both `reference.py` and `solver._encode`.
