# Feature: pipeline, adapters, LLM proposer, API

Files: `app/pipeline.py`, `app/adapters/{schema,salesforce,airtable,discord}.py`, `app/llm.py`, `app/main.py`.
Tests: `tests/e2e/test_pipeline_test_mode.py` (through FastAPI, zero network), plus the eval harness.

## Purpose
Run one rule through extract, parse, duplicate check, propose, verify, decide, write, report, leaving a
complete trace, and do it identically against mocks (test key) or the real systems (live key).

## How it works
1. **Extract** (`SalesforceAdapter.get_rule`): Tooling API list query, then the per-rule `Metadata` fetch
   (Salesforce only allows `Metadata` when the query returns one row). Descriptions, error messages and
   `errorDisplayField` are kept: they carry the business intent.
2. **Parse** (`build_rule_ir`) with the field metadata from `describe`. Unsupported construct or unknown
   field: AMBIGUOUS, an audit record is written, no LLM call.
3. **Target schema + idempotency**: Airtable schema read, before-snapshot of all tables, and a lookup in
   `Migration_Rules` for a PASS record with this rule name. Found: DUPLICATE, no LLM call, no write.
4. **Propose** (`Proposer.propose`): OpenRouter chat completion with a strict JSON schema derived from
   `models.Proposal`. Modes `live`, `cassette` (replay, used by test mode), `record` (live and save).
   Prompt variants `informed` (production: explains blank handling, percent scale, label vs API name) and
   `naive` (ablation: those hints removed). The guard field name is normalised to `Guard: <rule>`; the
   model's suggestion is noted in the trace.
5. **Verify**: Airtable parser, then `check_equivalence`. Unparseable proposal: AMBIGUOUS (0.2).
   `SchemaMismatch`: AMBIGUOUS (0.0). Counterexample: FAIL (0.10). `unsat`: PASS (0.95).
6. **Write**: on PASS, create the guard formula field if missing (`exists` otherwise), then always write
   one `Migration_Rules` audit record (PASS, FAIL and AMBIGUOUS all get one; NOT_FOUND and DUPLICATE do
   not) and read it back.
7. **Report**: after-snapshot, diff, bounded-state check (`unexpected_changes`), structured claims checked
   against the diff, final Discord embed citing read-back ids, trace finalised with the canonical hash.

Every semantic assumption the run relies on is written first as an `assumption` entry with its evidence
and whether it was verified empirically (four of five were, on 2026-09-13).

## Adapters
- **Salesforce live** goes through `sf api request rest` (the CLI holds the refresh token). SOAP login is
  disabled by default on new orgs; the token `sf org display` exposes is rejected by the REST API
  (`INVALID_AUTH_HEADER`), so the CLI is the transport. `sf api request rest --method DELETE` fails
  client-side in CLI 2.150.6; use `sf data delete record`.
- **Salesforce mock** answers the exact Tooling paths the adapter uses from `fixtures/seed/salesforce.json`
  (exported from the real org by `scripts/export_seed.py`). Read-only.
- **Airtable live** uses raw REST via httpx (schema, records, create field, create record). Budget: a live
  run is about 12 calls including the two full snapshots; Free plan allows 1,000 per month.
- **Airtable mock** is seeded from `fixtures/seed/airtable.json` with `Guard:` fields stripped so it starts
  pre-migration; state persists in `state/mock_airtable.json` across runs (DUPLICATE and recovery cases
  need it) and `reset()` restores the seed. `inject_fault="airtable.create_record:503"` raises once.
  `filterByFormula` support is limited to `{Field} = "value"`, which is all the pipeline uses.
- **Discord** is best-effort: one embed per step, limits clipped, errors traced but never fatal.

## Decisions and why
- **Audit record for FAIL and AMBIGUOUS too.** "Surface it" means the destination system shows what was
  refused and why; the record carries `NOT WRITTEN · <proposal>` in Guard Formula.
- **Idempotency via the destination**, not a local file: the `Migration_Rules` lookup is the same in test
  and live, and it is what a human would check.
- **Bounded state as a pipeline output**, not only an eval concept: `unexpected_changes` is in every
  summary, so a live run that touches the wrong table says so itself.
- **Field-level security surprise (2026-09-13):** custom fields created via the Tooling API were invisible
  to the admin `describe` until `FieldPermissions` rows were added. The parser reported them as unknown
  fields and the rules came back AMBIGUOUS, which is the correct behaviour for missing metadata.
- **Cassette prompts must not leak.** The first recording had rule descriptions that literally described
  the traps; they were rewritten in the org as plain business intent, and the naive variant also drops the
  percent-scale annotations from the field list.

## Gotchas
- `python-dotenv` `load_dotenv()` from stdin fails; scripts pass the path explicitly.
- Airtable `Migration_Rules` confidence fields were created with precision 0 and cannot be changed via
  API; set to 2 decimals in the UI on 2026-09-13; the API cannot change it.
- `TestClient` is an `httpx.Client`; e2e tests block `httpx.HTTPTransport.handle_request`, not
  `Client.request`.

## How to test
`python -m pytest tests/e2e` (no network), `python -m eval.run_eval` (Protocol A), then the report at
`eval/report.md`. To re-record LLM cassettes: `python -m eval.record_cassettes --force` (needs
`OPENROUTER_API_KEY`, about 3 cents).

## How to extend
A new rule type needs a parser branch and a solver leaf, then a seed rule in the org
(`scripts/seed_salesforce.py`), `scripts/export_seed.py`, a case in `eval/cases.py`, and a cassette.
