"""Live scenario reset for Protocol B: delete the Migration_Rules records Receipt wrote (Run ID starts with
`run_`), leaving the 15 seeded records and every other table untouched. Guard formula fields cannot be
deleted via the Airtable API, so they persist and later runs report them as `exists`.

    python -m eval.reset_live            # dry run
    python -m eval.reset_live --apply
"""
from __future__ import annotations

import sys

import httpx

from app.config import settings

if __name__ == "__main__":
    h = {"Authorization": f"Bearer {settings.airtable_pat}"}
    base = settings.airtable_base_id
    url = f"https://api.airtable.com/v0/{base}/Migration_Rules"
    recs, offset = [], None
    while True:
        params = {"filterByFormula": 'LEFT({Run ID}, 4) = "run_"'}
        if offset:
            params["offset"] = offset
        page = httpx.get(url, headers=h, params=params, timeout=30).json()
        recs += page.get("records", [])
        offset = page.get("offset")
        if not offset:
            break
    print(f"{len(recs)} Receipt-written Migration_Rules records:", [r["fields"].get("Rule") for r in recs])
    if "--apply" in sys.argv and recs:
        for i in range(0, len(recs), 10):
            ids = [r["id"] for r in recs[i:i + 10]]
            r = httpx.delete(url, headers=h, params=[("records[]", i) for i in ids], timeout=30)
            print("delete", r.status_code, len(ids))
    elif recs:
        print("dry run; pass --apply to delete")
