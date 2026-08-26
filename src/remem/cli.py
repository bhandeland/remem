"""Typer frontend. Parses arguments, calls services, formats output.
No decisions about knowledge belong in this file."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Optional
from uuid import UUID

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
    with open_session() as s:
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
    with open_session() as s:
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
    """Search stored knowledge."""
    with open_session() as s:
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=list(kind or []), project=project,
                  tags=list(tag or []), limit=limit),
        )
    if as_json:
        typer.echo(json.dumps([_entry_dict(h.entry, h.snippet) for h in hits], indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    for h in hits:
        typer.echo(f"{h.entry.id}  [{h.entry.kind}] {h.entry.title}")
        typer.echo(f"    {h.snippet}")


@app.command()
def get(
    entry_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Print one entry in full."""
    with open_session() as s:
        entry = s.store.get_entry(UUID(entry_id), s.owner.id)
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
):
    """Edit an entry in place (for typos - use supersede for corrections)."""
    text = _read_body(body) if body is not None else None
    with open_session() as s:
        try:
            entry = write.update(s.store, s.owner.id, UUID(entry_id),
                                 title=title, body=text)
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
    text = _read_body(body)
    with open_session() as s:
        try:
            entry = write.supersede(s.store, s.owner.id, UUID(entry_id),
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
    with open_session() as s:
        c = kb.create(
            s.store, s.owner.id, slug=slug, title=title,
            description=description, project=project,
            query=CollectionQuery(tags=list(tag or []), project=project),
        )
        typer.echo(c.slug)


@kb_app.command("list")
def kb_list():
    """List knowledge bases."""
    with open_session() as s:
        for c in s.store.list_collections(s.owner.id):
            typer.echo(f"{c.slug}\t{c.title}")


@kb_app.command("pin")
def kb_pin(slug: str, entry_id: str,
           position: Annotated[int, typer.Option("--position")] = 0):
    """Pin an entry into a knowledge base."""
    with open_session() as s:
        try:
            c = kb.get(s.store, s.owner.id, slug)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        s.store.pin(c.id, UUID(entry_id), position)
        typer.echo("pinned")


@kb_app.command("show")
def kb_show(
    slug: str,
    max_chars: Annotated[Optional[int], typer.Option("--max-chars")] = None,
    full: Annotated[bool, typer.Option("--full")] = False,
):
    """Render a knowledge base as a context block."""
    with open_session() as s:
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
    import psycopg

    from remem.backends.postgres.store import PostgresStore

    cfg = load()
    created = ensure_database(cfg.dsn)
    if created:
        typer.echo(f"Created database at {cfg.dsn}")

    # Migrations must run on a bare connection: open_session() seeds the
    # principal, which needs the principals table to already exist.
    with psycopg.connect(cfg.dsn) as conn:
        applied = migrate(conn)
        conn.commit()
        owner = PostgresStore(conn).ensure_principal(cfg.user_handle)
        conn.commit()

    typer.echo(f"Applied: {', '.join(applied) if applied else 'nothing pending'}")
    typer.echo(f"Principal: {owner.handle} ({owner.id})")


@db_app.command("migrate")
def db_migrate():
    """Apply pending migrations."""
    with open_session() as s:
        applied = migrate(s.conn)
        s.conn.commit()
    typer.echo(", ".join(applied) if applied else "nothing pending")


@db_app.command("status")
def db_status():
    """Show connectivity and migration state."""
    with open_session() as s:
        typer.echo(f"Connected: {s.config.dsn}")
        typer.echo(f"Applied:   {', '.join(applied_versions(s.conn)) or 'none'}")
        typer.echo(f"Pending:   {', '.join(pending_versions(s.conn)) or 'none'}")


@app.command()
def serve():
    """Run the MCP server on stdio (this is what agents launch)."""
    from remem.mcp_server import main as serve_main

    serve_main()


@app.command()
def install(
    agent: str = "claude-code",
    scope: Annotated[str, typer.Option("--scope")] = "user",
):
    """Install remem into an agent (MCP server, hook, and skill)."""
    from remem.agents.registry import UnknownAgent, get as get_adapter

    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    report = adapter.install(scope=scope)
    for action in report.actions:
        typer.echo(f"  {action}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    typer.echo(f"\nInstalled remem for {report.agent}.")


if __name__ == "__main__":
    app()
