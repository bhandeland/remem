"""Splitting a markdown document into chunk-sized pieces.

Pure: handed text, returns chunks. No file I/O, no store, no config - which
is what lets its tests run without Postgres.

Splitting is on headings and ONLY on headings. The obvious alternative, a
size-based sub-splitter for long sections, was rejected in the design: a
plan section is mostly fenced code, so a size split cuts through the middle
of a fence and yields a chunk that starts mid-function with an unterminated
backtick. A 15KB section that is one coherent unit is better than four
incoherent ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Bodies larger than this are truncated with a pointer, never split. Nothing
#: in the corpus this was written for comes close; the cap exists so a future
#: document without headings cannot write an unbounded body.
MAX_BODY = 32 * 1024

_TRUNCATED = "\n\n[truncated]"

#: h1-h3 only. Deeper headings stay inside their section: h4 in this corpus
#: marks a sub-point of an argument, not a separate one.
_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")

#: A fence opener or closer. Tracking these is the ONLY fence logic in the
#: design, and it exists for heading DETECTION - a '# comment' on the first
#: line of a Python block is not a section - not for fence-aware splitting.
_FENCE = re.compile(r"^\s*(```|~~~)")


def slugify(text: str) -> str:
    """A heading turned into the stable half of a chunk's identity.

    Punctuation is dropped rather than encoded: `sec:` tags are read by
    people in search output, and a heading's backticks and commas carry no
    identity that its words do not.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return cleaned.strip("-")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One entry's worth of a document.

    Frozen because a chunk is a reading of a file at a moment. Ingest
    compares chunks against stored bodies and must never be able to edit one
    en route.
    """

    slug: str
    title: str
    body: str
    anchor: bool = False


def _capped(body: str) -> str:
    """Truncate on a byte budget, never mid-character."""
    encoded = body.encode()
    if len(encoded) <= MAX_BODY:
        return body
    budget = MAX_BODY - len(_TRUNCATED.encode())
    return encoded[:budget].decode(errors="ignore") + _TRUNCATED


def split(text: str, *, doc_name: str) -> list[Chunk]:
    """Anchor chunk first, then one chunk per h1-h3 heading, in order.

    The anchor is the document itself: its lead matter, everything before the
    first heading that is not the title. It is not merely a nicety - the
    sweep in services/ingest.py supersedes vanished chunks BY the anchor,
    because set_superseded needs a replacement id and a deleted heading has
    none.

    `doc_name` is what the caller calls the document - the filename stem -
    and it does two jobs that must not be confused, the same distinction
    `root` draws in services/ingest.ingest_file. It IDENTIFIES the document
    (a headingless file's slug is its name, and slugs are half of the
    (src, sec) identity), and it NAMES it only when the file has no opening
    h1. A file that has one is titled by its h1, because "Ingest design §
    Decisions" is what a person reading search output needs and
    "2026-09-01-doc-ingest-design § Decisions" is what they were getting.
    Identity never moves with the title, so retitling a document does not
    duplicate its chunks.
    """
    lines = text.splitlines()
    doc_title = doc_name
    fenced = False
    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None

    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is None:
            (current if current is not None else lead).append(line)
            continue
        level, title = len(heading.group(1)), heading.group(2)
        if level == 1 and not sections and current is None:
            # The document's own h1 titles the anchor rather than opening a
            # section. Only an h1 that opens the file counts: one appearing
            # after a section has begun is an ordinary heading, and taking
            # the title from it would rename the document halfway down.
            doc_title = title
            continue
        current = [line]
        sections.append((title, current))

    chunks = [
        Chunk(
            slug="", title=doc_title, body=_capped("\n".join(lead).strip()), anchor=True
        )
    ]

    if not sections:
        # A file with no headings is still worth one searchable chunk. Its
        # slug is the document, so re-ingest matches it like any other.
        return chunks + [
            Chunk(
                slug=slugify(doc_name),
                title=doc_title,
                body=_capped("\n".join(lead).strip()),
            )
        ]

    seen: dict[str, int] = {}
    for title, body_lines in sections:
        slug = slugify(title)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            # Two headings with the same words are two chunks, and identity
            # is (src, sec) - so they need distinct slugs or the second
            # would supersede the first on every single ingest.
            slug = f"{slug}-{seen[slug]}"
        chunks.append(
            Chunk(
                slug=slug,
                title=f"{doc_title} § {title}",
                body=_capped("\n".join(body_lines).strip()),
            )
        )
    return chunks
