from __future__ import annotations

from pathlib import Path

import pytest

from remem import memory_file

FIXTURES = Path(__file__).parent / "fixtures" / "memory"


def _fact_files() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.md") if p.name != "MEMORY.md")


def test_the_fixture_corpus_is_actually_there():
    # A glob that silently matched nothing would make every test below pass
    # vacuously, which is the failure mode this whole file exists to avoid.
    assert len(_fact_files()) >= 5


# A whole-file byte-equality test stood here first. It failed on
# remem-path-shadows-uv-tool-install.md, which carries Claude Code's own
# bookkeeping metadata (node_type, originSessionId, modified) and a
# trailing space after `metadata:` that no other fixture has. Chasing
# whole-file equality would have meant reproducing that trailing space
# byte for byte forever, which is not a property remem's sync needs -
# it needs the *body* to round-trip exactly (that is what the watermark
# hashes), rendering to be idempotent (a file is normalised once and then
# stable), and no metadata key to ever be silently dropped. The three
# tests below assert exactly those three things instead, and that fixture
# is what proves they are the right ones: it fails whole-file equality by
# construction and passes all three.
def _parse_fixture(path: Path) -> memory_file.MemoryFile:
    text = path.read_text()
    index = memory_file.parse_index((FIXTURES / "MEMORY.md").read_text())
    return memory_file.parse(
        text,
        name=path.stem,
        title=index.get(path.name, ""),
    )


@pytest.mark.parametrize("path", _fact_files(), ids=lambda p: p.name)
def test_body_round_trips_byte_for_byte(path):
    # The sync's watermark hashes the body alone (frontmatter is
    # deliberately excluded, so YAML whitespace can never read as a
    # content change) - so this is the property that actually has to hold.
    mf = _parse_fixture(path)
    rendered = memory_file.render(mf)
    reparsed = memory_file.parse(rendered, name=mf.name, title=mf.title)
    assert reparsed.body == mf.body


@pytest.mark.parametrize("path", _fact_files(), ids=lambda p: p.name)
def test_render_is_idempotent(path):
    # A file with non-canonical frontmatter (key order, a stray trailing
    # space) is normalised once, on first write, and must be stable after
    # that - otherwise every sync run would see a "change" forever.
    mf = _parse_fixture(path)
    once = memory_file.render(mf)
    twice = memory_file.render(memory_file.parse(once, name=mf.name, title=mf.title))
    assert once == twice


@pytest.mark.parametrize("path", _fact_files(), ids=lambda p: p.name)
def test_no_metadata_key_is_lost(path):
    text = path.read_text()
    mf = _parse_fixture(path)
    rendered = memory_file.render(mf)
    in_metadata = False
    for line in text.splitlines():
        if line.startswith("metadata:"):
            in_metadata = True
            continue
        if in_metadata and line.startswith(" ") and line.strip():
            key = line.strip().partition(":")[0]
            assert f"{key}:" in rendered
        elif in_metadata and not line.startswith(" "):
            in_metadata = False


def test_parse_reads_every_field():
    text = (FIXTURES / "cursor-runs-claude-code-hooks.md").read_text()
    mf = memory_file.parse(
        text,
        name="cursor-runs-claude-code-hooks",
        title="Cursor runs hooks",
    )
    assert mf.name == "cursor-runs-claude-code-hooks"
    assert mf.title == "Cursor runs hooks"
    assert mf.type == "project"
    assert mf.description.startswith("Cursor 3.x loads")
    assert "Cursor 3.x reads" in mf.body


def test_parse_rejects_a_file_with_no_frontmatter():
    with pytest.raises(memory_file.MalformedMemoryFile):
        memory_file.parse("just a body", name="x", title="X")


def test_parse_index_maps_filename_to_link_text():
    index = memory_file.parse_index((FIXTURES / "MEMORY.md").read_text())
    assert index["cursor-runs-claude-code-hooks.md"] == (
        "Cursor runs Claude Code's hooks"
    )


def test_render_index_is_sorted_by_name_and_uses_the_em_dash():
    files = [
        memory_file.MemoryFile(
            name="b",
            title="B",
            description="second",
            type=None,
            body="x",
        ),
        memory_file.MemoryFile(
            name="a",
            title="A",
            description="first",
            type=None,
            body="x",
        ),
    ]
    assert memory_file.render_index(files) == (
        "- [A](a.md) — first\n- [B](b.md) — second\n"
    )


def test_title_from_name_is_mechanical():
    assert memory_file.title_from_name("cursor-runs-claude-code-hooks") == (
        "Cursor runs claude code hooks"
    )


def test_body_sha_ignores_nothing_and_is_stable():
    assert memory_file.body_sha("a") == memory_file.body_sha("a")
    assert memory_file.body_sha("a") != memory_file.body_sha("a\n")


# --- the flat frontmatter dialect -------------------------------------
# Claude Code writes memory files in two shapes, and only one of them was
# known when parse() was written. Sixteen real files across two project
# directories put `type:` and their bookkeeping keys at the top level of
# the frontmatter with no `metadata:` line at all. Read as though the
# indented form were the only one, every such key lands in the "fields
# remem understands" bucket, is understood by nothing, and is dropped by
# the next render - taking the file's type and its provenance with it.
def test_a_top_level_type_is_the_type():
    mf = _parse_fixture(FIXTURES / "flat-frontmatter-dialect.md")
    assert mf.type == "reference"


def test_a_top_level_bookkeeping_key_survives_a_render():
    mf = _parse_fixture(FIXTURES / "flat-frontmatter-dialect.md")
    assert mf.extra.get("originSessionId") == ("00000000-0000-0000-0000-000000000000")
    assert "originSessionId:" in memory_file.render(mf)


def test_the_two_dialects_parse_to_the_same_shape():
    # The point of the fix: which dialect a file happens to be written in
    # must not be observable downstream of parse().
    flat = _parse_fixture(FIXTURES / "flat-frontmatter-dialect.md")
    nested = memory_file.parse(
        memory_file.render(flat),
        name=flat.name,
        title=flat.title,
    )
    assert (nested.type, nested.extra, nested.body) == (
        flat.type,
        flat.extra,
        flat.body,
    )
