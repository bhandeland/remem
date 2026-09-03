"""Parsing and rendering Claude Code memory files. Pure - no I/O, no store.

Kept out of services/ for the same reason markdown.py and session_size.py
are: a format bug should be caught on CI, and a module that touches no
database has tests that carry no `db` marker and therefore run there.

The round-trip is load-bearing rather than cosmetic, but whole-file byte
equality is *not* the property that matters - a real fixture proved that
(a `metadata:` line with a trailing space that this module has no reason to
reproduce). What the sync actually needs is: the body round-trips exactly
(the watermark hashes the body alone, deliberately excluding frontmatter,
so YAML whitespace can never read as a content change), render() is
idempotent (a file gets normalised once, on first write, and is stable
after that), and no metadata key parse() sees is ever dropped by render() -
Claude Code's own bookkeeping (`originSessionId`, `modified`, ...) lives in
these files too, and this module does not own it well enough to discard it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: The link/hook separator in MEMORY.md. An em dash, against this repo's
#: spaced-hyphen convention, because this line's format belongs to Claude
#: Code and matching the corpus already on disk matters more than matching
#: remem's prose style. The only place in the repo where that is true.
INDEX_SEPARATOR = " — "

_INDEX_LINE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<file>[^)]+)\)")


class MalformedMemoryFile(Exception):
    """A file that does not have the frontmatter a memory file must have."""


@dataclass(slots=True, frozen=True)
class MemoryFile:
    #: The frontmatter `name:` slug, and what `[[wiki-links]]` resolve
    #: against. Not identity: the sync keys on the filename stem, and this
    #: field is carried through a regenerate rather than rewritten, because
    #: it is the user's. parse() falls back to the stem when a file has no
    #: `name:` at all.
    name: str
    #: The MEMORY.md link text. Lives in the index, not in this file, which
    #: is why parse() takes it as an argument.
    title: str
    #: The frontmatter `description:`.
    description: str
    type: str | None
    body: str
    #: Metadata keys remem does not own, preserved verbatim and in source
    #: order. Real memory files carry Claude Code's own bookkeeping here -
    #: node_type, originSessionId, modified - and a renderer that dropped
    #: them would destroy provenance on a file remem did not write.
    extra: dict[str, str] = field(default_factory=dict)


def body_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def title_from_name(name: str) -> str:
    """A placeholder title for a stray file the index does not list yet.

    Deliberately mechanical and a little ugly, so the next MEMORY.md line
    written for it reads as an improvement rather than a conflict.
    """
    words = name.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else name


def parse_index(text: str) -> dict[str, str]:
    """MEMORY.md filename -> link text. Lines it cannot read are skipped.

    Skipping rather than raising: the index is the file a human is most
    likely to have hand-edited, and one malformed line must not cost the
    titles of every other memory.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _INDEX_LINE.match(line.strip())
        if m:
            out[m.group("file")] = m.group("title")
    return out


def index_lines(text: str) -> dict[str, str]:
    """MEMORY.md filename -> the whole line, verbatim.

    parse_index() keeps only the link text; this keeps the hook after the
    separator too, which is what lets a line be carried through unchanged for
    a file remem could not parse and therefore has no entry to rebuild from.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        m = _INDEX_LINE.match(stripped)
        if m:
            out[m.group("file")] = stripped
    return out


def parse(text: str, *, name: str, title: str) -> MemoryFile:
    """One file plus its index title. Never a file in isolation.

    `name` is the filename stem and that is what identity is: the sync keys
    on it, MEMORY.md links to it, and the `mem:<name>` tag records it. The
    frontmatter `name:` is kept on the returned MemoryFile - it is the user's
    field and a regenerate writes it back unchanged - but it does not decide
    which entry this file is. A file renamed on disk therefore mints a new
    entry and the old name is regenerated from its entry; following a rename
    would need identity that survives one, which this does not have.
    """
    if not text.startswith("---\n"):
        raise MalformedMemoryFile("no frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise MalformedMemoryFile("unterminated frontmatter")
    head = text[4:end + 1]
    body = text[end + len("\n---\n"):]

    fields: dict[str, str] = {}
    # Metadata keys other than `type`, in the order they appeared. remem
    # does not know what these mean - they are often Claude Code's own
    # bookkeeping - so they are carried through opaquely rather than
    # interpreted.
    extra: dict[str, str] = {}
    in_metadata = False
    for line in head.splitlines():
        if not line.strip():
            continue
        if line.startswith("metadata:"):
            in_metadata = True
            continue
        key, _, value = line.strip().partition(":")
        key = key.strip()
        value = value.strip()
        if in_metadata and line.startswith(" "):
            if key == "type":
                fields["metadata.type"] = value
            else:
                extra[key] = value
        else:
            in_metadata = False
            fields[key] = value

    return MemoryFile(
        name=fields.get("name") or name,
        title=title or title_from_name(fields.get("name") or name),
        description=fields.get("description", ""),
        type=fields.get("metadata.type"),
        body=body,
        extra=extra,
    )


def render(mf: MemoryFile) -> str:
    """Inverse of parse() for the properties that matter. See module docstring.

    `type` is always emitted first among the metadata keys (when present),
    with `extra` following in its stored order. This does not reproduce a
    source file's original key order byte for byte - the fifth fixture's
    `metadata: ` trailing space proved that isn't the right target anyway -
    but it is a deterministic function of (type, extra), which is what
    idempotence actually requires: re-parsing rendered output must yield
    the same (type, extra) it started from, so a second render is a no-op.
    """
    lines = [
        "---",
        f"name: {mf.name}",
        f"description: {mf.description}",
    ]
    if mf.type is not None or mf.extra:
        lines.append("metadata:")
        if mf.type is not None:
            lines.append(f"  type: {mf.type}")
        lines += [f"  {key}: {value}" for key, value in mf.extra.items()]
    lines += ["---"]
    # `body` (as produced by parse()) already carries the blank line that
    # separates it from the closing fence as its own leading "\n" - so this
    # is the *only* newline render adds between "---" and the body, not a
    # second one stacked on top of parse()'s. Two "\n"s here would print an
    # extra blank line on every file, forever.
    return "\n".join(lines) + "\n" + mf.body


def render_index(
    files: list[MemoryFile], carried: dict[str, str] | None = None
) -> str:
    """Sorted by filename. Any deterministic order would do; the requirement
    is only that it not depend on iteration order, because an index that
    reshuffles itself makes every sync look like a change.

    `carried` maps a filename to a line to emit verbatim - for files the
    caller has on disk but cannot rebuild a line for. They sort in among the
    generated ones rather than being appended, so a file that becomes
    unparseable does not also jump to the bottom of the index.
    """
    rows = [
        (f"{mf.name}.md",
         f"- [{mf.title}]({mf.name}.md){INDEX_SEPARATOR}{mf.description}")
        for mf in files
    ]
    rows += list((carried or {}).items())
    return "".join(f"{line}\n" for _, line in sorted(rows))
