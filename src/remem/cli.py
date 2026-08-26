"""Typer frontend. Parses arguments, calls services, formats output.
No decisions about knowledge belong in this file."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Annotated, Optional
from uuid import UUID

import psycopg
import typer

from remem.backends.postgres.migrate import applied_versions, migrate, pending_versions
from remem.config import load
from remem.domain import CollectionQuery, Entry, Kind, Origin, Query
from remem.services import kb, write
from remem.services.search import find
from remem.session import ensure_database, open_session

app = typer.Typer(help="Knowledge and memory store for AI coding agents.")
db_app = typer.Typer(help="Database setup and status.")
kb_app = typer.Typer(help="Knowledge bases.")
app.add_typer(db_app, name="db")
app.add_typer(kb_app, name="kb")


def _unreachable(dsn: str) -> None:
    """Docker not running is this tool's expected failure mode; say so."""
    typer.echo(
        f"Cannot reach Postgres at {dsn}. Start it with `docker compose up -d` "
        "(and make sure Docker itself is running).",
        err=True,
    )
    raise typer.Exit(1)


@contextmanager
def _session():
    """open_session() with the one failure every command shares handled once."""
    cfg = load()
    try:
        with open_session(cfg) as s:
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
    kind: Annotated[Kind, typer.Option("--kind")] = Kind.MEMORY,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Store a memory, doc, or rule."""
    text = _read_body(body)
    with _session() as s:
        entry = write.remember(
            s.store, s.owner.id, title=title, body=text, kind=kind,
            project=project, tags=list(tag or []), origin=Origin.HUMAN,
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
):
    """Search stored knowledge.

    Falls back to typo-tolerant matching when an exact search finds nothing;
    those results are marked with a leading ~ (and "fuzzy": true in --json).
    """
    with _session() as s:
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=list(kind or []), project=project,
                  tags=list(tag or []), limit=limit),
            fuzzy_threshold=s.config.fuzzy_threshold,
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


if __name__ == "__main__":
    app()
