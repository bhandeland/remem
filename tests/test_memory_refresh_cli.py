"""`bag memory refresh` - the spawned, silent half of `memory sync`.

Nobody types this. It is started detached by a session start, so its whole
contract is the opposite of `sync`'s: exit 0 on every path, print nothing
to stdout, and explain itself only to stderr behind BAG_HOOK_DEBUG. The
run row it leaves behind, and the advisory `bag record status` raises off
it, are how a person finds out what happened.

Same bootstrap as tests/test_memory_cli.py - the command opens its own
session, so the schema is committed before the CLI connects and the DSN
arrives through the environment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag import memory_file
from saddlebag.agents.claude_code.memory import slug_for
from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.cli import app
from saddlebag.domain import (
    CollectionQuery,
    Kind,
    MemoryRun,
    MemoryTrigger,
    Origin,
    Principal,
)
from saddlebag.project import resolve_project
from saddlebag.services import kb, memory
from saddlebag.services.write import remember
from tests.conftest import found

pytestmark = pytest.mark.db

runner = CliRunner()

PROJECT = resolve_project() or "saddlebag"


@pytest.fixture
def env(live_dsn: str, tmp_path: Path) -> dict[str, str]:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    return {
        "BAG_DSN": live_dsn,
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }


def _memory_directory(env: dict[str, str]) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / slug_for(Path.cwd()) / "memory"


def _designate(
    env: dict[str, str],
) -> tuple[PostgresStore, psycopg.Connection[Any], Principal]:
    conn = psycopg.connect(env["BAG_DSN"])
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
def designated_with_one_stray(env: dict[str, str]) -> Path:
    """A designated project holding one file saddlebag has never seen."""
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
def designated_in_conflict(env: dict[str, str]) -> Path:
    """Entry and file disagree with no watermark to say which side moved."""
    store, conn, owner = _designate(env)
    remember(
        store,
        owner.id,
        title="A fact",
        body="from saddlebag\n",
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


def _latest_run(env: dict[str, str]) -> MemoryRun | None:
    with psycopg.connect(env["BAG_DSN"]) as conn:
        store = PostgresStore(conn)
        owner = store.ensure_principal("brandon")
        return store.latest_memory_run(owner.id, PROJECT)


def test_refresh_syncs_the_directory_without_printing(
    env: dict[str, str], designated_with_one_stray: Path
) -> None:
    """The work happens; the session sees nothing of it."""
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    run = _latest_run(env)
    assert run is not None
    assert run.adopted == 1


def test_refresh_records_the_run_as_auto(
    env: dict[str, str], designated_with_one_stray: Path
) -> None:
    """The one caller `trigger='auto'` was added for. `sync` stays manual."""
    runner.invoke(app, ["memory", "refresh"], env=env)
    run = found(_latest_run(env))
    assert run.trigger == MemoryTrigger.AUTO
    assert run.finished_at is not None


def test_refresh_on_an_undesignated_project_does_nothing_quietly(
    env: dict[str, str],
) -> None:
    """The common case: no designation, no row, no noise, exit 0.

    `memory sync` exits 1 here and tells the user to designate. From a hook
    that would be a non-zero exit on every session for everyone who has not
    opted in.
    """
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert _latest_run(env) is None


def test_an_undesignated_project_is_named_as_such_not_dumped_as_an_error(
    env: dict[str, str],
) -> None:
    """Not opted in is the common case, and it reads as one under debug.

    Without the specific `NotDesignated` catch this still exits 0 - the
    outer BaseException handler swallows it - so the exit code alone cannot
    tell the two apart. The diagnostic is the only place the difference is
    visible, which makes it the thing worth asserting: a person debugging a
    silent hook must not be shown an exception where the answer is "you
    never designated this project".
    """
    result = runner.invoke(
        app, ["memory", "refresh"], env={**env, "BAG_HOOK_DEBUG": "1"}
    )
    assert result.exit_code == 0
    assert "has no memory collection" in result.stderr
    assert "NotDesignated" not in result.stderr


def test_refresh_stays_silent_and_exits_zero_on_a_conflict(
    env: dict[str, str], designated_in_conflict: Path
) -> None:
    """`sync` exits 1 here. The sidecar plus the advisory carry it instead."""
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert (designated_in_conflict / "a-fact.saddlebag-conflict.md").exists()
    assert found(_latest_run(env)).conflicts == ["a-fact"]


def test_refresh_explains_itself_on_stderr_under_hook_debug(
    env: dict[str, str], designated_with_one_stray: Path
) -> None:
    """Silence is ambiguous, so the opt-in diagnostic is the whole story."""
    result = runner.invoke(
        app, ["memory", "refresh"], env={**env, "BAG_HOOK_DEBUG": "1"}
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "adopted" in result.stderr


def test_refresh_exits_zero_when_the_database_is_unreachable(
    env: dict[str, str], tmp_path: Path
) -> None:
    """Docker being down is this tool's expected failure, and `_session`
    turns it into `typer.Exit(1)`. A hook-spawned command must swallow that
    like everything else."""
    result = runner.invoke(
        app,
        ["memory", "refresh"],
        env={**env, "BAG_DSN": "postgresql://127.0.0.1:1/nope"},
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_refresh_exits_zero_even_when_something_calls_sys_exit(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    designated_with_one_stray: Path,
) -> None:
    """The one thing `except BaseException` buys over `except Exception`.

    `typer.Exit` is a RuntimeError, so the unreachable-database path above
    does not distinguish the two handlers - it passes either way. A genuine
    SystemExit does, and it is not hypothetical: any library on this path is
    free to call sys.exit(). Nothing a detached command nobody is watching
    can hit should turn into a non-zero exit.
    """

    def boom(*a: Any, **k: Any) -> memory.Report:
        raise SystemExit(3)

    monkeypatch.setattr("saddlebag.services.memory.sync", boom)
    result = runner.invoke(app, ["memory", "refresh"], env=env)
    assert result.exit_code == 0
    assert result.stdout == ""
