# Feature: trace layer (`app/trace.py`, `app/models.py`, `app/config.py`)

## Purpose
Every run leaves a receipt a judge can read step by step: `traces/<run_id>.jsonl` (appended live) and
`traces/<run_id>.json` (finalised, with a canonical hash). This is the evidence behind every Discord
headline and every eval row.

## How it works
- `Tracer(mode, rule, case_id, attempt)` writes a header line immediately, then one JSON line per entry.
  Entries carry `seq`, `ts`, `kind`, and `latency_ms` when produced through `tracer.span(...)`.
- Kinds: `llm_call` (exact messages, response_format, raw response, parsed object, cassette key),
  `tool_call` (tool, request, response or error), `solver`, `decision`, `state_snapshot`, `claims`,
  `assumption` (a semantic assumption with its evidence and whether it was verified empirically),
  `note`, `error`.
- `finalize(summary)` writes the `.json` with `canonical_hash` = SHA-256 over header + entries after
  removing volatile keys (`ts`, `latency_ms`, `run_id`, `attempt`, `elapsed_ms`, `started_at`,
  `finished_at`). Two test-mode runs of the same case must hash equal (eval Protocol A).
- `redact()` runs on every payload before it touches disk: configured secret values (OpenRouter key,
  Airtable PAT, Discord webhook token) plus token shapes (`sk-or-v1-…`, `pat….…`, `Bearer …`, webhook
  path token). Deterministic, so it does not disturb the hash.
- `config.resolve_mode(api_key)`: `sk_test_*` matching `RECEIPT_TEST_KEY` selects mocks and cassettes;
  `sk_live_*` matching `RECEIPT_LIVE_KEY` selects real adapters; anything else is 401.

## Decisions and why
- **jsonl first, json at the end.** A crash mid-run must still leave evidence (Arga: "claimed a result
  that did not happen" is only checkable if the partial trace survives).
- **Hash excludes only volatile keys, never content.** A change in any prompt, response, verdict or
  counterexample changes the hash; reproducibility means identical content, not identical timestamps.
- **Redaction inside the tracer, not at the call sites.** Call sites will forget. The tracer cannot.
- **`assumption` is its own entry kind.** The domain research showed the silent failures come from
  semantic assumptions (blank handling, percent scale). Making them explicit, with evidence and a
  `verified` flag, is the difference between "we believe" and "we checked".

## Gotchas
- `python-dotenv`'s `find_dotenv()` fails when run from stdin; `config.py` loads `.env` by explicit path.
- `traces/*.jsonl` is git-ignored; the finalised `.json` files for eval runs are committed on purpose.
- `Tracer.span` re-raises after recording; callers that want to continue must catch themselves.

## How to test
`python -m pytest tests/unit/test_trace.py`. Expected values are hand-computed or read back from disk,
including an independent SHA-256 recomputation for a fixed input.

## How to extend
Add a typed helper on `Tracer` for a new entry kind rather than calling `add()` with ad-hoc keys, so the
UI's trace viewer can render it.
