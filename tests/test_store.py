"""Storage helpers: slugs, frontmatter, STM writing, batch writes/deletes."""

from wiki_server.store import (
    apply_changes,
    delete_file,
    fm_value,
    parse_frontmatter,
    slugify,
    stm_index_content,
    unique_stem,
    write_file,
    write_stm_entry,
)


def test_slugify():
    assert slugify("Voyage à Venise !") == "voyage-a-venise"
    assert slugify("") == "note"
    assert slugify("é", maxlen=3) == "e"


def test_unique_stem_avoids_collision(wiki, write):
    write("long_term/people/maryse.md", "x")
    assert unique_stem("long_term/people", "maryse") == "maryse-2"
    assert unique_stem("long_term/people", "maryse", taken={"maryse-2"}) == "maryse-3"


def test_fm_value_strips_newlines():
    assert fm_value("a\nb\r\nc") == "a b  c"


def test_parse_frontmatter_roundtrip():
    meta, body = parse_frontmatter("---\ntype: event\ndue: 2026-06-30\n---\n\nHello\n")
    assert meta == {"type": "event", "due": "2026-06-30"}
    assert body == "Hello\n"


def test_parse_frontmatter_none():
    meta, body = parse_frontmatter("Just text\n")
    assert meta == {} and body == "Just text\n"


def test_write_stm_entry(wiki):
    name, created = write_stm_entry("Vu Maryse", summary="Maryse", tags=["famille"])
    entry = wiki / "short_term/entries" / f"{name}.md"
    assert entry.is_file()
    assert "Vu Maryse" in entry.read_text()
    index = (wiki / "short_term/index.md").read_text()
    assert name in index and "famille" in index


def test_write_stm_entry_tag_sanitized(wiki):
    name, _ = write_stm_entry("x", tags=["a|b\nc"])
    index = (wiki / "short_term/index.md").read_text()
    # No raw pipe/newline from a tag should break the markdown table.
    assert "a/b c" in index


def test_stm_index_content_excludes(wiki):
    a, _ = write_stm_entry("first")
    b, _ = write_stm_entry("second")
    rebuilt = stm_index_content(exclude_stems={a})
    assert b in rebuilt and a not in rebuilt


def test_apply_changes_writes_and_deletes(wiki, write):
    write("long_term/old.md", "old")
    apply_changes({"long_term/new.md": "new"}, ["long_term/old.md"], "msg")
    assert (wiki / "long_term/new.md").read_text() == "new"
    assert not (wiki / "long_term/old.md").exists()


def test_write_file_returns_path(wiki):
    p = write_file("long_term/x.md", "hi", "msg")
    assert p == wiki / "long_term/x.md" and p.read_text() == "hi"


def test_delete_file(wiki, write):
    write("long_term/x.md", "hi")
    assert delete_file("long_term/x.md", "msg") is True
    assert delete_file("long_term/missing.md", "msg") is False
