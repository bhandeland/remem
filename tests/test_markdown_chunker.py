"""The chunker is pure - no store, no I/O - so these tests carry no db
marker and run everywhere. That split is deliberate: this repo has twice
been bitten by markers that hid a test on CI."""

from __future__ import annotations

from remem.markdown import MAX_BODY, Chunk, slugify, split


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Invariants worth not breaking") == "invariants-worth-not-breaking"
    assert slugify("`--archive` is a flag, not a path heuristic") == "archive-is-a-flag-not-a-path-heuristic"
    assert slugify("Two   spaces") == "two-spaces"


def test_the_first_chunk_is_the_anchor_and_carries_the_lead_paragraph():
    text = "# Ingest design\n\nDesign, 2026-09-01.\n\n## Problem\n\nnone of it is in remem\n"
    chunks = split(text, doc_name="ingest design")

    assert chunks[0].anchor is True
    assert chunks[0].slug == ""
    assert chunks[0].title == "Ingest design"
    assert "Design, 2026-09-01." in chunks[0].body
    assert "none of it is in remem" not in chunks[0].body


def test_one_chunk_per_heading_titled_with_the_document():
    text = "# Doc\n\nlead\n\n## Alpha\n\nbody a\n\n## Beta\n\nbody b\n"
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "alpha", "beta"]
    assert chunks[1].title == "Doc § Alpha"
    assert chunks[1].body.strip() == "## Alpha\n\nbody a"


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    text = (
        "# Doc\n\nlead\n\n## Alpha\n\n"
        "```python\n"
        "# this is a comment, not a heading\n"
        "x = 1\n"
        "```\n\n"
        "still alpha\n"
    )
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "alpha"]
    assert "still alpha" in chunks[1].body


def test_headings_deeper_than_h3_stay_inside_their_section():
    text = "# Doc\n\nlead\n\n## Alpha\n\n#### Deep\n\ndeep body\n"
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "alpha"]
    assert "#### Deep" in chunks[1].body


def test_a_file_with_no_headings_is_one_chunk_plus_the_anchor():
    chunks = split("just prose, no headings at all\n", doc_name="notes")

    assert [c.slug for c in chunks] == ["", "notes"]
    assert chunks[1].body.strip() == "just prose, no headings at all"


def test_duplicate_headings_get_distinct_slugs():
    text = "# Doc\n\nlead\n\n## Notes\n\nfirst\n\n## Notes\n\nsecond\n"
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "notes", "notes-2"]


def test_an_oversized_body_is_truncated_not_split():
    text = "# Doc\n\nlead\n\n## Big\n\n" + ("x" * (MAX_BODY * 2)) + "\n"
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "big"]
    assert len(chunks[1].body.encode()) <= MAX_BODY
    assert chunks[1].body.endswith("[truncated]")


def test_chunks_are_frozen():
    chunk = Chunk(slug="a", title="t", body="b", anchor=False)
    try:
        chunk.slug = "b"
    except AttributeError:
        return
    raise AssertionError("Chunk must be frozen")


def test_the_title_comes_from_the_h1_not_the_document_name():
    text = "# Ingest design\n\nlead\n\n## Alpha\n\nbody a\n"
    chunks = split(text, doc_name="2026-09-01-doc-ingest-design")

    assert chunks[0].title == "Ingest design"
    assert chunks[1].title == "Ingest design § Alpha"


def test_the_document_name_titles_a_file_with_no_h1():
    text = "lead\n\n## Alpha\n\nbody a\n"
    chunks = split(text, doc_name="notes")

    assert chunks[0].title == "notes"
    assert chunks[1].title == "notes § Alpha"


def test_an_h1_below_the_lead_is_a_section_not_the_document_title():
    # Only an h1 that opens the file names the document. One appearing after
    # a section has begun is an ordinary heading, and stealing the title from
    # it would rename the whole document halfway down.
    text = "# Doc\n\nlead\n\n## Alpha\n\nbody a\n\n# Later\n\nbody l\n"
    chunks = split(text, doc_name="doc")

    assert [c.slug for c in chunks] == ["", "alpha", "later"]
    assert chunks[2].title == "Doc § Later"


def test_an_h1_inside_a_code_fence_does_not_title_the_document():
    text = "```\n# not a heading\n```\n\nlead\n\n## Alpha\n\nbody a\n"
    chunks = split(text, doc_name="notes")

    assert chunks[0].title == "notes"


def test_the_headingless_chunk_keeps_its_slug_when_the_h1_supplies_the_title():
    # Identity is the (src, sec) pair, and here sec is the document name.
    # Titling from the h1 must not move it, or every headingless file would
    # duplicate on the next ingest instead of matching.
    chunks = split("# A Long Written Title\n\njust prose\n", doc_name="notes")

    assert [c.slug for c in chunks] == ["", "notes"]
    assert chunks[1].title == "A Long Written Title"
