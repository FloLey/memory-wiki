"""The model and cost layer of the dream.

Per-stage model selection (UI choice, then env, then default), the token/cost
ledger accumulator, the single model call, and JSON extraction. Kept apart from
the orchestration so the LLM contract lives in one place.
"""

from __future__ import annotations

import datetime
import json
import os

from wiki_server import prompts
from wiki_server.paths import resolve_under_root

MODELS_FILE = "dream_models.json"
DEFAULT_MODEL = "claude-opus-4-8"
STAGES = ("triage", "decide", "write")

# Models offered in the UI, cheapest-impacting first. Label carries the list
# price (per 1M tokens, in/out) so the cost trade-off is visible when choosing.
AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 : rapide, le moins cher (1 / 5 $/M)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 : bon compromis (3 / 15 $/M)"),
    ("claude-opus-4-8", "Opus 4.8 : le plus capable, le plus cher (5 / 25 $/M)"),
]
_MODEL_IDS = {m for m, _ in AVAILABLE_MODELS}
_MAX_TOKENS = {"triage": 4096, "decide": 4096, "write": 8192}

# Anthropic list prices per 1M tokens (USD), by model tier (2026). Unknown /
# self-hosted models price at 0.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def read_models() -> dict[str, str]:
    """Per-stage model overrides chosen in the UI, stored in the wiki. Only
    valid {stage: known-model} pairs are returned; anything else is ignored."""
    path = resolve_under_root(MODELS_FILE)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {s: data[s] for s in STAGES
            if isinstance(data.get(s), str) and data[s] in _MODEL_IDS}


def set_models(mapping: dict[str, str]) -> dict[str, str]:
    """Persist the per-stage model choices, keeping only valid pairs, and return
    what was stored. Committed like any other wiki change."""
    from wiki_server.store import write_file

    chosen = {s: mapping[s] for s in STAGES
              if isinstance(mapping.get(s), str) and mapping[s] in _MODEL_IDS}
    write_file(MODELS_FILE, json.dumps(chosen, indent=2) + "\n",
               "manual: set dream models")
    return chosen


def effective_models() -> dict[str, str]:
    """The model that will actually run for each stage (UI choice first)."""
    return {s: _model_for(s) for s in STAGES}


def _model_for(stage: str) -> str:
    # UI choice (wiki file) wins, then env overrides, then the default.
    chosen = read_models().get(stage)
    if chosen:
        return chosen
    return (os.environ.get(f"WIKI_DREAM_MODEL_{stage.upper()}")
            or os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL)


def _prices_for(model: str) -> tuple[float, float]:
    name = (model or "").lower()
    for tier, prices in _PRICES.items():
        if tier in name:
            return prices
    return (0.0, 0.0)


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    price_in, price_out = _prices_for(model)
    return in_tok / 1_000_000 * price_in + out_tok / 1_000_000 * price_out


class _Usage:
    """Accumulates token usage and cost across the pipeline's calls (which may
    use different models per stage)."""

    def __init__(self) -> None:
        self.in_tok = 0
        self.out_tok = 0
        self.cost = 0.0
        self.models: set[str] = set()
        self.stages: dict[str, dict] = {}

    def add(self, model: str, in_tok: int, out_tok: int, stage: str = "?") -> None:
        cost = _estimate_cost(model, in_tok, out_tok)
        self.in_tok += in_tok
        self.out_tok += out_tok
        self.cost += cost
        self.models.add(model)
        s = self.stages.setdefault(
            stage, {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "models": set()})
        s["input_tokens"] += in_tok
        s["output_tokens"] += out_tok
        s["cost"] += cost
        s["models"].add(model)

    def _stage_model(self, stage: str) -> str:
        return ", ".join(sorted(self.stages[stage]["models"])) or DEFAULT_MODEL

    def cost_lines(self) -> list[str]:
        """One human line per stage: model used, cost, tokens. Ordered."""
        known = ("triage", "decide", "write")
        order = [s for s in known if s in self.stages]
        order += [s for s in sorted(self.stages) if s not in known]
        lines = []
        for s in order:
            v = self.stages[s]
            lines.append(f"{s} : {self._stage_model(s)} : ${v['cost']:.4f} "
                         f"({v['input_tokens']:,} in / {v['output_tokens']:,} out)")
        return lines

    def entry(self, when: datetime.datetime) -> dict | None:
        if not (self.in_tok or self.out_tok):
            return None
        return {
            "timestamp": when.replace(microsecond=0).isoformat(),
            "model": ", ".join(sorted(self.models)) or DEFAULT_MODEL,
            "input_tokens": self.in_tok,
            "output_tokens": self.out_tok,
            "cost": round(self.cost, 6),
            "by_stage": {
                k: {"input_tokens": v["input_tokens"], "output_tokens": v["output_tokens"],
                    "cost": round(v["cost"], 6), "model": self._stage_model(k)}
                for k, v in self.stages.items()
            },
        }


def _parse_json(text: str) -> dict | None:
    """Extract a JSON object from model output. None if unparseable."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i].strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") and block.endswith("}"):
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _call_model(usage: _Usage, model: str, prompt: str, max_tokens: int, stage: str = "?") -> str | None:
    """Call the model once and return its text. Never raises: any failure
    (import, client init, API) returns None. Records token usage under ``stage``."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        u = getattr(message, "usage", None)
        usage.add(model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0, stage)
        return text
    except Exception:
        return None


def _stage(usage: _Usage, stage: str, context: str) -> dict | None:
    """Run one pipeline stage: build the prompt, call the model, parse JSON.
    Returns None on any failure."""
    text = _call_model(usage, _model_for(stage), prompts.build(stage, context),
                       _MAX_TOKENS.get(stage, 4096), stage)
    return _parse_json(text) if text is not None else None
