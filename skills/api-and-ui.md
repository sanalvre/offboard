# Feature: API and UI (`app/main.py`, `app/ui/index.html`)

## Purpose
One FastAPI service that runs the pipeline in either mode, serves traces and reports, and a single static page
that reads like an evidence log: runs, one run's trace step by step, the eval report, and the coverage inventory.

## Endpoints
| method | path | auth | what |
|---|---|---|---|
| POST | `/runs` | key | run one rule; body `RunRequest` (`rule`, `case_id`, `attempt`, `cassette`, `inject_fault`, `prompt_variant`); returns `RunSummary` |
| GET | `/runs` | none | list finalised traces (newest first) |
| GET | `/runs/{run_id}` | none | the full trace document |
| GET | `/inventory` | key | coverage receipt: every validation rule on the object with `proven` / `blocked` / `ambiguous` / `not_attempted` from the latest trace in that mode |
| POST | `/state/reset` | key | test mode only: restore the mock base to the seed |
| GET | `/eval/report` | none | `eval/report.json` |
| GET | `/` | none | the UI |

Mode: `X-API-Key` equal to `OFFBOARD_TEST_KEY` (must start `sk_test_`) or `OFFBOARD_LIVE_KEY` (`sk_live_`); anything
else is 401. Test mode: `MockSalesforce`, `MockAirtable`, `MockDiscord`, cassette replay. Live: CLI transport,
Airtable REST, webhook, live model.

## UI
Vanilla HTML/JS, no build step, served from `app/ui/index.html`. Black on white, one monospace stack, 1px rules,
verdict badges (PASS outlined; FAIL/AMBIGUOUS/ERROR inverted; NOT_FOUND/DUPLICATE dashed). Views:
- **Runs**: table of runs plus a toolbar to run any seeded rule under either prompt variant and reset test state.
  In test mode the toolbar picks the cassette for the rule.
- **Run**: KPI strip (verdict, system and LLM confidence, gap, unsupported claims, unexpected changes, hash), then
  every trace entry in order with a one-line summary, the raw entry, and for solver entries the SMT-LIB text, for
  LLM entries the exact prompt.
- **Eval**: report KPIs and the case table with links to each attempt's trace.
- **Inventory**: coverage counts and per-rule status with business intent.

## Decisions and why
- Synchronous `/runs`: a run takes 5 to 20 s; simpler than job polling for a demo and keeps the trace path in the
  response.
- Traces are read from disk, not memory, so the UI shows runs from the CLI eval as well.
- The UI never receives secrets; the trace is redacted at write time.

## How to test
`tests/e2e/test_pipeline_test_mode.py` covers auth, runs, inventory and reset through `TestClient`. The UI is
static; open `http://127.0.0.1:8000` after `uvicorn app.main:app`.

## How to extend
Add an entry kind to `summarize()` in the UI when adding a `Tracer` helper, or it renders as raw JSON only.
