"""Keep only the trace files referenced by eval/report.json (and any report_*.json), delete the rest.
Development runs pile up hundreds of traces; the committed set is the receipts behind the current report.

    python -m eval.prune_traces            # dry run
    python -m eval.prune_traces --apply
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import settings

KEEP_PREFIX = ("report",)


def referenced() -> set[str]:
    keep: set[str] = set()
    for rep in (settings.root / "eval").glob("report*.json"):
        d = json.loads(rep.read_text(encoding="utf-8"))
        for c in d.get("cases", []):
            for a in c.get("attempts", []):
                keep.add(a["trace_path"])
                keep.add(a["trace_path"].replace(".json", ".jsonl"))
    return keep


if __name__ == "__main__":
    keep = referenced()
    files = sorted(settings.traces_dir.glob("run_*.json*"))
    doomed = [f for f in files if f.name not in keep]
    print(f"{len(files)} trace files, {len(keep)//2} referenced runs, {len(doomed)} to delete")
    if "--apply" in sys.argv:
        for f in doomed:
            f.unlink()
        print("deleted")
    else:
        print("dry run; pass --apply to delete")
