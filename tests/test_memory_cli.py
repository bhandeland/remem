"""CLI tests for `remem memory designate|sync|status`.

Same bootstrap as tests/test_ingest_cli.py: these commands open their own
session, so the schema has to be committed before the CLI connects, and the
DSN and config path are threaded through as environment variables rather
than a fixture object the CLI can see directly.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from remem import memory_file
from remem.agents.claude_code.memory import slug_for
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.cli import app
from remem.domain import CollectionQuery, Kind, Origin
from remem.project import resolve_project
from remem.services import kb, memory
from remem.services.write import remember

pytestmark = pytest.mark.db

runner = CliRunner()

#: The `env` fixture resolves `--project`-less commands the same way the CLI
#: does: from the real working directory, via git. Computed once so the
#: fixtures that seed a collection and the CLI invocation neither can
#: disagree about which project they mean.
PROJECT = resolve_project() or "remem"


@pytest.fixture
def env(live_dsn, tmp_path):
    """CliRunner's `env=` merges into os.environ for the invoke call, which
    is what lets `_memory_dir`'s `Path.cwd()` call see CLAUDE_CONFIG_DIR
    pointed at tmp_path - so this is returned as a dict rather than set via
    monkeypatch, unlike test_ingest_cli.py's `env`, which never needs the
    claude-code adapter to resolve anything."""
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "REMEM_DSN": live_dsn,
        "REMEM_USER_ID": "brandon",
        "REMEM_CONFIG": str(tmp_path / "none.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }


def _designated_store(env) -> tuple[PostgresStore, psycopg.Connection]:
    """A connection left open and committed, with `proj-memory` designated
    for PROJECT. The caller owns closing it; these fixtures return the
    directory, not the connection, so there is nothing left to close."""
    conn = psycopg.connect(env["REMEM_DSN"])
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    kb.create(
        store, owner.id, slug="proj-memory", title="Memory",
        project=PROJECT, query=CollectionQuery(project=PROJECT),
    )
    memory.designate(store, owner.id, PROJECT, "proj-memory")
    return store, conn


def _memory_directory(env) -> Path:
    return (
        Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / slug_for(Path.cwd())
        / "memory"
    )


@pytest.fixture
def memory_dir_with_one_stray(env):
    """A designated project whose directory holds one file remem has never
    seen. `remem memory sync` should adopt it - the case the brief's
    "1 adopted" assertion exercises."""
    _store, conn = _designated_store(env)
    conn.commit()
    conn.close()

    directory = _memory_directory(env)
    directory.mkdir(parents=True)
    (directory / "a-fact.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name="a-fact", title="a-fact", description="a hook",
                type="project", body="the body\n", extra={},
            )
        )
    )
    return directory


@pytest.fixture
def memory_dir_in_conflict(env):
    """A designated project where the entry and the file already disagree
    and no watermark says which one moved - classify()'s "mark is None"
    conflict case, reached without a prior sync."""
    store, conn = _designated_store(env)
    owner = store.ensure_principal("brandon")
    remember(
        store, owner.id,
        title="A fact", body="from remem\n", kind=Kind.NOTE,
        project=PROJECT, tags=["mem:a-fact"], origin=Origin.AGENT,
    )
    conn.commit()
    conn.close()

    directory = _memory_directory(env)
    directory.mkdir(parents=True)
    (directory / "a-fact.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name="a-fact", title="a-fact", description="a hook",
                type="project", body="from claude\n", extra={},
            )
        )
    )
    return directory


def test_designate_requires_an_existing_collection(env):
    result = runner.invoke(app, ["memory", "designate", "nope"], env=env)
    assert result.exit_code == 1
    assert "nope" in result.stderr


def test_sync_on_an_undesignated_project_says_so_and_exits_nonzero(env):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 1
    assert "designate" in result.stderr


def test_sync_prints_counts(env, memory_dir_with_one_stray):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 0
    assert "1 adopted" in result.stdout


def test_sync_exits_nonzero_when_a_file_is_left_in_conflict(
    env, memory_dir_in_conflict
):
    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 1
    # Counts land on stdout, failures on stderr. Asserting the wrong stream
    # is the mistake this repo has already made once with `remem ingest`.
    assert "conflict" in result.stderr


def test_status_reports_the_designation_and_the_overlap(env):
    result = runner.invoke(app, ["memory", "status"], env=env)
    assert result.exit_code == 0
    assert "not designated" in result.stdout
