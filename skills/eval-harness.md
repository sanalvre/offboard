# Feature: eval harness (`eval/cases.py`, `eval/run_eval.py`, `eval/record_cassettes.py`)

## Purpose
Show, not assert, that the system is reliable: every case links to the trace it was graded on, verdicts use
Arga Labs' vocabulary (pass / fail / unsafe), and the LLM's accuracy is reported separately from the
system's correctness.

## How it works
- `eval/cases.py`: 17 cases. Each has a rule, an expected verdict, the failure class it targets, and a list
  of check functions over the finalised trace document. Expected vocabulary: exact verdicts, `NOT_PASS`
  (the write must be blocked), and `SOLVER_TRUTH` (the verdict must follow the solver: PASS iff `unsat`,
  FAIL iff counterexample, AMBIGUOUS iff unverifiable). `SOLVER_TRUTH` is used for LLM-dependent cases
  because the model's output is stochastic: the harness asserts the system is consistent with the proof
  and reports how often the model was right.
- Invariants applied to every case (plan.md 3.4): zero unsupported claims, no unexpected state changes
  (any means `unsafe`), and a final Discord result post.
- `run_eval.py` Protocol A: test mode, each case run twice from a reset mock; hashes must match. Cases can
  declare a `pre` run (DUPLICATE runs the hero first), a `rerun` with a fault injected on the first attempt
  (recovery), and separate checks for the second attempt. Output: `eval/report.json`, `eval/report.md`
  with a Wilson 95% interval, reproducible count, LLM proposals proven, wrong proposals written (0 by
  construction, asserted by the bounded diff).
- Protocol B (`--protocol B -k 3`): live mode against the real org, base, model and Discord. Before every attempt
  `live_reset()` deletes only the `Migration_Rules` records OffBoard wrote (guard fields persist; Airtable has no
  delete-field API, so later attempts report `exists`). Cases marked `live_ok=False` (fault injection, hand-edited
  cassettes, prompt ablations, the mock-only distractor) are skipped. Reports mixed cases instead of reproducibility,
  to `eval/report_live.md`.
- `record_cassettes.py`: runs the pipeline with mocks and the live model in `record` mode, saving raw
  responses under `fixtures/cassettes/<case>.json`. Three cassettes are derived by hand from recorded ones
  (`derive()` in the recorder) with the guard formula replaced and the edit labelled inside the file.

## What the 2026-09-13 recordings showed

Cassettes are recorded with the model in `.env` (`anthropic/claude-sonnet-5`). An earlier recording with
`anthropic/claude-opus-5` behaved differently on the same prompts, which is itself a finding: the informed prompt
fixed both traps for Opus, and only the percent trap for Sonnet.

| case (Sonnet 5, final cassettes) | proposal | solver |
|---|---|---|
| null trap, informed prompt | `AND({Stage}="Closed Won", {Amount}<=0)`, no blank guard, conf 0.75 | counterexample `{Closed Won, blank}`, blocked |
| null trap, naive prompt | same formula, conf 0.95 | blocked |
| percent trap, informed prompt | `{Discount} > 0.5`, conf 0.90 | equivalent, written |
| percent trap, naive prompt | `{Discount} > 0.5`, conf 0.85 | equivalent, written |
| percent with blank (`Probability < 50`) | `{Probability} < 0.5`, no blank guard, conf 0.82 | counterexample `{Negotiation/Review, blank}`, blocked |
| schema mismatch (Region) | mapped Region to "(no counterpart)", dropped the condition, conf 0.15 | schema check raised before solving |
| hero, closed-lost, over-refusal | correct | equivalent, written |

Twelve of 28 LLM attempts across the LLM cases were proven equivalent; every wrong one was blocked; mean
overconfidence gap 0.37. Three hand-edited cassettes (`weaker_translation`, `naive_null_translation`,
`percent_units_literal`, each labelled inside the file with the original formula) guarantee the solver-catch cases
stay demonstrable when cassettes are re-recorded and the model happens to get them right.

## Decisions and why
- **Grade the system, report the model.** An eval that fails when the model is wrong measures the model;
  ours must measure whether a wrong proposal can reach Airtable. It cannot: a write requires `unsat`.
- **Reproducibility by content hash**, with run ids (including earlier runs' ids embedded in observed
  state), timestamps and latencies normalised. Seventeen of seventeen reproduce.
- **Prove the grader can fail.** `tests/e2e` forces a rogue write inside the mock and asserts the harness
  returns `unsafe`. A grader that only ever passes is not evidence.
- **Prompt ablation instead of a rigged trap.** The first plan assumed the model would fall for the traps.
  With an informed prompt it mostly does not; the honest demonstration is the same rule under both prompts.

## Gotchas
- Traces pile up in `traces/`; commit only those referenced by the current report.
- The mock's `filterByFormula` only supports `{Field} = "value"`.
- Re-recording cassettes changes the source proposals the hand-edited cassettes are derived from; the recorder
  rebuilds all three derived cassettes every run (`python -m eval.record_cassettes derived` rebuilds only those).

## How to extend
Add a `Case` with checks over the trace document, a seed rule if needed, and record a cassette.
For Protocol B, add `eval/reset_live.py` (delete non-seed `Migration_Rules` records) before running.
