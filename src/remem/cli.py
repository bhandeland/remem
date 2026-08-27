"""Typer frontend. Parses arguments, calls services, formats output.
No decisions about knowledge belong in this file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Optional
from uuid import UUID

import psycopg
import typer

from remem.backends.postgres.migrate import applied_versions, migrate, pending_versions
from remem.config import load
from remem.domain import CollectionQuery, Entry, Kind, Origin, Query
from remem.project import resolve_project
from remem.services import kb, write
from remem.services.search import find
from remem.session import ensure_database, open_session

app = typer.Typer(help="Knowledge and memory store for AI coding agents.")
db_app = typer.Typer(help="Database setup and status.")
kb_app = typer.Typer(help="Knowledge bases.")
app.add_typer(db_app, name="db")
app.add_typer(kb_app, name="kb")

capture_app = typer.Typer(help="Automatic capture of session knowledge.")
app.add_typer(capture_app, name="capture")

handoff_app = typer.Typer(help="Session handoffs.")
app.add_typer(handoff_app, name="handoff")


def _default_project() -> str | None:
    """The repository's name, not the current directory's.

    See remem.project - a subdirectory or a worktree used to file entries
    under its own directory name, silently, where nothing would find them.
    """
    return resolve_project()


def _unreachable(dsn: str) -> None:
    """Docker not running is this tool's expected failure mode; say so."""
    typer.echo(
        f"Cannot reach Postgres at {dsn}. Start it with `docker compose up -d` "
        "(and make sure Docker itself is running).",
        err=True,
    )
    raise typer.Exit(1)


@contextmanager
def _session(*, autocommit: bool = False):
    """open_session() with the one failure every command shares handled once."""
    cfg = load()
    try:
        with open_session(cfg, autocommit=autocommit) as s:
            yield s
    except psycopg.OperationalError:
        _unreachable(cfg.dsn)


@contextmanager
def _connect(dsn: str):
    """A bare connection, for the db commands that run before the schema
    exists: open_session() seeds the principal, which needs its table."""
    try:
        with psycopg.connect(dsn) as conn:
            yield conn
    except psycopg.OperationalError:
        _unreachable(dsn)


def _entry_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        typer.echo(f"'{value}' is not a valid entry id", err=True)
        raise typer.Exit(1)


def _job_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        typer.echo(f"'{value}' is not a valid capture job id", err=True)
        raise typer.Exit(1)


