import json

import psycopg
import pytest
from typer.testing import CliRunner

from remem.agents.claude_code.adapter import ClaudeCodeAdapter
from remem.backends.postgres.migrate import migrate
from remem.cli import app

runner = CliRunner()


def test_installer_registers_the_session_end_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for group in settings["hooks"]["SessionEnd"]
        for h in group["hooks"]
    ]
    assert any("remem hook session-end" in c for c in commands)


def test_installing_twice_leaves_one_session_end_hook(tmp_path):
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionEnd"]) == 1


def test_install_report_says_capture_is_off_by_default(tmp_path):
    report = ClaudeCodeAdapter().install(scope="user", home=tmp_path)
    combined = " ".join(report.actions + report.notes).lower()
    assert "capture" in combined
    assert "enable" in combined


@pytest.fixture
def env(live_dsn, monkeypatch, tmp_path):
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("REMEM_DSN", live_dsn)
    monkeypatch.setenv("REMEM_USER_ID", "brandon")
    monkeypatch.setenv("REMEM_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.mark.db
def test_enable_then_status_reports_the_project(env):
    assert runner.invoke(app, ["capture", "enable", "--project", "remem"]).exit_code == 0
    result = runner.invoke(app, ["capture", "status"])
    assert result.exit_code == 0
    assert "remem" in result.stdout


@pytest.mark.db
def test_status_json_is_valid_when_nothing_has_happened(env):
    result = runner.invoke(app, ["capture", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"] == {}
    assert payload["failures"] == []
    assert payload["enabled_projects"] == []


@pytest.mark.db
def test_disable_removes_the_project_from_status(env):
    runner.invoke(app, ["capture", "enable", "--project", "remem"])
    runner.invoke(app, ["capture", "disable", "--project", "remem"])
    payload = json.loads(
        runner.invoke(app, ["capture", "status", "--json"]).stdout
    )
    assert payload["enabled_projects"] == []


@pytest.mark.db
def test_drain_on_an_empty_queue_succeeds_quietly(env):
    result = runner.invoke(app, ["capture", "drain"])
    assert result.exit_code == 0
    assert "0" in result.stdout


class _TitleFromTranscript:
    """Distils one entry whose title is the transcript's own text.

    Lets a test tell the two jobs apart without spawning anything.
    """

    def __init__(self, *args, **kwargs):
        pass

    def distill(self, transcript, project):
        from remem.distill.base import CapturedEntry
        from remem.domain import Kind

        return [CapturedEntry(title=transcript.strip(), body="body",
                              kind=Kind.MEMORY)]


def _enqueue_job(dsn, project, transcript_path):
    from remem.backends.postgres.store import PostgresStore
    from remem.services import capture

    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        capture.enable(store, owner.id, project)
        job = capture.enqueue(store, owner.id, project=project,
                              transcript_path=transcript_path, session_id="s")
        c.commit()
        return owner.id, job.id


@pytest.mark.db
def test_a_real_database_error_does_not_discard_the_rest_of_the_drain(
    env, monkeypatch, tmp_path
):
    """One job erroring the connection must not roll back the whole batch.

    A genuine failed statement puts a non-autocommit connection into
    InFailedSqlTransaction, so every later statement - including the ones
    that record the failure - is swallowed, and Postgres turns the final
    COMMIT into a ROLLBACK without raising. The drain then reports work it
    did not do. Unlike a wrapper raising a Python error, this touches the
    real connection, which is the only way to reproduce that state.
    """
    from remem.backends.postgres.store import PostgresStore

    good = tmp_path / "good.jsonl"
    good.write_text("GOOD")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("BAD")
    owner_id, good_id = _enqueue_job(env, "remem", str(good))
    _, bad_id = _enqueue_job(env, "remem", str(bad))

    real_put = PostgresStore.put_entry

    def flaky_put(self, entry):
        if entry.title == "BAD":
            # A genuinely invalid statement on the live cursor, not a Python
            # exception raised beside it.
            self._conn.execute("select * from no_such_table_at_all")
        return real_put(self, entry)

    monkeypatch.setattr(PostgresStore, "put_entry", flaky_put)
    monkeypatch.setattr(
        "remem.distill.claude_cli.ClaudeCliDistiller", _TitleFromTranscript
    )

    result = runner.invoke(app, ["capture", "drain"])
    assert result.exit_code == 0

    with psycopg.connect(env) as c:
        rows = dict(
            (str(r[0]), (r[1], r[2]))
            for r in c.execute(
                "select id, status, error from capture_jobs"
            ).fetchall()
        )
        titles = [
            r[0] for r in c.execute("select title from entries").fetchall()
        ]

    assert rows[str(good_id)][0] == "done"
    assert rows[str(bad_id)][0] == "failed"
    assert rows[str(bad_id)][1] is not None
    # The successful job's work survived its neighbour's failure.
    assert titles == ["GOOD"]
    # And the report told the truth about it.
    assert "entries written 1" in result.stdout
