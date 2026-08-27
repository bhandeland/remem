"""Session handoffs: one live entry per (project, topic).

Every decision about handoffs lives here. The CLI only formats and the hook
only asks whether one exists.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from uuid import UUID

from remem.domain import Entry, Kind, Origin, Query
from remem.services.write import remember
from remem.store import Store

TOPIC_PREFIX = "topic:"

#: The fixed shape of a handoff body. Fixed rather than free-form so prime
#: knows what it is reading and a human can scan one without reading it.
SECTIONS = ("Done", "In flight", "Next steps", "Gotchas")

BLANK_BODY = "\n\n".join(f"## {s}\n" for s in SECTIONS)


class NoProject(Exception):
    """A handoff was written with no project to file it under.

    Rejected rather than stored: neither the SessionStart pointer nor
    `handoff latest` could ever resolve it, so it would be written and then
    never seen again.
    """


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def topic_tag(topic: str) -> str:
    return f"{TOPIC_PREFIX}{slugify(topic)}"


def topic_of(entry: Entry) -> str | None:
    for tag in entry.tags:
        if tag.startswith(TOPIC_PREFIX):
            return tag[len(TOPIC_PREFIX):]
    return None


def latest(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    topic: str | None = None,
) -> Entry | None:
    """The newest live handoff for a project, optionally for one topic.

    `topic is None` means "any topic" and is a deliberately wide query. A
    topic that is given but slugs to nothing (all punctuation, say) is a
    different case and must not collapse into the same wide query - doing so
    would return, and thus present, an unrelated topic's handoff as this
    topic's. Reject it instead of silently dropping the tag filter.
    """
    tags = []
    if topic is not None:
        slug = slugify(topic)
        if not slug:
            raise ValueError(f"topic {topic!r} has no slug-able characters")
        tags = [topic_tag(slug)]
    hits = store.search(
        Query(
            project=project,
            tags=tags,
            origins=[Origin.HANDOFF],
            limit=1,
        ),
        owner_id,
    )
    return hits[0].entry if hits else None


def write(
    store: Store,
    owner_id: UUID,
    *,
    project: str | None,
    body: str,
    topic: str | None = None,
    session_id: str | None = None,
    today: date | None = None,
) -> tuple[Entry, Entry | None]:
    """Store a handoff, superseding the previous one for the same topic.

    Returns the new entry and the one it replaced, if any. Superseding is what
    makes volume tolerable: a topic handed off fifty times still costs one live
    entry, because every read filters `superseded_by is null`.
    """
    if not project:
        raise NoProject("a handoff needs a project; run it inside a repository")
    if not body.strip() or body.strip() == BLANK_BODY.strip():
        # The second case is `--edit` closed without writing anything: the
        # editor was seeded with BLANK_BODY (non-empty, so the naive "is it
        # empty" check misses it), and saving it unchanged stores four empty
        # headings as if they were a real handoff.
        raise ValueError("a handoff needs a body")

    slug = slugify(topic or project)
    if not slug:
        # Name whichever one it actually was - project stands in for topic
        # when none is given, and the error should not blame "topic" for a
        # project name that has no slug-able characters.
        label = "topic" if topic else "project"
        raise ValueError(f"{label} {topic or project!r} has no slug-able characters")
    previous = latest(store, owner_id, project=project, topic=slug)

    day = today or datetime.now(tz=UTC).date()
    entry = remember(
        store,
        owner_id,
        title=f"Handoff: {slug} ({day.isoformat()})",
        body=body,
        kind=Kind.DOC,
        project=project,
        tags=[topic_tag(slug)],
        agent="claude-code",
        session_id=session_id,
        origin=Origin.HANDOFF,
    )

    # After the insert, never before: set_superseded requires the replacement
    # row to exist for this owner.
    if previous is not None:
        store.set_superseded(previous.id, entry.id, owner_id)
    return entry, previous


def age_phrase(created_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - created_at).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"