def _entry_dict(entry: Entry, snippet: str | None = None) -> dict:
    data = {
        "id": str(entry.id),
        "kind": str(entry.kind),
        "title": entry.title,
        "project": entry.project,
        "tags": list(entry.tags),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
    if snippet is not None:
        data["snippet"] = snippet
    else:
        data["body"] = entry.body
    return data


def _body_from_editor(initial: str = "") -> str:
    """Compose a body in $EDITOR.

    A rule worth keeping is usually a paragraph, and shell quoting is a poor
    place to write prose. An empty result aborts: storing a blank entry because
    the editor was closed without writing is worse than doing nothing.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(initial)
        path = fh.name
    try:
        subprocess.call([editor, path])
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not text:
        typer.echo("Nothing written - not storing an empty entry.", err=True)
        raise typer.Exit(1)
    return text


def _resolve_project(project: str | None, is_global: bool) -> str | None:
    """Project defaults to the directory; --global opts out deliberately.

    Before this defaulted, forgetting --project stored an entry with no
    project - a silent orphan, since a project's knowledge base queries on it.
    The write succeeded and the entry simply never appeared.
    """
    if is_global and project is not None:
        typer.echo("Pass either --project or --global, not both", err=True)
        raise typer.Exit(1)
    if is_global:
        return None
    return project or _default_project()


def _read_body(body: str | None) -> str:
    if body == "-":
        return sys.stdin.read()
    if body is None:
        raise typer.BadParameter("--body is required (use '-' to read stdin)")
    return body


@app.command()
def whoami():
    """Show the active principal and database."""
    with _session() as s:
        typer.echo(f"{s.owner.handle}  ({s.owner.id})")
        typer.echo(s.config.dsn)


@app.command()
def remember(
    title: str,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    kind: Annotated[Kind, typer.Option("--kind")] = Kind.MEMORY,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Store a memory, doc, or rule.

    The project defaults to this directory's name, which is what the knowledge
    base injected at session start queries on. Pass --global for knowledge that
    is not tied to one project.
    """
    resolved = _resolve_project(project, is_global)
    text = _body_from_editor() if edit else _read_body(body)
    with _session() as s:
        entry = write.remember(
            s.store, s.owner.id, title=title, body=text, kind=kind,
            project=resolved, tags=list(tag or []), origin=Origin.HUMAN,
        )
        typer.echo(entry.id)


@app.command()
def rule(
    title: str,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Write a convention for this project.

    Shorthand for `remember --kind rule`. Rules are the entries injected into
    every session and never truncated, so they are the ones worth making
    frictionless to write.
    """
    resolved = _resolve_project(project, is_global)
    text = _body_from_editor() if edit else _read_body(body)
    with _session() as s:
        entry = write.remember(
            s.store, s.owner.id, title=title, body=text, kind=Kind.RULE,
            project=resolved, tags=list(tag or []), origin=Origin.HUMAN,
        )
        typer.echo(entry.id)


@app.command()
def search(
    query: str,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    handoff: Annotated[bool, typer.Option("--handoff")] = False,
):
    """Search stored knowledge.

    Falls back to typo-tolerant matching when an exact search finds nothing;
    those results are marked with a leading ~ (and "fuzzy": true in --json).
    --handoff also searches session handoffs, which are excluded by default.
    """
    with _session() as s:
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=list(kind or []), project=project,
                  tags=list(tag or []), limit=limit),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=handoff,
        )
    if as_json:
        payload = []
        for h in hits:
            data = _entry_dict(h.entry, h.snippet)
            data["fuzzy"] = h.fuzzy
            payload.append(data)
        typer.echo(json.dumps(payload, indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    if hits[0].fuzzy:
        # Say it once, up front: these are approximate, and the caller should
        # know that before reading any of them.
        typer.echo(f"No exact matches for {query!r}. Showing similar entries:\n")
    for h in hits:
        marker = "~ " if h.fuzzy else ""
        typer.echo(f"{marker}{h.entry.id}  [{h.entry.kind}] {h.entry.title}")
        typer.echo(f"    {h.snippet}")


@app.command()
def get(
    entry_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Print one entry in full."""
    parsed = _entry_id(entry_id)
    with _session() as s:
        entry = s.store.get_entry(parsed, s.owner.id)
    if entry is None:
        typer.echo(f"No entry {entry_id}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(_entry_dict(entry), indent=2))
    else:
        typer.echo(f"# {entry.title}\n")
        typer.echo(entry.body)


@app.command()
def update(
    entry_id: str,
    title: Annotated[Optional[str], typer.Option("--title")] = None,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    clear_project: Annotated[bool, typer.Option("--clear-project")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    clear_tags: Annotated[bool, typer.Option("--clear-tags")] = False,
):
    """Edit an entry in place (for typos - use supersede for corrections).

    Only the fields you pass change. --clear-project and --clear-tags empty a
    field, which passing nothing cannot express.
    """
    parsed = _entry_id(entry_id)
    text = _read_body(body) if body is not None else None
    if clear_project and project is not None:
        typer.echo("Pass either --project or --clear-project, not both", err=True)
        raise typer.Exit(1)
    if clear_tags and tag:
        typer.echo("Pass either --tag or --clear-tags, not both", err=True)
        raise typer.Exit(1)

    new_project = write.CLEAR if clear_project else project
    new_tags = [] if clear_tags else (list(tag) if tag else None)

    with _session() as s:
        try:
            entry = write.update(s.store, s.owner.id, parsed,
                                 title=title, body=text,
                                 project=new_project, tags=new_tags)
        except write.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        typer.echo(entry.id)


@app.command()
def supersede(
    entry_id: str,
    title: Annotated[str, typer.Option("--title")],
    body: Annotated[Optional[str], typer.Option("--body")] = None,
):
    """Replace knowledge that stopped being true. The old entry is kept."""
    parsed = _entry_id(entry_id)
    text = _read_body(body)
    with _session() as s:
        try:
            entry = write.supersede(s.store, s.owner.id, parsed,
                                    title=title, body=text)
        except write.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        typer.echo(entry.id)


@kb_app.command("new")
def kb_new(
    slug: str,
    title: Annotated[str, typer.Option("--title")],
    description: Annotated[Optional[str], typer.Option("--description")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Create a knowledge base."""
    with _session() as s:
        c = kb.create(
            s.store, s.owner.id, slug=slug, title=title,
            description=description, project=project,
            query=CollectionQuery(tags=list(tag or []), project=project),
        )
        typer.echo(c.slug)
        for advisory in kb.advisories(c):
            typer.echo(f"warning: {advisory}", err=True)


@kb_app.command("query")
def kb_query(
    slug: str,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    clear: Annotated[bool, typer.Option("--clear")] = False,
):
    """Replace which entries a knowledge base selects automatically.

    A query is otherwise fixed at creation. Pinned entries are unaffected.
    --clear empties the query so the knowledge base holds only its pins.
    """
    if clear and (project or tag or kind):
        typer.echo("Pass either --clear or the filters, not both", err=True)
        raise typer.Exit(1)

    query = CollectionQuery() if clear else CollectionQuery(
        tags=list(tag or []), kinds=list(kind or []), project=project
    )
    with _session() as s:
        try:
            collection = kb.set_query(s.store, s.owner.id, slug, query)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        for note in kb.advisories(collection):
            typer.echo(f"note: {note}", err=True)
        typer.echo(collection.slug)


@kb_app.command("list")
def kb_list():
    """List knowledge bases."""
    with _session() as s:
        for c in s.store.list_collections(s.owner.id):
            typer.echo(f"{c.slug}\t{c.title}")


@kb_app.command("pin")
def kb_pin(slug: str, entry_id: str,
           position: Annotated[int, typer.Option("--position")] = 0):
    """Pin an entry into a knowledge base."""
    parsed = _entry_id(entry_id)
    with _session() as s:
        try:
            kb.pin(s.store, s.owner.id, slug, parsed, position)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        except kb.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        typer.echo("pinned")


@kb_app.command("show")
def kb_show(
    slug: str,
    max_chars: Annotated[Optional[int], typer.Option("--max-chars")] = None,
    full: Annotated[bool, typer.Option("--full")] = False,
):
    """Render a knowledge base as a context block."""
    with _session() as s:
        try:
            collection = kb.get(s.store, s.owner.id, slug)
            entries = kb.resolve(s.store, s.owner.id, slug)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        budget = 10**9 if full else (max_chars or s.config.max_chars)
        try:
            typer.echo(kb.render(collection, entries, budget))
        except kb.RulesExceedBudget as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)


@db_app.command("up")
def db_up():
    """Create the database, apply migrations, and seed the principal."""
    from remem.backends.postgres.store import PostgresStore

    cfg = load()
    try:
        created = ensure_database(cfg.dsn)
    except psycopg.OperationalError:
        _unreachable(cfg.dsn)
    if created:
        typer.echo(f"Created database at {cfg.dsn}")

    with _connect(cfg.dsn) as conn:
        applied = migrate(conn)
        conn.commit()
        owner = PostgresStore(conn).ensure_principal(cfg.user_handle)
        conn.commit()

    typer.echo(f"Applied: {', '.join(applied) if applied else 'nothing pending'}")
    typer.echo(f"Principal: {owner.handle} ({owner.id})")


@db_app.command("migrate")
def db_migrate():
    """Apply pending migrations."""
    cfg = load()
    with _connect(cfg.dsn) as conn:
        applied = migrate(conn)
        conn.commit()
    typer.echo(", ".join(applied) if applied else "nothing pending")


@db_app.command("status")
def db_status():
    """Show connectivity and migration state."""
    cfg = load()
    with _connect(cfg.dsn) as conn:
        typer.echo(f"Connected: {cfg.dsn}")
        typer.echo(f"Applied:   {', '.join(applied_versions(conn)) or 'none'}")
        typer.echo(f"Pending:   {', '.join(pending_versions(conn)) or 'none'}")


@app.command()
def serve():
    """Run the MCP server on stdio (this is what agents launch)."""
    from remem.mcp_server import main as serve_main

    serve_main()


@app.command()
def install(
    agent: Annotated[str, typer.Argument()] = "claude-code",
    scope: Annotated[str, typer.Option("--scope")] = "user",
):
    """Install remem into an agent (MCP server, hook, and skill)."""
    from remem.agents.base import UnsupportedScope
    from remem.agents.registry import UnknownAgent, get as get_adapter

    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    try:
        report = adapter.install(scope=scope)
    except UnsupportedScope as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for action in report.actions:
        typer.echo(f"  {action}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    typer.echo(f"\nInstalled remem for {report.agent}.")
    for note in report.notes:
        typer.echo(note)


hook_app = typer.Typer(help="Agent hook entry points (not for interactive use).")
app.add_typer(hook_app, name="hook")


@hook_app.command("session-start")
def hook_session_start():
    """Print the project's knowledge base. Always exits 0."""
    from remem.agents.claude_code.hook import main as hook_main

    raise typer.Exit(hook_main())


@hook_app.command("session-end")
def hook_session_end():
    """Queue this session for capture. Always exits 0."""
    from remem.agents.claude_code.hook import main_session_end

    raise typer.Exit(main_session_end())


@hook_app.command("session-size")
def hook_session_size():
    """Warn when a session has grown long enough to hand off. Always exits 0."""
    from remem.agents.claude_code.hook import main_session_size

    raise typer.Exit(main_session_size())


@capture_app.command("enable")
def capture_enable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn on automatic capture for a project (defaults to this directory)."""
    from remem.services import capture

    name = project or _default_project()
    with _session() as s:
        capture.enable(s.store, s.owner.id, name)
        model = s.config.capture_model
    typer.echo(f"capture enabled for '{name}'")
    # State the cost at the moment the tradeoff is actionable. Measured on a
    # real session: roughly 20 cents per distillation on sonnet, a third of
    # that on haiku - which returned noticeably worse judgement about what was
    # worth keeping.
    typer.echo(
        f"distillation runs `claude -p --model {model}` once per session, "
        f"roughly $0.10-0.25 each.\n"
        f"change it with REMEM_CAPTURE_MODEL (e.g. haiku for less, "
        f"opus for more)."
    )


@capture_app.command("disable")
def capture_disable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn off automatic capture for a project."""
    from remem.services import capture

    name = project or _default_project()
    with _session() as s:
        capture.disable(s.store, s.owner.id, name)
    typer.echo(f"capture disabled for '{name}'")


@capture_app.command("status")
def capture_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show what capture has queued, done, and failed."""
    with _session() as s:
        counts = s.store.capture_job_counts(s.owner.id)
        failures = s.store.recent_failed_capture_jobs(s.owner.id)
        model = s.config.capture_model
        projects = s.store.enabled_capture_projects(s.owner.id)

    if as_json:
        typer.echo(json.dumps({
            "model": model,
            "enabled_projects": projects,
            "counts": counts,
            "failures": [
                {"id": str(f.id), "project": f.project, "error": f.error}
                for f in failures
            ],
        }, indent=2))
        return

    typer.echo(f"Capture enabled for: {', '.join(projects) or 'no projects'}")
    typer.echo(f"Distillation model: {model}")
    if counts:
        typer.echo("Jobs: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        typer.echo("Jobs: none yet")
    for f in failures:
        typer.echo(f"  failed {f.id} [{f.project}]: {f.error}")


@capture_app.command("drain")
def capture_drain(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    job: Annotated[Optional[str], typer.Option("--job")] = None,
):
    """Distil queued sessions into entries.

    `--job ID` retries exactly that job, however many times it has already
    failed. It is the only way back for a job that hit the attempt cap.
    """
    from remem.distill.claude_cli import ClaudeCliDistiller
    from remem.services import capture

    job_id = _job_id(job) if job is not None else None

    # Autocommit, unlike every other command: the drain records its own
    # progress as it goes, and it spends minutes at a time inside `claude`.
    # One transaction for the batch would both discard already-succeeded work
    # on a database error and hold row locks across those minutes.
    with _session(autocommit=True) as s:
        distiller = ClaudeCliDistiller(model=s.config.capture_model)
        if job_id is not None:
            try:
                report = capture.drain_job(s.store, s.owner.id, job_id,
                                           distiller)
            except capture.CaptureJobNotFound as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)
        else:
            report = capture.drain(s.store, s.owner.id, distiller, limit=limit)
    typer.echo(
        f"claimed {report.claimed}, succeeded {report.succeeded}, "
        f"failed {report.failed}, entries written {report.entries_written}"
    )


@handoff_app.command("write")
def handoff_write(
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    topic: Annotated[Optional[str], typer.Option("--topic")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Store this session's state so it can be resumed after /clear.

    The body is four sections - Done, In flight, Next steps, Gotchas. Writing
    a handoff supersedes the previous one for the same topic, so a project
    only ever has one live handoff per workstream.
    """
    from remem.services import handoff as handoff_svc

    name = project or _default_project()
    text = (
        _body_from_editor(handoff_svc.BLANK_BODY) if edit else _read_body(body)
    )
    with _session() as s:
        try:
            entry, superseded = handoff_svc.write(
                s.store, s.owner.id, project=name, topic=topic, body=text,
            )
        except (handoff_svc.NoProject, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
    typer.echo(entry.id)
    if superseded is not None:
        typer.echo(f"superseded {superseded.id}")
    typer.echo(
        f"resume with: /clear, then remem-prime "
        f"{handoff_svc.topic_of(entry)}"
    )


@handoff_app.command("latest")
def handoff_latest(
    topic: Annotated[Optional[str], typer.Option("--topic")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Print the newest live handoff for this project."""
    from remem.services import handoff as handoff_svc

    name = project or _default_project()
    with _session() as s:
        entry = (
            handoff_svc.latest(s.store, s.owner.id, project=name, topic=topic)
            if name
            else None
        )
    if entry is None:
        # An ordinary state, not an error: most projects have never been
        # handed off, and prime asks about them anyway.
        typer.echo(f"No handoff stored for '{name or 'this directory'}'.")
        return
    if as_json:
        typer.echo(json.dumps(_entry_dict(entry), indent=2))
        return
    typer.echo(f"{entry.title}  ({entry.id})")
    typer.echo("")
    typer.echo(entry.body)


if __name__ == "__main__":
    app()
