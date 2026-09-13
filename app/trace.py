"""Per-run trace: the receipt behind every verdict.

Every entry is appended to traces/<run_id>.jsonl as it happens (a crash still leaves evidence) and the
whole run is finalised into traces/<run_id>.json with a canonical hash.

Entry kinds: llm_call, tool_call, solver, decision, state_snapshot, claims, assumption, note, error.
Every entry has seq, ts, kind, and latency_ms when produced by a span.

Canonical hash: SHA-256 over the entries with volatile fields removed (timestamps, latencies, run/attempt
ids, and anything under keys listed in VOLATILE_KEYS). Two test-mode runs of the same case must hash equal.

Redaction: secret values from Settings and common token shapes are replaced before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings

_RUN_ID_RE = re.compile(r"run_\d{8}T\d{6}_[0-9a-f]{8}")
VOLATILE_KEYS = {"ts", "latency_ms", "run_id", "attempt", "elapsed_ms", "started_at", "finished_at", "createdTime", "Migrated At", "recorded_at"}

_TOKEN_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),
    re.compile(r"pat[A-Za-z0-9]{14}\.[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer [A-Za-z0-9._\-!]{16,}"),
    re.compile(r"(webhooks/\d{15,}/)[A-Za-z0-9_\-]{30,}"),
]


def redact(value: Any, secrets: Optional[list[str]] = None) -> Any:
    """Recursively replace secret material in strings. Deterministic, so it does not disturb hashing."""
    secrets = secrets if secrets is not None else settings.secrets()
    if isinstance(value, str):
        s = value
        for sec in secrets:
            if sec and sec in s:
                s = s.replace(sec, "<redacted>")
        for pat in _TOKEN_PATTERNS:
            s = pat.sub(lambda m: (m.group(1) + "<redacted>") if m.lastindex else "<redacted>", s)
        return s
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    return value


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def canonical_hash(entries: list[dict], header: dict) -> str:
    """SHA-256 over header + entries with volatile keys removed and the run id (which leaks into audit records,
    field descriptions and Discord footers as plain text) replaced by a placeholder."""
    payload = {"header": strip_volatile(header), "entries": strip_volatile(entries)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    # any run id (this run's, or an earlier run's that is now part of the observed state) is a placeholder
    blob = _RUN_ID_RE.sub("<run_id>", blob)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    def __init__(self, mode: str, rule: str, case_id: Optional[str] = None, attempt: int = 1,
                 run_id: Optional[str] = None, traces_dir: Optional[Path] = None):
        self.run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self.dir = Path(traces_dir or settings.traces_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.header: dict[str, Any] = {
            "run_id": self.run_id, "mode": mode, "rule": rule, "case_id": case_id, "attempt": attempt,
            "started_at": _now(), "schema": "receipt-trace/1",
        }
        self.entries: list[dict[str, Any]] = []
        self._seq = 0
        self.jsonl_path = self.dir / f"{self.run_id}.jsonl"
        self.json_path = self.dir / f"{self.run_id}.json"
        with self.jsonl_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"header": redact(self.header)}, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ core

    def add(self, kind: str, **payload: Any) -> int:
        self._seq += 1
        entry = {"seq": self._seq, "ts": _now(), "kind": kind, **redact(payload)}
        self.entries.append(entry)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return self._seq

    @contextmanager
    def span(self, kind: str, **payload: Any) -> Iterator[dict[str, Any]]:
        """Time a step; the caller fills `out` (dict) with the result, or an exception is recorded verbatim."""
        out: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            yield out
        except Exception as e:  # noqa: BLE001 - the whole point is to record it
            out.setdefault("error", {"type": type(e).__name__, "message": str(e)})
            self.add(kind, latency_ms=round((time.perf_counter() - t0) * 1000, 1), **payload, **out)
            raise
        self.add(kind, latency_ms=round((time.perf_counter() - t0) * 1000, 1), **payload, **out)

    # ------------------------------------------------------------------ typed helpers

    def tool_call(self, tool: str, request: dict, response: Any = None, error: Any = None, latency_ms: float = 0.0) -> int:
        payload = {"tool": tool, "request": request}
        if error is not None:
            payload["error"] = error
        else:
            payload["response"] = response
        return self.add("tool_call", latency_ms=latency_ms, **payload)

    def llm_call(self, model: str, messages: list, response_format: Any, raw_response: Any, parsed: Any,
                 mode: str, cassette_key: Optional[str], latency_ms: float, error: Any = None) -> int:
        return self.add("llm_call", model=model, messages=messages, response_format=response_format,
                        raw_response=raw_response, parsed=parsed, mode=mode, cassette_key=cassette_key,
                        latency_ms=latency_ms, error=error)

    def assumption(self, name: str, statement: str, evidence: str, verified: bool) -> int:
        return self.add("assumption", name=name, statement=statement, evidence=evidence, verified=verified)

    def note(self, message: str, **extra: Any) -> int:
        return self.add("note", message=message, **extra)

    # ------------------------------------------------------------------ finalise

    def finalize(self, summary: dict[str, Any]) -> dict[str, Any]:
        self.header["finished_at"] = _now()
        h = canonical_hash(self.entries, {**self.header, "summary": strip_volatile(summary)})
        doc = {"header": redact(self.header), "summary": redact(summary), "canonical_hash": h, "entries": self.entries}
        self.json_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return doc
