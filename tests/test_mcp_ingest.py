from __future__ import annotations

from pathlib import Path

import pytest

from saddlebag.backends.postgres.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


def test_recall_hides_archived_chunks_unless_asked(env: str, tmp_path: Path) -> None:
    from saddlebag.mcp_server import recall_tool
    from saddlebag.services import ingest
    from saddlebag.session import open_session

    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n\nlead\n\n## Task 9\n\nsession wiring and the CLI\n")
    with open_session() as s:
        ingest.ingest_file(s.store, s.owner.id, doc, project="saddlebag", archive=True)

    assert recall_tool(query="session wiring") == []

    shown = recall_tool(query="session wiring", include_archived=True)
    assert isinstance(shown, list)
    assert [h["title"] for h in shown] == ["Plan § Task 9"]
