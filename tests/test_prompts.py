"""Editable prompts: seeding defaults and resetting them."""

from wiki_server import prompts
from wiki_server.dream.config import DEFAULT_DREAM_MD, DREAM_POLICY


def test_ensure_prompt_seeds_default(wiki):
    out = prompts.ensure_prompt("triage")
    assert out == prompts.TRIAGE_DEFAULT
    assert (wiki / "prompts/triage.md").read_text(encoding="utf-8") == prompts.TRIAGE_DEFAULT


def test_reset_prompts_restores_all_defaults(wiki, write):
    # owner has edited the policy and every stage prompt
    write(DREAM_POLICY, "# edited policy\n")
    for rel in prompts.PROMPT_FILES.values():
        write(rel, "# edited\n")

    prompts.reset_prompts()

    assert (wiki / DREAM_POLICY).read_text(encoding="utf-8") == DEFAULT_DREAM_MD
    for stage, rel in prompts.PROMPT_FILES.items():
        assert (wiki / rel).read_text(encoding="utf-8") == prompts.DEFAULTS[stage]


def test_reset_prompts_creates_missing_files(wiki):
    # nothing on disk yet: reset writes the full set
    prompts.reset_prompts()
    assert (wiki / DREAM_POLICY).is_file()
    for rel in prompts.PROMPT_FILES.values():
        assert (wiki / rel).is_file()
