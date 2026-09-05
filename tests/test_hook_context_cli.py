"""`remem hook context` - the harness-neutral half of SessionStart.

Same contract as `remem record event` (tests/test_record_cli.py): a hook
entry point in everything but name, so it must exit 0 unconditionally and
print nothing but the block itself to stdout. Every early return explains
itself only through REMEM_HOOK_DEBUG on stderr.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.agents.cursor.adapter import ROOT_KEY, CursorAdapter
from remem.backends.postgres.migrate import migrate
from remem.backends.postgres.store import PostgresStore
from remem.cli import app

runner = CliRunner()

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    # `hook context` spawns two detached `remem` processes in a `finally`
    # on every path (extraction and re-ingest). Pointed at this live test
    # database, those processes outlive the test and race conftest's
    # truncate-cascade for locks on the same tables - a deadlock seen twice
    # on this branch. Every test gets the no-op stub by default; the tests
    # that assert on spawning install their own recorder afterwards, which
    # wins because monkeypatch applies in call order.
    monkeypatch.setattr("remem.hookio.spawn_process", lambda env: False)
    monkeypatch.setattr("remem.hookio.spawn_ingest", lambda env: False)
    return live_dsn


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "myrepo"
    root.mkdir()
    return root


def _seed_kb(dsn, *, project="myrepo"):
    """A knowledge base whose slug matches `repo`'s directory name, with one
    rule in it - so a test can assert the command's stdout actually carries
    that rule, not merely that the command ran without crashing."""
    from remem.domain import CollectionQuery, Kind
    from remem.services import kb
    from remem.services.write import remember

    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        kb.create(store, owner.id, slug=project, title=project,
                  query=CollectionQuery(project=project))
        remember(store, owner.id, title="Lint rule", body="always run ruff",
                 summary="Run ruff linter", kind=Kind.RULE, project=project)
        c.commit()


def test_context_exits_zero_on_garbage_stdin(env):
    """Fail-soft: malformed stdin must not be why the block is missing."""
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_garbage_stdin_under_hook_debug(env, monkeypatch):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "not valid JSON" in result.stderr


def test_context_exits_zero_for_an_unknown_agent(env, repo):
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_an_unknown_agent_under_hook_debug(env, monkeypatch, repo):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no-such-agent" in result.stderr


def test_context_exits_zero_when_the_payload_carries_no_cwd(env):
    """No cwd means identity.project is None - an ordinary "cannot place
    this session" answer, not an error."""
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_a_missing_cwd_under_hook_debug(env, monkeypatch):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no project" in result.stderr.lower() or "cwd" in result.stderr.lower()


def test_an_adapter_whose_identity_capability_raises_degrades(env, monkeypatch, repo):
    """Same contract as event()/env_settings()/settings_path(): a broken
    third-party adapter must never be why the block is missing for
    everyone - it just costs the block, silently."""

    def boom(self, env, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_an_adapter_whose_identity_capability_raises_explains_itself(
    env, monkeypatch, repo
):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")

    def boom(self, env, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "identity" in result.stderr
    assert "boom" in result.stderr


def test_context_prints_the_knowledge_base_for_the_session(env, repo):
    """The happy path: this is the command's only reason to exist, and
    nothing above pins it - every other test here uses a repo with no
    knowledge base, so a deleted `typer.echo(...)` would leave them all
    green."""
    _seed_kb(env, project=repo.name)

    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"cwd": str(repo), "session_id": "x"})
    )

    assert result.exit_code == 0
    assert "Lint rule" in result.stdout
    assert "Run ruff linter" in result.stdout


