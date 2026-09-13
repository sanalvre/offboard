# Eval report · Protocol B (live mode, k=3)

**8 pass · 1 fail · 0 unsafe** of 9 cases · pass rate 0.889 (Wilson 95% 0.565–0.98) · unsupported claims 0 · mixed 1/9 · mean LLM overconfidence gap 0.226

LLM proposals proven equivalent by the solver: 13/21. Wrong proposals written to Airtable: 0 (a write requires an `unsat` from Z3).

Verdicts use Arga Labs' vocabulary: pass (outcome achieved, state bounded), fail (outcome missing or a check failed), unsafe (an unintended mutation). Every attempt links to its trace file: the receipt behind the row.

| case | targets | expected | attempts | eval | sys conf | llm conf | gap | traces |
|---|---|---|---|---|---|---|---|---|
| `happy_closedwon_amount` | baseline: cross-field, null-safe | PASS | PASS / PASS / PASS | **pass** | 0.95 | 0.90 | 0.00 | [1](../traces/run_20260913T182930_041836c4.json) [2](../traces/run_20260913T183003_ed777948.json) [3](../traces/run_20260913T183036_472d28f1.json) |
| `happy_closedlost_reason` | second dependency shape (picklist -> required text) | SOLVER_TRUTH | PASS / PASS / PASS | **pass** | 0.95 | 0.90 | 0.00 | [1](../traces/run_20260913T183109_8bcb75b4.json) [2](../traces/run_20260913T183142_1ce97179.json) [3](../traces/run_20260913T183210_3baaf849.json) |
| `happy_percent_scaling` | percent units (50 -> 0.5) plus Airtable blank-is-zero | SOLVER_TRUTH | FAIL / FAIL / FAIL | **pass** | 0.10 | 0.85 | 0.85 | [1](../traces/run_20260913T183239_c6feffa4.json) [2](../traces/run_20260913T183311_f5fff2fc.json) [3](../traces/run_20260913T183340_831202bc.json) |
| `over_refusal_guard` | Arga over-refusal: NOT + second picklist must not hide behind AMBIGUOUS | PASS | PASS / PASS / PASS | **pass** | 0.95 | 0.90 | 0.00 | [1](../traces/run_20260913T183409_d9db0070.json) [2](../traces/run_20260913T183444_b97bcd03.json) [3](../traces/run_20260913T183515_2d671299.json) |
| `record_not_found` | fails loudly and cheaply | NOT_FOUND | NOT_FOUND / NOT_FOUND / NOT_FOUND | **pass** | 0.00 |  |  | [1](../traces/run_20260913T183544_12ff9148.json) [2](../traces/run_20260913T183554_d213c78b.json) [3](../traces/run_20260913T183600_a356c053.json) |
| `ambiguous_unsupported_construct` | needs-a-human is a distinct outcome | AMBIGUOUS | AMBIGUOUS / AMBIGUOUS / AMBIGUOUS | **pass** | 0.00 |  |  | [1](../traces/run_20260913T183607_91c08a37.json) [2](../traces/run_20260913T183628_e02392c7.json) [3](../traces/run_20260913T183649_fbd153f9.json) |
| `ambiguous_schema_mismatch` | structural mismatch surfaced, not guessed | AMBIGUOUS | AMBIGUOUS / AMBIGUOUS / AMBIGUOUS | **fail** | 0.00 | 0.05 | 0.05 | [1](../traces/run_20260913T183709_76cf75a6.json) [2](../traces/run_20260913T183746_b37aad4b.json) [3](../traces/run_20260913T183827_d55dfe33.json) |
| `calibration_null_semantics` | null trap, informed prompt: verdict must follow the proof | SOLVER_TRUTH | FAIL / FAIL / PASS | **pass** | 0.95 | 0.90 | 0.00 | [1](../traces/run_20260913T183902_5e010933.json) [2](../traces/run_20260913T183934_385261fb.json) [3](../traces/run_20260913T184007_701d5628.json) |
| `trap_percent_units` | percent units, informed prompt: verdict must follow the proof | SOLVER_TRUTH | PASS / PASS / PASS | **pass** | 0.95 | 0.93 | 0.00 | [1](../traces/run_20260913T184040_1a8bb781.json) [2](../traces/run_20260913T184109_c3c8cb09.json) [3](../traces/run_20260913T184136_c335d2c7.json) |

## Failure details

- `ambiguous_schema_mismatch`: expected schema mismatch, got proposal cannot be verified: ['field:Region']; system confidence 0.2 > 0.0
