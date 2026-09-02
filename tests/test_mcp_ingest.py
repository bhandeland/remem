from __future__ import annotations

import pytest

from remem.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    import psycopg
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_recall_hides_archived_chunks_unless_asked(env, tmp_path):
    from remem.mcp_server import recall_tool
    from remem.services import ingest
    from remem.session import open_session

    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n\nlead\n\n## Task 9\n\nsession wiring and the CLI\n")
    with open_session() as s:
        ingest.ingest_file(s.store, s.owner.id, doc, project="remem", archive=True)

    assert recall_tool(query="session wiring") == []

    shown = recall_tool(query="session wiring", include_archived=True)
    assert [h["title"] for h in shown] == ["plan § Task 9"]
