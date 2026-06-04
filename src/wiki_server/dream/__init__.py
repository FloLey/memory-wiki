"""Nightly consolidation daemon (the "dream").

A three-stage pipeline so no single call ever needs the whole long-term memory:
1. triage  : cluster short-term memory and route each unit.
2. decide  : per unit, read only the touched pages and decide the change.
3. write   : per integrate/promote decision, produce the page's final content.

Dry-run stops after decide and reports the plan; execute applies everything in
one revertible commit, never deleting long-term content. The package is split
into focused modules:

- ``config``    : constants, the DREAM.md policy default, low-level reads.
- ``models``    : per-stage model choice, the cost ledger accumulator, the model
                  call and JSON extraction.
- ``index``     : long-term index regeneration.
- ``usage``     : the cost ledger (read / reset / summary).
- ``runner``    : the lock + key check + error-to-report guard.
- ``pipeline``  : triage / decide / write and the dry-run / execute entry points.
- ``migration`` : the one-shot entities split.

This module re-exports the public surface used by the server and the console.
"""

from __future__ import annotations

from .config import DREAM_POLICY, ensure_policy
from .migration import migrate_entities
from .models import AVAILABLE_MODELS, DEFAULT_MODEL, STAGES, effective_models, read_models, set_models
from .pipeline import list_reports, run_dry_run, run_execute
from .usage import read_usage, reset_usage, usage_summary

__all__ = [
    # pipeline entry points
    "run_dry_run", "run_execute", "list_reports",
    # migration
    "migrate_entities",
    # cost ledger
    "read_usage", "reset_usage", "usage_summary",
    # policy
    "ensure_policy", "DREAM_POLICY",
    # model selection
    "effective_models", "set_models", "read_models",
    "AVAILABLE_MODELS", "STAGES", "DEFAULT_MODEL",
]