def test_context_reads_a_named_agent_and_matches_the_default(env, repo):
    """The opencode adapter reads sessionID, not session_id - a payload
    shape difference `--agent` exists to absorb. This is the injection-half
    equivalent of `remem record event`'s --agent tests: with a knowledge
    base actually in place, the two adapters must read different keys out
    of different payloads and still produce byte-identical output - proving
    --agent reached a distinct, working adapter rather than merely failing
    to find one (which would also print nothing and exit 0)."""
    _seed_kb(env, project=repo.name)

    claude_code = runner.invoke(
        app,
        ["hook", "context", "--agent", "claude-code"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    opencode = runner.invoke(
        app,
        ["hook", "context", "--agent", "opencode"],
        input=json.dumps({"cwd": str(repo), "sessionID": "x"}),
    )

    assert claude_code.exit_code == 0
    assert opencode.exit_code == 0
    assert claude_code.stdout != ""
    assert claude_code.stdout == opencode.stdout


def test_an_adapter_whose_inject_capability_raises_degrades_to_stdout(env, monkeypatch, repo):
    """Same contract as identity()/event()/env_settings()/settings_path(): a
    broken inject() must not be why the block never reaches the harness -
    it just falls back to the stdout path every other adapter already
    uses."""
    _seed_kb(env, project=repo.name)

    def boom(self, block, payload, note=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(CursorAdapter, "inject", boom)
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "Lint rule" in result.stdout


def test_an_adapter_whose_inject_capability_raises_explains_itself(env, monkeypatch, repo):
    monkeypatch.setenv("REMEM_HOOK_DEBUG", "1")
    _seed_kb(env, project=repo.name)

    def boom(self, block, payload, note=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(CursorAdapter, "inject", boom)
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "inject" in result.stderr
    assert "boom" in result.stderr


def test_an_adapter_with_inject_does_not_print_the_block_to_stdout(env, repo):
    """Cursor cannot read stdout - printing the block there anyway would be
    noise nobody reads, not a useful fallback. inject() being present and
    succeeding means delivery already happened, so stdout stays empty and
    the block lands in the rules file instead."""
    _seed_kb(env, project=repo.name)

    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    written = repo / ".cursor" / "rules" / "remem.mdc"
    assert written.exists()
    assert "Lint rule" in written.read_text()


# --- Extraction is triggered from here, not only from Claude Code ---------
#
# `remem events process` used to be spawned from exactly one place, Claude
# Code's SessionStart hook, so a Cursor-only or opencode-only install
# recorded events forever and never extracted one. `hook context` is the
# session-start analogue every other harness already calls once per session,
# which makes it the one trigger all three share.


def _spy(monkeypatch):
    """Patch the spawn where `hook context` looks it up, and record calls."""
    calls: list[dict] = []

    def fake(env):
        calls.append(dict(env))
        return True

    monkeypatch.setattr("remem.hookio.spawn_process", fake)
    return calls


def test_context_spawns_the_extraction_processor(env, repo, monkeypatch):
    calls = _spy(monkeypatch)
    _seed_kb(env)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "s1"}),
    )
    assert result.exit_code == 0
    assert len(calls) == 1


def test_context_spawns_the_processor_even_when_no_project_resolves(
    env, monkeypatch
):
    """The backlog is global, not this session's project.

    `remem events process` works off every extractable session for the
    owner, so whether THIS payload produced a block has no bearing on
    whether there is extraction work waiting. Claude Code spawns
    regardless of whether its block rendered; this must match.
    """
    calls = _spy(monkeypatch)
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""
    assert len(calls) == 1


def test_spawn_process_refuses_to_run_inside_the_extractor(monkeypatch):
    """The recursion guard, exercised directly on the shared helper.

    The extractor spawns `claude -p`, whose own hooks would otherwise spawn
    another extractor, which reads the events that run recorded, without
    bound. CHILD_ENV_VAR is what stops it. Asserted against spawn_process
    itself rather than through the CLI, because the command legitimately
    shells out to git to resolve the project - trapping every Popen would
    catch that instead and pass for the wrong reason.
    """
    from remem import hookio
    from remem.extract.base import CHILD_ENV_VAR

    def explode(*a, **k):
        raise AssertionError("spawned a processor inside the extractor")

    monkeypatch.setattr("subprocess.Popen", explode)
    assert hookio.spawn_process({CHILD_ENV_VAR: "1"}) is False


def test_spawn_process_launches_the_processor_detached(monkeypatch):
    """The guard must not be the only reason it ever returns False."""
    from remem import hookio

    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    assert hookio.spawn_process({}) is True
    assert seen["argv"] == ["remem", "events", "process"]
    assert seen["kwargs"]["start_new_session"] is True
