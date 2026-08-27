from datetime import UTC, datetime, timedelta

import pytest

from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import Kind, Origin
from remem.services import handoff

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn):
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


BODY = "## Done\nshipped it\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


def test_a_handoff_is_a_doc_entry_tagged_with_its_topic(store, owner):
    entry, superseded = handoff.write(
        store, owner.id, project="remem", topic="gitlab-ci", body=BODY,
        today=datetime(2026, 8, 27, tzinfo=UTC).date(),
    )
    assert entry.kind == Kind.DOC
    assert entry.origin == Origin.HANDOFF
    assert entry.project == "remem"
    assert entry.tags == ["topic:gitlab-ci"]
    assert entry.title == "Handoff: gitlab-ci (2026-08-27)"
    assert superseded is None


def test_the_topic_defaults_to_the_project(store, owner):
    entry, _ = handoff.write(store, owner.id, project="remem", body=BODY)
    assert entry.tags == ["topic:remem"]


def test_writing_a_handoff_supersedes_the_previous_one_for_that_topic(store, owner):
    first, _ = handoff.write(store, owner.id, project="remem",
                             topic="ci", body=BODY)
    second, superseded = handoff.write(store, owner.id, project="remem",
                                       topic="ci", body=BODY)
    assert superseded is not None and superseded.id == first.id
    assert store.get_entry(first.id, owner.id).superseded_by == second.id
    assert handoff.latest(store, owner.id, project="remem", topic="ci").id == second.id


def test_another_topic_is_left_alone(store, owner):
    other, _ = handoff.write(store, owner.id, project="remem",
                             topic="search", body=BODY)
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    assert store.get_entry(other.id, owner.id).superseded_by is None


def test_another_project_is_left_alone(store, owner):
    other, _ = handoff.write(store, owner.id, project="elsewhere",
                             topic="ci", body=BODY)
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    assert store.get_entry(other.id, owner.id).superseded_by is None


def test_latest_without_a_topic_returns_the_newest_for_the_project(store, owner):
    handoff.write(store, owner.id, project="remem", topic="ci", body=BODY)
    newest, _ = handoff.write(store, owner.id, project="remem",
                              topic="search", body=BODY)
    assert handoff.latest(store, owner.id, project="remem").id == newest.id


def test_latest_is_none_when_nothing_was_handed_off(store, owner):
    assert handoff.latest(store, owner.id, project="remem") is None


def test_a_handoff_without_a_project_is_rejected(store, owner):
    with pytest.raises(handoff.NoProject):
        handoff.write(store, owner.id, project=None, topic="ci", body=BODY)


def test_an_empty_body_is_rejected(store, owner):
    with pytest.raises(ValueError):
        handoff.write(store, owner.id, project="remem", body="   ")


def test_slugify_makes_a_tag_safe_topic():
    assert handoff.slugify("GitLab CI / runners") == "gitlab-ci-runners"
    assert handoff.slugify("  spaced  out  ") == "spaced-out"


def test_topic_of_reads_the_tag_back(store, owner):
    entry, _ = handoff.write(store, owner.id, project="remem",
                             topic="ci", body=BODY)
    assert handoff.topic_of(entry) == "ci"


def test_age_phrase_is_terse():
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    assert handoff.age_phrase(now - timedelta(seconds=30), now) == "just now"
    assert handoff.age_phrase(now - timedelta(minutes=9), now) == "9m ago"
    assert handoff.age_phrase(now - timedelta(hours=2), now) == "2h ago"
    assert handoff.age_phrase(now - timedelta(days=3), now) == "3d ago"
