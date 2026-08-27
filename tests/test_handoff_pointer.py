import json

import pytest

from remem.agents.claude_code import hook
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.domain import CollectionQuery
from remem.services import handoff, kb, write

pytestmark = pytest.mark.db

BODY = "## Done\nx\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


@pytest.fixture
def live(live_dsn, monkeypatch, tmp_path):
    import psycopg
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {"REMEM_DSN": live_dsn, "REMEM_USER_ID": "brandon",
            "REMEM_CONFIG": str(tmp_path / "none.toml")}


def _seed(dsn, *, with_kb, with_handoff, project="remem"):
    import psycopg
    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        if with_kb:
            kb.create(store, owner.id, slug=project, title=project,
                      query=CollectionQuery(project=project))
            write.remember(store, owner.id, title="A note", body="body",
                           project=project)
        if with_handoff:
            handoff.write(store, owner.id, project=project, topic="ci",
                          body=BODY)
        c.commit()


def _payload(cwd):
    return json.dumps({"cwd": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    """A real git repository, since the project comes from git, not the dir."""
    import subprocess
    d = tmp_path / "remem"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


def test_the_pointer_is_appended_to_the_context_block(live, repo):
    _seed(live["REMEM_DSN"], with_kb=True, with_handoff=True)
    out = hook.session_start(_payload(repo), env=live)
    assert "A note" in out
    assert "Handoff available: ci" in out
    assert "remem-prime ci" in out


def test_the_pointer_appears_with_no_knowledge_base_at_all(live, repo):
    _seed(live["REMEM_DSN"], with_kb=False, with_handoff=True)
    out = hook.session_start(_payload(repo), env=live)
    assert "Handoff available: ci" in out


def test_no_handoff_means_no_pointer(live, repo):
    _seed(live["REMEM_DSN"], with_kb=True, with_handoff=False)
    out = hook.session_start(_payload(repo), env=live)
    assert "Handoff available" not in out


def test_nothing_at_all_still_returns_empty(live, repo):
    _seed(live["REMEM_DSN"], with_kb=False, with_handoff=False)
    assert hook.session_start(_payload(repo), env=live) == ""
