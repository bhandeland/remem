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
        store,
        owner.id,
        slug="proj-memory",
        title="Memory",
        project=PROJECT,
        query=CollectionQuery(project=PROJECT),
    )
    memory.designate(store, owner.id, PROJECT, "proj-memory")
    return store, conn


def _memory_directory(env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / slug_for(Path.cwd()) / "memory"


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
def memory_dir_in_conflict(env):
    """A designated project where the entry and the file already disagree
    and no watermark says which one moved - classify()'s "mark is None"
    conflict case, reached without a prior sync."""
    store, conn = _designated_store(env)
    owner = store.ensure_principal("brandon")
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


def test_a_bare_designate_refuses_rather_than_clearing(env):
    # `None if clear else slug` used to collapse "argument omitted" into
    # "clear it", so a user who typed this to see the current designation had
    # destroyed it by the time they read the output.
    store, conn = _designated_store(env)
    conn.commit()
    owner = store.ensure_principal("brandon")

    result = runner.invoke(app, ["memory", "designate"], env=env)

    assert result.exit_code != 0
    conn.rollback()  # a fresh read, not this transaction's snapshot
    assert memory.designation(store, owner.id, PROJECT) == "proj-memory"
    conn.close()


def test_designate_none_still_clears(env):
    store, conn = _designated_store(env)
    conn.commit()
    owner = store.ensure_principal("brandon")

    result = runner.invoke(app, ["memory", "designate", "--none"], env=env)

    assert result.exit_code == 0
    conn.rollback()
    assert memory.designation(store, owner.id, PROJECT) is None
    conn.close()


def test_a_dry_run_conflict_does_not_promise_a_sidecar(env, memory_dir_in_conflict):
    result = runner.invoke(app, ["memory", "sync", "--dry-run"], env=env)

    assert result.exit_code == 1
    assert f"no {memory.CONFLICT_SUFFIX} file was written" in result.stderr
    assert list(memory_dir_in_conflict.glob(f"*{memory.CONFLICT_SUFFIX}")) == []


def test_a_conflict_with_a_sidecar_names_it(env, memory_dir_in_conflict):
    result = runner.invoke(app, ["memory", "sync"], env=env)

    assert result.exit_code == 1
    assert f"a-fact{memory.CONFLICT_SUFFIX}" in result.stderr
    assert (memory_dir_in_conflict / f"a-fact{memory.CONFLICT_SUFFIX}").exists()


def test_status_reports_the_designation_and_the_overlap(env):
    result = runner.invoke(app, ["memory", "status"], env=env)
    assert result.exit_code == 0
    assert "not designated" in result.stdout


# --- sync --all ---------------------------------------------------------
def test_designate_records_the_working_directory(env):
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
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["memory", "designate", "proj-memory"], env=env)
    assert result.exit_code == 0, result.output

    conn = psycopg.connect(env["REMEM_DSN"])
    store = PostgresStore(conn)
    owner = store.ensure_principal("brandon")
    [d] = memory.designations(store, owner.id)
    conn.close()
    assert d.working_dir == str(Path.cwd().resolve())


def test_designate_refuses_a_project_that_is_not_this_directory(env):
    # The designation records this cwd, so designating some other project
    # from here would record a directory that has nothing to do with it -
    # and the mismatch would surface much later, as a sync writing to the
    # wrong place.
    result = runner.invoke(
        app,
        ["memory", "designate", "proj-memory", "--project", "somewhere-else"],
        env=env,
    )
    assert result.exit_code == 1
    assert "Refusing" in result.output


def test_sync_all_and_project_are_mutually_exclusive(env):
    result = runner.invoke(
        app,
        ["memory", "sync", "--all", "--project", "x"],
        env=env,
    )
    assert result.exit_code != 0


def test_sync_all_with_nothing_designated_says_so_and_exits_zero(env):
    result = runner.invoke(app, ["memory", "sync", "--all"], env=env)
    assert result.exit_code == 0, result.output
    assert "No project has a memory collection" in result.output


def test_sync_all_reports_one_line_per_project(env, memory_dir_with_one_stray):
    # Designated by the fixture through the service, which records no
    # working directory - exactly the pre-migration-015 row shape - so this
    # also pins that such a row is skipped loudly rather than synced.
    result = runner.invoke(app, ["memory", "sync", "--all"], env=env)
    assert result.exit_code == 1
    assert "skipped" in result.output
    assert "designate" in result.output


def test_sync_all_syncs_a_designation_that_has_a_directory(
    env, memory_dir_with_one_stray
):
    # Re-designate through the CLI so the working directory is recorded.
    assert (
        runner.invoke(
            app,
            ["memory", "designate", "proj-memory"],
            env=env,
        ).exit_code
        == 0
    )
    result = runner.invoke(app, ["memory", "sync", "--all"], env=env)
    assert result.exit_code == 0, result.output
    assert "1 adopted" in result.output
    assert PROJECT in result.output


def test_sync_names_each_rename_it_followed(env, memory_dir_with_one_stray):
    # A rename re-tags an entry, which is a write to the store the user did
    # not ask for by name. Counting it inside `unchanged` would make the one
    # line the user reads describe a run in which nothing moved.
    directory = memory_dir_with_one_stray
    assert runner.invoke(app, ["memory", "sync"], env=env).exit_code == 0
    (directory / "a-fact.md").rename(directory / "renamed-fact.md")

    result = runner.invoke(app, ["memory", "sync"], env=env)
    assert result.exit_code == 0
    assert "1 renamed" in result.stdout
    assert "a-fact -> renamed-fact" in result.stdout
