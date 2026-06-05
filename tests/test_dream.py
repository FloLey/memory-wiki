"""Dream pipeline: parsing, index, model choice, execution, migration.

Internals are tested through their own submodule (``pipeline``, ``models`` ...);
the package root re-exports only the public surface.
"""

import datetime

from wiki_server import dream
from wiki_server.dream import index, migration, models, pipeline, runner, usage


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# --- pure helpers ---------------------------------------------------------

def test_parse_json_plain():
    assert models._parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_in_code_fence():
    assert models._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_garbage():
    assert models._parse_json("not json") is None


def test_as_list_normalizes():
    assert pipeline._as_list(None) == []
    assert pipeline._as_list({"x": 1}) == [{"x": 1}]
    assert pipeline._as_list([1, 2]) == [1, 2]


def test_render_index_groups_known_then_legacy():
    paths = {
        "long_term/self/identity.md",
        "long_term/people/maryse.md",
        "long_term/entities/old.md",  # legacy folder, not a known category
    }
    descs = {"long_term/people/maryse.md": "soeur"}
    out = index._render_index(paths, descs)
    assert "## people" in out and "people/maryse.md" in out and "soeur" in out
    # Known categories come first, the leftover legacy folder is appended.
    assert out.index("## people") < out.index("## entities")


# --- per-stage cost -------------------------------------------------------

def test_usage_tracks_cost_per_stage():
    u = models._Usage()
    u.add("claude-haiku-4-5-20251001", 1_000_000, 0, "triage")  # 1$ in
    u.add("claude-opus-4-8", 0, 1_000_000, "write")             # 25$ out
    e = u.entry(datetime.datetime(2026, 6, 4))
    assert e["by_stage"]["triage"]["cost"] == 1.0
    assert e["by_stage"]["write"]["cost"] == 25.0
    assert e["cost"] == 26.0


def test_cost_lines_shows_model_and_price():
    u = models._Usage()
    u.add("claude-haiku-4-5-20251001", 1_000_000, 0, "triage")
    lines = u.cost_lines()
    assert len(lines) == 1
    assert "triage" in lines[0] and "haiku" in lines[0] and "$1.00" in lines[0]


def test_usage_summary_aggregates_stages(wiki, write):
    import json
    write("dream_reports/usage.json", json.dumps([
        {"cost": 2.0, "by_stage": {"triage": {"input_tokens": 1, "output_tokens": 0, "cost": 1.0},
                                   "decide": {"input_tokens": 0, "output_tokens": 2, "cost": 1.0}}},
        {"cost": 1.0, "by_stage": {"triage": {"input_tokens": 3, "output_tokens": 0, "cost": 1.0}}},
    ]))
    s = usage.usage_summary()
    assert s["by_stage"]["triage"]["cost"] == 2.0
    assert s["by_stage"]["triage"]["input_tokens"] == 4
    assert s["by_stage"]["decide"]["cost"] == 1.0


# --- model selection ------------------------------------------------------

def test_model_default_is_opus(wiki):
    assert dream.effective_models() == {s: dream.DEFAULT_MODEL for s in dream.STAGES}


def test_set_models_validates_and_persists(wiki):
    stored = dream.set_models(
        {"triage": "claude-haiku-4-5-20251001", "decide": "claude-sonnet-4-6", "write": "bogus"}
    )
    assert stored == {"triage": "claude-haiku-4-5-20251001", "decide": "claude-sonnet-4-6"}
    eff = dream.effective_models()
    assert eff["triage"] == "claude-haiku-4-5-20251001"
    assert eff["write"] == dream.DEFAULT_MODEL  # unset -> default


def test_ui_model_wins_over_env(wiki, monkeypatch):
    dream.set_models({"triage": "claude-haiku-4-5-20251001"})
    monkeypatch.setenv("WIKI_DREAM_MODEL_TRIAGE", "claude-opus-4-8")
    assert models._model_for("triage") == "claude-haiku-4-5-20251001"


# --- execution ------------------------------------------------------------

def _stm(write, *names):
    write("short_term/index.md", "# STM\n")
    write("long_term/index.md", "# LTM\n")
    for n in names:
        write(f"short_term/entries/{n}.md", f"---\ncreated: 2026-06-04\n---\n\n{n}\n")


def test_execute_cumulative_merge(wiki, write, monkeypatch):
    _stm(write, "a", "b")
    monkeypatch.setattr(pipeline, "_decisions", lambda u, p, se, no: [
        {"unit": {"stm": ["a.md"]}, "decision": {"pages": [
            {"action": "promote", "page": "long_term/people/maryse.md", "change": "soeur"}]}},
        {"unit": {"stm": ["b.md"]}, "decision": {"pages": [
            {"action": "promote", "page": "long_term/people/maryse.md", "change": "mere"}]}},
    ])
    # The write stage merges onto whatever it receives as current content.
    monkeypatch.setattr(pipeline, "_write_page",
                        lambda u, p, op, current="": {"content": (current or "# p\n") + f"\n- {op['change']}", "description": "d"})
    _, report = pipeline._execute(_now(), "2026-06-04")
    page = (wiki / "long_term/people/maryse.md").read_text()
    assert "soeur" in page and "mere" in page  # neither contribution overwritten
    # Report has the plan and a deduped applied summary: one page line, listed once.
    assert "## Plan" in report and "## Appliqué" in report
    assert report.count("long_term/people/maryse.md : touchée par 2") == 1
    assert "1 page(s) écrite(s) : long_term/people/maryse.md" in report


