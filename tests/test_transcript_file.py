"""The pure half of transcript capture: parsing and append classification.

No `db` marker anywhere in this file, deliberately - nothing here touches
Postgres or the filesystem, so every one of these runs on CI. The properties
tested are the ones that actually break, chosen the way memory_file's were.
"""

from __future__ import annotations

import json

from saddlebag.transcript_file import (
    ReadPlan,
    classify,
    parse,
    sha256_hex,
)


def test_classify_skips_a_file_that_has_not_grown() -> None:
    """The common case at every session start, and it costs one stat."""
    assert classify(100, "abc", 100, None) is ReadPlan.SKIP


def test_classify_appends_when_the_prefix_still_matches() -> None:
    assert classify(100, "abc", 180, "abc") is ReadPlan.APPEND


def test_classify_rebuilds_when_the_prefix_changed() -> None:
    """The file was rewritten, not appended. Safe: lines are derived."""
    assert classify(100, "abc", 180, "def") is ReadPlan.REBUILD


def test_classify_reports_a_shrunk_file_rather_than_following_it() -> None:
    """The one case where the file on disk is NOT followed.

    Our stored copy is more complete, and the purpose of the source row is
    that a rotating file does not destroy the session.
    """
    assert classify(100, "abc", 40, "abc") is ReadPlan.SHRUNK


def test_sha256_hex_is_stable_and_hex() -> None:
    got = sha256_hex(b"hello")
    assert got == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


def test_parse_accounts_for_every_line() -> None:
    """Rows plus failures equals lines. Nothing is ever silently dropped.

    A line whose SHAPE is unrecognised becomes a row with a null type; a line
    that is not valid JSON cannot become a jsonb row at all and is named as a
    failure instead. The conservation is the property.
    """
    content = b"\n".join(
        [
            json.dumps({"type": "user", "uuid": "u1"}).encode(),
            b"this is not json",
            json.dumps({"no_type_field": True}).encode(),
        ]
    )
    lines, failures = parse(content)
    assert len(lines) + len(failures) == 3
    assert [line.seq for line in lines] == [0, 2]
    assert [f.seq for f in failures] == [1]
    assert lines[1].type is None


def test_parse_keeps_the_whole_line_in_raw() -> None:
    payload = {"type": "assistant", "uuid": "x", "message": {"deep": [1, 2]}}
    lines, _ = parse(json.dumps(payload).encode())
    assert lines[0].raw == payload


def test_parse_reads_the_timestamp_when_there_is_one() -> None:
    content = json.dumps(
        {"type": "user", "timestamp": "2026-09-15T12:00:00.000Z"}
    ).encode()
    lines, _ = parse(content)
    assert lines[0].occurred_at is not None
    assert lines[0].occurred_at.year == 2026


def test_parse_tolerates_an_unparseable_timestamp() -> None:
    """A field we hoist for indexing must never fail an import."""
    content = json.dumps({"type": "user", "timestamp": "last tuesday"}).encode()
    lines, failures = parse(content)
    assert failures == []
    assert lines[0].occurred_at is None


def test_parse_ignores_blank_lines_without_counting_them() -> None:
    """A trailing newline is not a line, and must not become a null row."""
    content = b'{"type": "user"}\n\n'
    lines, failures = parse(content)
    assert len(lines) == 1
    assert failures == []


def test_parse_survives_invalid_utf8() -> None:
    """The reason `content` is bytea. One bad byte must cost one line."""
    content = b'{"type": "user"}\n' + b"\xff\xfe not utf-8\n"
    lines, failures = parse(content)
    assert len(lines) == 1
    assert len(failures) == 1


def test_parse_starts_at_the_offset_it_is_given() -> None:
    """Append parses only the tail, and the seq must continue the file."""
    lines, _ = parse(b'{"type": "user"}', start_seq=7)
    assert lines[0].seq == 7


def test_parse_is_idempotent() -> None:
    """Rebuilding the derived table must always produce the same rows.

    Everything downstream rests on this: a label keyed on (transcript_id,
    seq) is only safe if a re-parse puts the same content at the same seq.
    """
    content = b'{"type": "user"}\n{"type": "assistant"}\n'
    first, _ = parse(content)
    second, _ = parse(content)
    assert [(line.seq, line.raw) for line in first] == [
        (line.seq, line.raw) for line in second
    ]


def test_parse_names_a_line_postgres_cannot_hold_as_jsonb() -> None:
    """`\\u0000` is valid JSON and decodes to U+0000, which `jsonb` refuses.

    Found by a real import: the refused batch raised out of the whole run,
    and the zero-lines repair re-parsed that file on every later run, so
    one line stopped the project's imports for good. Its bytes are in the
    stored source either way; the derived row is what cannot exist. A NUL
    in a KEY is refused just the same, and so is one nested in a list.
    """
    content = (
        b'{"type": "user"}\n'
        b'{"type": "user", "text": "a\\u0000b"}\n'
        b'{"type": "user", "a\\u0000": 1}\n'
        b'{"type": "user", "list": [{"deep": ["\\u0000"]}]}\n'
        b'{"type": "user", "text": "a\\\\u0000b"}\n'
    )
    lines, failures = parse(content)
    assert [line.seq for line in lines] == [0, 4]
    assert [f.seq for f in failures] == [1, 2, 3]
    assert all("U+0000" in f.reason for f in failures)
