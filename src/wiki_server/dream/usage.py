"""The cost ledger: a JSON list of per-run usage entries under dream_reports/."""

from __future__ import annotations

import json

from wiki_server.paths import resolve_under_root
from wiki_server.store import write_files

from .config import USAGE_FILE


def read_usage() -> list[dict]:
    path = resolve_under_root(USAGE_FILE)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def reset_usage() -> None:
    """Clear the cost ledger so tracking starts over. The history stays in git."""
    write_files({USAGE_FILE: "[]\n"}, "manual: reset dream cost ledger")


def usage_summary() -> dict:
    entries = read_usage()
    total = sum(float(e.get("cost", 0)) for e in entries)
    runs = len(entries)
    by_stage: dict[str, dict] = {}
    for e in entries:
        for stage, v in (e.get("by_stage") or {}).items():
            agg = by_stage.setdefault(
                stage, {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "models": set()})
            agg["input_tokens"] += int(v.get("input_tokens", 0))
            agg["output_tokens"] += int(v.get("output_tokens", 0))
            agg["cost"] += float(v.get("cost", 0))
            if v.get("model"):
                agg["models"].add(v["model"])
    return {
        "runs": runs,
        "total_cost": total,
        "last_cost": float(entries[-1].get("cost", 0)) if entries else 0.0,
        "avg_cost": total / runs if runs else 0.0,
        "input_tokens": sum(int(e.get("input_tokens", 0)) for e in entries),
        "output_tokens": sum(int(e.get("output_tokens", 0)) for e in entries),
        "by_stage": by_stage,
    }