def test_execute_writes_each_page_once(wiki, write, monkeypatch):
    _stm(write, "a", "b", "c")
    monkeypatch.setattr(pipeline, "_decisions", lambda u, p, se, no: [
        {"unit": {"stm": [f"{n}.md"]}, "decision": {"pages": [
            {"action": "promote", "page": "long_term/self/identity.md", "change": n}]}}
        for n in ("a", "b", "c")
    ])
    calls = {"n": 0}

    def counting_write(u, p, op, current=""):
        calls["n"] += 1
        return {"content": (current or "# id\n") + op["change"], "description": "d"}

    monkeypatch.setattr(pipeline, "_write_page", counting_write)
    pipeline._execute(_now(), "2026-06-04")
    # Three units touch the same page, but it is written only once.
    assert calls["n"] == 1
    page = (wiki / "long_term/self/identity.md").read_text()
    assert "a" in page and "b" in page and "c" in page


def test_normalize_page_adds_long_term_prefix():
    assert pipeline._normalize_page("people/maud.md") == "long_term/people/maud.md"
    assert pipeline._normalize_page("long_term/self/identity.md") == "long_term/self/identity.md"
    assert pipeline._normalize_page("/projects/x.md") == "long_term/projects/x.md"
    assert pipeline._normalize_page("/long_term/people/maud.md") == "long_term/people/maud.md"
    assert pipeline._normalize_page("not a path") is None
    assert pipeline._normalize_page(None) is None


def test_execute_applies_category_relative_path(wiki, write, monkeypatch):
    _stm(write, "a")
    monkeypatch.setattr(pipeline, "_decisions", lambda u, p, se, no: [
        {"unit": {"stm": ["a.md"]}, "decision": {"pages": [
            {"action": "promote", "page": "people/maud.md", "change": "copine"}]}},
    ])
    monkeypatch.setattr(pipeline, "_write_page",
                        lambda u, p, op, current="": {"content": "# Maud\n", "description": "d"})
    pipeline._execute(_now(), "2026-06-04")
    # The prefix-less path is applied under long_term/, not rejected.
    assert (wiki / "long_term/people/maud.md").exists()
    assert not (wiki / "short_term/entries/a.md").exists()  # STM consumed (it worked)


def test_execute_atomic_keeps_stm_on_failure(wiki, write, monkeypatch):
    _stm(write, "a")
    monkeypatch.setattr(pipeline, "_decisions", lambda u, p, se, no: [
        {"unit": {"stm": ["a.md"]}, "decision": {
            "pages": [{"action": "promote", "page": "long_term/people/ok.md", "change": "x"},
                      {"action": "integrate", "page": "long_term/private/secret.md", "change": "y"}],
            "temporal": [{"type": "event", "due": "2026-07-01", "content": "evt"}]}},
    ])
    monkeypatch.setattr(pipeline, "_write_page",
                        lambda u, p, op, current="": {"content": "# x\n", "description": "d"})
    pipeline._execute(_now(), "2026-06-04")
    assert (wiki / "long_term/people/ok.md").exists()       # valid output applied
    assert not (wiki / "long_term/private/secret.md").exists()  # forbidden skipped
    assert (wiki / "short_term/entries/a.md").exists()      # STM kept (a part failed)


def test_execute_skips_dateless_temporal(wiki, write, monkeypatch):
    _stm(write, "a")
    monkeypatch.setattr(pipeline, "_decisions", lambda u, p, se, no: [
        {"unit": {"stm": ["a.md"]}, "decision": {
            "pages": [], "temporal": [{"type": "event", "due": None, "content": "naissance"}]}},
    ])
    pipeline._execute(_now(), "2026-06-04")
    assert list((wiki / "temporal").glob("*.md")) == []     # no never-expiring item
    assert (wiki / "short_term/entries/a.md").exists()      # nothing produced -> STM kept


# --- migration ------------------------------------------------------------

def test_migrate_entities(wiki, write, monkeypatch):
    write("long_term/index.md", "# LTM\n")
    write("long_term/entities/maryse.md", "---\ndescription: soeur\n---\n\n# Maryse\n\n[A](../entities/amaury.md)\n")
    write("long_term/entities/amaury.md", "---\ndescription: bebe\n---\n\n# Amaury\n")
    write("long_term/entities/dnb.md", "---\ndescription: emp\n---\n\n# DnB\n")
    monkeypatch.setattr(migration, "_classify_entities",
                        lambda usage, items: {"maryse": "people", "amaury": "people", "dnb": "organizations"})
    migration._migrate(_now(), "2026-06-04")
    assert (wiki / "long_term/people/maryse.md").exists()
    assert (wiki / "long_term/organizations/dnb.md").exists()
    assert not (wiki / "long_term/entities/maryse.md").exists()
    # link rewritten to the new folder
    assert "../people/amaury.md" in (wiki / "long_term/people/maryse.md").read_text()
    idx = (wiki / "long_term/index.md").read_text()
    assert "## people" in idx and "## organizations" in idx and "## entities" not in idx


# --- guarded wrapper ------------------------------------------------------

def test_guarded_reports_missing_key(wiki, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rel, report = dream.run_dry_run()
    assert "ANTHROPIC_API_KEY" in report
    assert rel.startswith("dream_reports/")


def test_guarded_turns_exception_into_report(wiki, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    def boom(when, day):
        raise RuntimeError("kaboom")
    rel, report = runner._guarded(False, boom)
    assert "kaboom" in report  # traceback captured, no raw 500
