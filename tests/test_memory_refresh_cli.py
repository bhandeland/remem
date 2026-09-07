"""`remem memory refresh` - the spawned, silent half of `memory sync`.

Nobody types this. It is started detached by a session start, so its whole
contract is the opposite of `sync`'s: exit 0 on every path, print nothing
to stdout, and explain itself only to stderr behind REMEM_HOOK_DEBUG. The
run row it leaves behind, and the advisory `remem record status` raises off
it, are how a person finds out what happened.

Same bootstrap as tests/test_memory_cli.py - the command opens its own
session, so the schema is committed before the CLI connects and the DSN
arrives through the environment.
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
from remem.domain import CollectionQuery, Kind, MemoryTrigger, Origin
from remem.project import resolve_project
from remem.services import kb, memory
from remem.services.write import remember

pytestmark = pytest.mark.db

runner = CliRunner()

PROJECT = resolve_project() or "remem"


@pytest.fixture
def env(live_dsn, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "REMEM_DSN": live_dsn,
        "REMEM_USER_ID": "brandon",
        "REMEM_CONFIG": str(tmp_path / "none.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }


def _memory_directory(env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / slug_for(Path.cwd()) / "memory"


def _designate(env):
    conn = psycopg.connect(env["REMEM_DSN"])
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    kb.create(
        store,
        owner.id,
        slug="proj-memory",
        title="Memory",
        project=PROJECT,
        query=CollectionQuery(project=PROJECT),
    )
    memory.designate(store, owner.id, PROJECT, "proj-memory")
    return store, conn, owner


@pytest.fixture
def designated_with_one_stray(env):
    """A designated project holding one file remem has never seen."""
    _store, conn, _owner = _designate(env)
    conn.commit()
    conn.close()
    directory = _memory_directory(env)
    directory.mkdir(parents=True)
    (directory / "a-fact.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name="a-fact",
                title="a-fact",
                description="a hook",
                type="project",
                body="the body\n",
                extra={},
            )
        )
    )
    return directory


@pytest.fixture
def designated_in_conflict(env):
    """Entry and file disagree with no watermark to say which side moved."""
    store, conn, owner = _designate(env)
    remember(
        store,
        owner.id,
        title="A fact",
        body="from remem\n",
        kind=Kind.NOTE,
        project=PROJECT,
        tags=["mem:a-fact"],
        origin=Origin.AGENT,
    )
    conn.commit()
    conn.close()
    directory = _memory_directory(env)
    directory.mkdir(parents=True)
    (directory / "a-fact.md").write_text(
        memory_file.render(
            memory_file.MemoryFile(
                name="a-fact",
                title="a-fact",
                description="a hook",
                type="project",
                body="from claude\n",
                extra={},
            )
        )
    )
    return directory


def _latest_run(env):
    with psycopg.connect(env["REMEM_DSN"]) as conn:
        store = PostgresStore(conn)
        owner = store.ensure_principal("brandon")
        return store.latest_memory_run(owner.id, PROJECT)


def test_refresh_syncs_the_directory_without_printing(env, designated_with_one_stray):
    """The work happens; the session sees nothing of it."""
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    run = _latest_run(env)
    assert run is not None
    assert run.adopted == 1


def test_refresh_records_the_run_as_auto(env, designated_with_one_stray):
    """The one caller `trigger='auto'` was added for. `sync` stays manual."""
    runner.invoke(app, ["memory", "refresh"], env=env)
    run = _latest_run(env)
    assert run.trigger == MemoryTrigger.AUTO
    assert run.finished_at is not None


def test_refresh_on_an_undesignated_project_does_nothing_quietly(env):
    """The common case: no designation, no row, no noise, exit 0.

    `memory sync` exits 1 here and tells the user to designate. From a hook
    that would be a non-zero exit on every session for everyone who has not
    opted in.
    """
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert _latest_run(env) is None


def test_an_undesignated_project_is_named_as_such_not_dumped_as_an_error(env):
    """Not opted in is the common case, and it reads as one under debug.

    Without the specific `NotDesignated` catch this still exits 0 - the
    outer BaseException handler swallows it - so the exit code alone cannot
    tell the two apart. The diagnostic is the only place the difference is
    visible, which makes it the thing worth asserting: a person debugging a
    silent hook must not be shown an exception where the answer is "you
    never designated this project".
    """
    result = runner.invoke(
        app, ["memory", "refresh"], env={**env, "REMEM_HOOK_DEBUG": "1"}
    )
    assert result.exit_code == 0
    assert "has no memory collection" in result.stderr
    assert "NotDesignated" not in result.stderr


def test_refresh_stays_silent_and_exits_zero_on_a_conflict(env, designated_in_conflict):
    """`sync` exits 1 here. The sidecar plus the advisory carry it instead."""
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert (designated_in_conflict / "a-fact.remem-conflict.md").exists()
    assert _latest_run(env).conflicts == ["a-fact"]


def test_refresh_explains_itself_on_stderr_under_hook_debug(
    env, designated_with_one_stray
):
    """Silence is ambiguous, so the opt-in diagnostic is the whole story."""
    result = runner.invoke(
        app, ["memory", "refresh"], env={**env, "REMEM_HOOK_DEBUG": "1"}
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "adopted" in result.stderr


def test_refresh_exits_zero_when_the_database_is_unreachable(env, tmp_path):
    """Docker being down is this tool's expected failure, and `_session`
    turns it into `typer.Exit(1)`. A hook-spawned command must swallow that
    like everything else."""
    result = runner.invoke(
        app,
        ["memory", "refresh"],
        env={**env, "REMEM_DSN": "postgresql://127.0.0.1:1/nope"},
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_refresh_exits_zero_even_when_something_calls_sys_exit(
    env, monkeypatch, designated_with_one_stray
):
    """The one thing `except BaseException` buys over `except Exception`.

    `typer.Exit` is a RuntimeError, so the unreachable-database path above
    does not distinguish the two handlers - it passes either way. A genuine
    SystemExit does, and it is not hypothetical: any library on this path is
    free to call sys.exit(). Nothing a detached command nobody is watching
    can hit should turn into a non-zero exit.
    """

    def boom(*a, **k):
        raise SystemExit(3)

    monkeypatch.setattr("remem.services.memory.sync", boom)
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
