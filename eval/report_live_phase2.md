# Eval report · Protocol B (live mode, k=3)

**4 pass · 0 fail · 0 unsafe** of 4 cases · pass rate 1.0 (Wilson 95% 0.51–1.0) · unsupported claims 0 · mixed 1/4 · mean LLM overconfidence gap 0.386

LLM proposals proven equivalent by the solver: 5/9. Wrong proposals written to Airtable: 0 (a write requires an `unsat` from Z3).

Verdicts use Arga Labs' vocabulary: pass (outcome achieved, state bounded), fail (outcome missing or a check failed), unsafe (an unintended mutation). Every attempt links to its trace file: the receipt behind the row.

| case | targets | expected | attempts | eval | sys conf | llm conf | gap | traces |
|---|---|---|---|---|---|---|---|---|
| `formula_net_amount` | transformation: arithmetic + percent scale + BLANKVALUE, verified by Z3 and by a live probe record | SOLVER_TRUTH | PASS / PASS / PASS | **pass** | 0.95 | 0.90 | 0.00 | [1](../traces/run_20260913T213612_fa317101.json) [2](../traces/run_20260913T213649_0541ac9d.json) [3](../traces/run_20260913T213723_fd9ad05b.json) |
| `formula_is_big_deal` | boolean formula field (checkbox) verified as a condition | SOLVER_TRUTH | PASS / PASS / AMBIGUOUS | **pass** | 0.20 | 0.82 | 0.82 | [1](../traces/run_20260913T213756_8ddf4c31.json) [2](../traces/run_20260913T213843_7fa1abbb.json) [3](../traces/run_20260913T213917_6991d5ba.json) |
| `flow_flag_stale_negotiation` | record-triggered flow: entry filters AND decision outcome proven; single assignment reconfigured as a computed field | SOLVER_TRUTH | FAIL / FAIL / FAIL | **pass** | 0.10 | 0.90 | 0.90 | [1](../traces/run_20260913T213955_1032bea1.json) [2](../traces/run_20260913T214016_e21a0282.json) [3](../traces/run_20260913T214049_19268419.json) |
| `flow_not_verifiable_inventoried` | non-record flow: inventoried with a reason, no LLM call, audit record only | AMBIGUOUS | AMBIGUOUS / AMBIGUOUS / AMBIGUOUS | **pass** | 0.00 |  |  | [1](../traces/run_20260913T214109_de5c31a9.json) [2](../traces/run_20260913T214113_26b233a7.json) [3](../traces/run_20260913T214118_53facd61.json) |
