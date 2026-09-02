"""Typer frontend. Parses arguments, calls services, formats output.
No decisions about knowledge belong in this file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from uuid import UUID

import psycopg
import typer

from remem.backends.postgres.migrate import applied_versions, migrate, pending_versions
from remem.config import load
from remem.domain import CollectionQuery, Entry, Kind, Match, Origin, Query
from remem.embed import EmbedderUnavailable, load_embedder
from remem.project import resolve_project
from remem.services import ingest as ingest_service
from remem.services import kb, write
from remem.services.embed import backfill
from remem.services.search import find
from remem.session import ensure_database, open_session

app = typer.Typer(help="Knowledge and memory store for AI coding agents.")
db_app = typer.Typer(help="Database setup and status.")
kb_app = typer.Typer(help="Knowledge bases.")
app.add_typer(db_app, name="db")
app.add_typer(kb_app, name="kb")

# Hidden, not removed: `capture` is muscle memory and it is in people's shell
# history. A command that has moved should say where, once, rather than
# failing with a usage error that does not name the new spelling.
capture_app = typer.Typer(help="Deprecated - see `remem record` and `remem events`.")
app.add_typer(capture_app, name="capture", hidden=True)

record_app = typer.Typer(help="Record raw events from a harness.")
app.add_typer(record_app, name="record")

events_app = typer.Typer(help="Extraction and retention for recorded events.")
app.add_typer(events_app, name="events")

handoff_app = typer.Typer(help="Session handoffs.")
app.add_typer(handoff_app, name="handoff")

config_app = typer.Typer(help="remem and agent settings.")
app.add_typer(config_app, name="config")


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
        typer.echo(f"'{value}' is not a valid job id", err=True)
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
    kind: Annotated[Kind, typer.Option("--kind")] = Kind.NOTE,
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
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    archive: Annotated[bool, typer.Option("--archive")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    """Load markdown documents in as searchable, heading-sized entries.

    Re-ingesting is safe and cheap: unchanged sections are skipped entirely,
    edited ones supersede their previous version, and sections that have
    disappeared from the file are superseded by the document's anchor entry
    so nothing is left live and stale.

    --archive stores these documents under the 'archived' origin, which is
    excluded from default search results and reachable with
    `remem search --archived`. Use it for material that is history rather
    than reference - executed implementation plans, for instance.
    """
    resolved = _resolve_project(project, is_global)
    with _session() as s:
        report = ingest_service.ingest_paths(
            s.store, s.owner.id, list(paths),
            project=resolved, archive=archive, dry_run=dry_run,
        )
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.created} new, {report.changed} changed, "
        f"{report.unchanged} unchanged, {report.swept} swept."
    )
    for path, reason in report.failures:
        typer.echo(f"failed: {path}: {reason}", err=True)
    if report.failures:
        # Fail-loud, unlike every hook in this repo: a person typed this.
        raise typer.Exit(1)
    if not dry_run and (report.created or report.changed):
        # Not after a dry run: nothing was written, so there is nothing to
        # embed, and saying otherwise sends the user to a no-op.
        typer.echo("Run `remem embed` to give the new entries vectors.")


@app.command()
def search(
    query: str,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    handoff: Annotated[bool, typer.Option("--handoff")] = False,
    archived: Annotated[bool, typer.Option("--archived")] = False,
):
    """Search stored knowledge.

    Three tiers, tried in order and never blended: exact full-text, then
    entries with related meaning (marked ~), then typo-tolerant matching
    (marked ?). --json reports which as "match".
    --handoff also searches session handoffs, which are excluded by default.
    --archived also searches archived document chunks, which are excluded
    by default.
    """
    with _session() as s:
        # No embedder is passed. The service builds one only if the semantic
        # tier is reached, and decides for itself that an unavailable one is
        # a None rather than an error - both of which are policy, and neither
        # of which a frontend should be restating.
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=list(kind or []), project=project,
                  tags=list(tag or []), limit=limit),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=handoff,
            include_archived=archived,
            semantic_threshold=s.config.semantic_threshold,
            embed_model=s.config.embed_model,
        )
    if as_json:
        payload = []
        for h in hits:
            data = _entry_dict(h.entry, h.snippet)
            data["match"] = str(h.match)
            payload.append(data)
        typer.echo(json.dumps(payload, indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    # Say it once, up front: the caller should know how these were found
    # before reading any of them. Tiers never blend, so hits[0] speaks for
    # the whole result set.
    if hits[0].match is Match.SEMANTIC:
        typer.echo(f"No exact matches for {query!r}. Showing entries with "
                   f"related meaning:\n")
    elif hits[0].match is Match.FUZZY:
        typer.echo(f"No exact or related matches for {query!r}. Showing "
                   f"similar spellings:\n")
    for h in hits:
        marker = {Match.EXACT: "", Match.SEMANTIC: "~ ", Match.FUZZY: "? "}[h.match]
        typer.echo(f"{marker}{h.entry.id}  [{h.entry.kind}] {h.entry.title}")
        typer.echo(f"    {h.snippet}")


@app.command()
def embed(
    limit: Annotated[Optional[int], typer.Option(
        "--limit", help="Stop after this many entries.")] = None,
    batch: Annotated[int, typer.Option("--batch")] = 32,
):
    """Embed entries that have no vector for the configured model.

    Idempotent and safe to re-run - it does whatever is missing. Run it after
    writing entries, or from cron. Changing REMEM_EMBED_MODEL makes every
    entry need embedding again; the old vectors stay until deleted.
    """
    # Built before the session is opened, deliberately: a cron command
    # should fail on a missing embedder without first paying for a Postgres
    # connection. That means this reads config via load() rather than the
    # session's s.config - the two are the same file, just read a moment
    # apart, which is fine for a value (embed_model) nothing else in this
    # command touches concurrently.
    try:
        embedder = load_embedder(load().embed_model)
    except EmbedderUnavailable as exc:
        # Loud, not fail-soft: embedding is this command's entire job, and a
        # silent success would leave search quietly missing a tier forever.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        if not s.store.try_advisory_lock("embed", s.owner.id):
            # A previous run is still going. Silence and exit 0 - a cron
            # command that mails the user about a working system is a cron
            # command they will turn off. The same rule as `events process`:
            # every command in this pipeline is expected to overlap itself.
            raise typer.Exit(0)
        result = backfill(s.store, s.owner.id, embedder,
                          batch_size=batch, max_entries=limit)

    typer.echo(f"Embedded {result.embedded} entries with {result.model}.")
    if result.failed:
        typer.echo(f"{result.failed} failed - re-run to retry.", err=True)
        raise typer.Exit(1)


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


@app.command()
def verify(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Prove an agent's install actually records events, without reinstalling.

    The same live round-trip `install` runs as its own last step - record,
    read back, delete - so a user who wants to re-check after fixing the
    database, or just before trusting the pipeline, does not have to run the
    whole install again to find out.
    """
    from remem.agents.registry import UnknownAgent, get as get_adapter

    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    verify_fn = getattr(adapter, "verify", None)
    if verify_fn is None:
        typer.echo(f"{agent} has no install verification.", err=True)
        raise typer.Exit(1)

    report = verify_fn(env=dict(os.environ), home=Path.home())
    for action in report.actions:
        typer.echo(f"  {action}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    if report.warnings:
        # verify() itself never raises - a failure is a warning on the
        # report, because install() calling it must never die mid-install.
        # But this command is typed by a human asking "does this actually
        # work?", and a report full of warnings that still exits 0 answers
        # that question wrong.
        raise typer.Exit(1)
    typer.echo(f"\nVerified {report.agent}.")


@app.command("doctor")
def doctor(
    agent: Annotated[Optional[str], typer.Argument()] = None,
    # No default scope. `--scope` unset means "look everywhere this adapter
    # can be installed" - the service sweeps - because defaulting to user
    # scope made this command report cursor "not installed" on a machine
    # where cursor was installed at project scope and recording events. An
    # unexamined scope must never produce a confident answer about it.
    scope: Annotated[Optional[str], typer.Option("--scope")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Check that each harness's config registers the hooks remem installs.

    The gap this closes: hooks are fail-soft, so a harness that was never
    told to call remem looks exactly like one with nothing to say. `remem
    verify` proves remem records when called; this asks whether the harness
    will ever call it. Neither answers the other's question.

    Opens no database connection - a diagnostic that needs the system
    healthy is no use when it is not.
    """
    from remem.agents import registry
    from remem.services import doctor as doctor_service

    adapters = registry.discover()
    if agent is not None:
        if agent not in adapters:
            known = ", ".join(sorted(adapters)) or "none"
            typer.echo(f"unknown agent '{agent}'. Available: {known}")
            raise typer.Exit(1)
        adapters = {agent: adapters[agent]}

    reports = doctor_service.check(
        adapters, scope=scope, home=Path.home(), env=dict(os.environ)
    )
    if as_json:
        typer.echo(json.dumps(doctor_service.to_dict(reports), indent=2))
    else:
        typer.echo(doctor_service.render(reports))
    raise typer.Exit(1 if doctor_service.failed(reports) else 0)


def _message(exc: Exception) -> str:
    """An exception's message, without KeyError's repr quotes.

    UnknownSetting subclasses KeyError, and KeyError stringifies as the repr
    of its argument, so str() would render a sentence wrapped in quotes.
    Reaching for args[0] is exact; stripping quote characters off both ends
    of the rendered string would also mangle a message that legitimately
    ends in a quoted key name.
    """
    return exc.args[0] if isinstance(exc, KeyError) else str(exc)


def _config_targets(agent: str):
    """Resolve --agent to an adapter and ask the service where its files are.

    Everything this function decides is a parsing decision: the name of the
    agent, and what to print when there is no such agent. Which file that
    agent's environment block lives in, and what a missing capability means,
    are the service's calls - hard-wiring one adapter's resolver here is how
    `--agent codex` would end up writing into ~/.claude/settings.json.
    """
    import os
    from pathlib import Path

    from remem.agents.registry import UnknownAgent, get as get_adapter
    from remem.services import settings as svc

    env = os.environ
    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        # registry.get already lists what is registered, so echo it as-is.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    targets = svc.resolve_targets(adapter, Path.home(), env)
    return env, targets.remem_path, targets.agent_path, targets.table


@config_app.command("set")
def config_set(
    key: str,
    value: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Set one setting, in remem's config or the agent's environment."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
        resolved = svc.coerce(var, value, target)
    except (svc.UnknownSetting, svc.NotSettable, svc.InvalidValue) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    if target is svc.Target.REMEM:
        backup = svc.write_remem(remem_path, var.name, resolved)
        where = remem_path
    else:
        backup = svc.write_agent(agent_path, var.name, resolved)
        where = agent_path

    typer.echo(f"{var.name} = {resolved!r} in {where}")
    # Rewriting either file loses whatever was not a setting - comments and
    # formatting in config.toml, key order in settings.json. The copy is
    # taken automatically, so the only thing left to get wrong is not saying
    # where it went; a timestamped .bak name is not one anybody would guess.
    if backup is not None:
        typer.echo(f"Backed up to {backup}")
    warning = svc.shadow_warning(target, var.name, env)
    if warning:
        typer.echo(warning, err=True)


@config_app.command("unset")
def config_unset(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Remove one setting, restoring its default."""
    from remem.services import settings as svc

    _, remem_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    if target is svc.Target.REMEM:
        backup = svc.write_remem(remem_path, var.name, None)
    else:
        backup = svc.write_agent(agent_path, var.name, None)
    typer.echo(f"Unset {var.name}.")
    if backup is not None:
        typer.echo(f"Backed up to {backup}")


@config_app.command("get")
def config_get(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print one setting's effective value and where it came from."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    try:
        _, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    rows = svc.list_settings(remem_path, agent_path, table, env)
    row = next(r for r in rows if r.key == var.name)
    typer.echo(f"{row.value if row.value is not None else '(unset)'}\t{row.source}")


@config_app.command("list")
def config_list(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Show every settable key, its value, and where that value came from."""
    from remem.services import settings as svc

    env, remem_path, agent_path, table = _config_targets(agent)
    if not table:
        typer.echo(f"{agent} has no settable environment variables.")
    for row in svc.list_settings(remem_path, agent_path, table, env):
        value = row.value if row.value is not None else "(unset)"
        typer.echo(f"{row.key}\t{value}\t{row.source}\t{row.var.help}")
        if row.var.note:
            typer.echo(f"\t{row.var.note}")


hook_app = typer.Typer(help="Agent hook entry points (not for interactive use).")
app.add_typer(hook_app, name="hook")


@hook_app.command("session-start")
def hook_session_start():
    """Print the project's knowledge base. Always exits 0."""
    from remem.agents.claude_code.hook import main as hook_main

    raise typer.Exit(hook_main())


@hook_app.command("session-end")
def hook_session_end():
    """Record this SessionEnd payload as an event. Always exits 0.

    The command name is kept from before the idle trigger existed - an
    already-installed settings.json names it, and a hook command that no
    longer exists is an error on every session close. It now does exactly
    what `remem hook record-event` does: extraction runs on an idle timer,
    so this just shortens the wait rather than being required for it.
    """
    from remem.agents.claude_code.hook import main_record_event

    raise typer.Exit(main_record_event())


@hook_app.command("record-event")
def hook_record_event():
    """Record this PostToolUse or SessionEnd payload as an event. Always exits 0."""
    from remem.agents.claude_code.hook import main_record_event

    raise typer.Exit(main_record_event())


@hook_app.command("session-size")
def hook_session_size():
    """Warn when a session has grown long enough to hand off. Always exits 0."""
    from remem.agents.claude_code.hook import main_session_size

    raise typer.Exit(main_session_size())


@hook_app.command("context")
def hook_context(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print the knowledge base block for the session on stdin.

    The harness-neutral half of what SessionStart does for Claude Code. A
    harness with no session-start hook - opencode, Cursor - calls this
    instead, passing whatever payload it has; the adapter's identity()
    turns that into a project.

    Fail-soft like every hook: exits 0 unconditionally, and prints the
    block and nothing else to stdout. Reasons go to stderr under
    REMEM_HOOK_DEBUG.
    """
    from remem import hookio
    from remem.agents import registry
    from remem.hookio import debug
    from remem.services import context

    env = dict(os.environ)

    try:
        stdin_text = sys.stdin.read()
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            debug(env, "stdin was not valid JSON")
            raise typer.Exit(0)

        try:
            adapter = registry.get(agent)()
        except registry.UnknownAgent as exc:
            debug(env, str(exc))
            raise typer.Exit(0)

        # identity() is a required Protocol method, unlike event() - but a
        # third-party adapter whose implementation raises must still only
        # cost the block, not the hook, so it is caught exactly like the
        # optional capabilities in services/settings.py are.
        try:
            identity = adapter.identity(env, payload)
        except Exception as exc:
            debug(
                env,
                f"{agent} adapter's identity() raised {type(exc).__name__}: {exc}",
            )
            raise typer.Exit(0)

        if not identity.project:
            debug(env, "the hook payload carried no project")
            raise typer.Exit(0)

        cfg = load()
        with open_session(cfg) as s:
            rendered = context.block(
                s.store,
                s.owner.id,
                identity.project,
                cfg.max_chars,
                note=lambda reason: debug(env, reason),
                owner_handle=s.owner.handle,
            )

        # Delivery is the adapter's business, not the frontend's. An
        # adapter with no inject() is one whose harness reads stdout, which
        # is the default and not a degradation - see the Protocol comment
        # in agents/base.py. A capability that raises degrades to the
        # stdout path and a debug line, never to a broken hook.
        inject = getattr(adapter, "inject", None)
        if inject is not None:
            try:
                written = inject(rendered, payload, note=lambda reason: debug(env, reason))
            except Exception as exc:
                debug(
                    env,
                    f"{agent} adapter's inject() raised "
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                if written:
                    debug(env, f"wrote the context block to {written}")
                else:
                    debug(env, "the adapter wrote no context block")
                raise typer.Exit(0)

        typer.echo(rendered, nl=False)
    except typer.Exit:
        raise
    except Exception as exc:
        debug(env, f"{type(exc).__name__}: {exc}")
    finally:
        # Extraction is triggered here for every harness that has no
        # session-start hook of its own - opencode and Cursor both call this
        # command once per session, which makes it the one trigger all three
        # harnesses share. Claude Code does the same from SessionStart.
        #
        # In `finally`, so it runs on every path above, including the early
        # returns for unusable stdin, an unknown agent, and an unresolvable
        # project. The backlog is global: `remem events process` works off
        # every extractable session for the owner, so whether THIS payload
        # produced a block says nothing about whether extraction has work.
        #
        # A harness with no `claude` on PATH will fail these jobs rather than
        # silently skip them - the attempt cap stops the retries and
        # `remem record status` shows the reason, which beats a probe here
        # that guesses wrong about where the extractor lives.
        hookio.spawn_process(env)

    raise typer.Exit(0)


@capture_app.command("enable", hidden=True)
def capture_enable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Deprecated: use `remem record enable`."""
    # stderr: a script or crontab still calling the old name should not gain
    # a permanent line of noise on stdout, once per run, forever.
    typer.echo("`capture enable` is renamed to `remem record enable`.", err=True)
    from remem.services import record

    name = project or _default_project()
    with _session() as s:
        record.enable(s.store, s.owner.id, name)
        model = s.config.extract_model
    typer.echo(f"capture enabled for '{name}'")
    # State the cost at the moment the tradeoff is actionable. Measured on a
    # real session: roughly 20 cents per extraction on sonnet, a third of
    # that on haiku - which returned noticeably worse judgement about what was
    # worth keeping. This deprecated command keeps naming the deprecated
    # variable - `remem record enable` is where the current name is taught.
    typer.echo(
        f"extraction runs `claude -p --model {model}` once per session, "
        f"roughly $0.10-0.25 each.\n"
        f"change it with REMEM_CAPTURE_MODEL (e.g. haiku for less, "
        f"opus for more)."
    )


@capture_app.command("disable", hidden=True)
def capture_disable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Deprecated: use `remem record disable`."""
    typer.echo("`capture disable` is renamed to `remem record disable`.", err=True)
    from remem.services import record

    name = project or _default_project()
    with _session() as s:
        record.disable(s.store, s.owner.id, name)
    typer.echo(f"capture disabled for '{name}'")


@capture_app.command("status", hidden=True)
def capture_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Deprecated: use `remem record status`."""
    # stderr: --json below must stay parseable on its own, and this is the
    # same reasoning as the other three aliases besides.
    typer.echo("`capture status` is renamed to `remem record status`.", err=True)
    with _session() as s:
        counts = s.store.extract_job_counts(s.owner.id)
        failures = s.store.recent_failed_extract_jobs(s.owner.id)
        model = s.config.extract_model
        projects = s.store.enabled_record_projects(s.owner.id)

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

    typer.echo(f"Recording enabled for: {', '.join(projects) or 'no projects'}")
    typer.echo(f"Extraction model: {model}")
    if counts:
        typer.echo("Jobs: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        typer.echo("Jobs: none yet")
    for f in failures:
        typer.echo(f"  failed {f.id} [{f.project}]: {f.error}")


@capture_app.command("drain", hidden=True)
def capture_drain(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    job: Annotated[Optional[str], typer.Option("--job")] = None,
):
    """Deprecated: use `remem events process`."""
    # stderr - see capture_enable's comment: `capture drain` is exactly the
    # kind of command an old crontab still calls, unattended, forever.
    typer.echo("`capture drain` is renamed to `remem events process`.", err=True)
    events_process(limit=limit, job=job)


@events_app.command("process")
def events_process(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    job: Annotated[Optional[str], typer.Option("--job")] = None,
):
    """Extract entries from sessions that have gone quiet.

    A session is extracted once it has been idle for REMEM_IDLE_MINUTES.
    Safe to run from cron: overlapping runs are held off by an advisory
    lock, and a run that finds nothing says so and exits 0.

    `--job ID` retries exactly that job, however many times it has already
    failed. It is the only way back for a job that hit the attempt cap.
    """
    from remem.extract.claude_cli import ClaudeCliExtractor
    from remem.services import extraction

    job_id = _job_id(job) if job is not None else None

    # Autocommit, unlike every other command: the run records its own
    # progress as it goes, and it spends minutes at a time inside `claude`.
    # One transaction for the batch would both discard already-succeeded work
    # on a database error and hold row locks across those minutes.
    with _session(autocommit=True) as s:
        if not s.store.try_advisory_lock("events-process", s.owner.id):
            # A previous run is still going. Silence and exit 0 - a cron
            # command that mails the user about a working system is a cron
            # command they will turn off.
            raise typer.Exit(0)
        extractor = ClaudeCliExtractor(model=s.config.extract_model)
        if job_id is not None:
            try:
                report = extraction.process_job(s.store, s.owner.id, job_id,
                                                extractor)
            except extraction.ExtractJobNotFound as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)
        else:
            report = extraction.process(
                s.store, s.owner.id, extractor,
                idle_seconds=s.config.idle_minutes * 60, limit=limit,
            )
    typer.echo(
        f"claimed {report.claimed}, succeeded {report.succeeded}, "
        f"failed {report.failed}, entries written {report.entries_written}"
    )


@events_app.command("prune")
def events_prune(
    before: Annotated[
        Optional[str], typer.Option("--before", help="e.g. 30d, 12h, 90m")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="only this project (default: all)"),
    ] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Delete raw events older than a window, if they have been extracted.

    There is no default window: `--before` must always be something the
    user typed, so a configured retention number can never quietly delete
    history. An event that has not been extracted yet is never deleted
    unless `--force` says so - for a session whose extraction is never
    going to finish.

    `--project` narrows the run to one project. Recording is opt-in per
    project, so the delete reaches the same granularity - otherwise the
    only way to drop one project's events is to delete every project's.
    It does NOT default to the current directory the way the writing
    commands do: every other `--project` here narrows a read or files a
    new row, and this one deletes, so the scope has to be typed.
    """
    from remem.services import events

    if before is None:
        typer.echo(
            "--before is required (e.g. --before 30d) - there is no default "
            "retention window.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        window = events.parse_window(before)
    except events.BadWindow as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        cutoff = datetime.now(timezone.utc) - window
        try:
            report = events.prune(
                s.store, s.owner.id, before=cutoff, force=force, project=project
            )
        except events.PruneRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps({
            "deleted": report.deleted,
            "kept_unextracted": report.kept_unextracted,
            "dangling": report.dangling,
            "project": project,
        }, indent=2))
        return
    # The dangling count prints even when it is zero - its absence would be
    # indistinguishable from a prune that never reported it at all.
    # The scope is named on every run, including the unscoped one. A
    # destructive command that says only what it deleted leaves the user to
    # guess whether it hit one project or all of them.
    scope = f"project '{project}'" if project else "all projects"
    typer.echo(
        f"deleted {report.deleted} events from {scope}, kept "
        f"{report.kept_unextracted} unextracted, left {report.dangling} "
        f"provenance rows dangling"
    )


@events_app.command("show")
def events_show(
    entry_id: Annotated[str, typer.Argument()],
):
    """Show the raw events an entry was extracted from - the forensic lookup.

    No provenance at all is an ordinary answer for a hand-written entry, not
    an error. A pruned event is reported as "event pruned", never "not
    found" - those mean different things and look identical to a user who
    is only told one of them.
    """
    from remem.services import events

    eid = _entry_id(entry_id)
    with _session() as s:
        rows = events.forensics(s.store, s.owner.id, eid)
    typer.echo(events.render_provenance(rows))


@record_app.command("status")
def record_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show what's been recorded, per harness, and what's stuck.

    This is the fail-loud half of a fail-soft pipeline: hooks and the idle
    trigger never raise, so this is the one place a harness that has quietly
    recorded nothing - wrong hook names, a broken adapter, opt-in never
    turned on - becomes visible on demand rather than never.
    """
    from remem.agents import registry
    from remem.services import doctor as doctor_service
    from remem.services import events

    # Wrapped: an unreadable config file must not take down a status
    # command that is otherwise about the database. doctor.check already
    # degrades per adapter; this covers the registry call itself.
    #
    # No scope is passed, so this sweeps every scope - the same question
    # `remem doctor` with no arguments asks. It has to: the harness this
    # advisory exists for is the one that has recorded nothing ever, and on
    # the machine that motivated the feature that harness is Cursor at
    # project scope, which a user-scope-only check cannot see at all.
    try:
        advisories = doctor_service.advisories(
            doctor_service.check(registry.discover(), env=dict(os.environ))
        )
    except Exception:
        advisories = []

    with _session() as s:
        report = events.status(
            s.store, s.owner.id, idle_seconds=s.config.idle_minutes * 60,
            hook_advisories=advisories,
        )

    if as_json:
        typer.echo(json.dumps(events.to_dict(report), indent=2))
        return
    typer.echo(events.render(report))


@record_app.command("enable")
def record_enable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn on event recording for a project (defaults to this directory)."""
    from remem.services import record

    name = project or _default_project()
    with _session() as s:
        record.enable(s.store, s.owner.id, name)
        model = s.config.extract_model
    typer.echo(f"recording enabled for '{name}'")
    # Same cost note as capture's, and for the same reason: state the
    # tradeoff at the moment it is actionable.
    typer.echo(
        f"extraction runs `claude -p --model {model}` once per session, "
        f"roughly $0.10-0.25 each.\n"
        f"change it with REMEM_EXTRACT_MODEL (e.g. haiku for less, "
        f"opus for more)."
    )


@record_app.command("disable")
def record_disable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn off event recording for a project."""
    from remem.services import record

    name = project or _default_project()
    with _session() as s:
        record.disable(s.store, s.owner.id, name)
    typer.echo(f"recording disabled for '{name}'")


@record_app.command("event")
def record_event(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
    strict: Annotated[bool, typer.Option("--strict")] = False,
):
    """Record one harness event read from stdin.

    A hook entry point in everything but name: it runs once per tool call,
    so it exits 0 unconditionally and prints nothing to stdout. Every early
    return explains itself through the same REMEM_HOOK_DEBUG channel the
    Claude Code hooks use - see remem/hookio.py.

    The one loud case is unreadable stdin, and only when a human is
    plausibly the one who typed the command: interactively, or with
    --strict. From a hook, even that stays silent.
    """
    from remem.agents import registry
    from remem.hookio import debug
    from remem.services import record

    env = dict(os.environ)
    loud = strict or sys.stdin.isatty()
    stdin_text = sys.stdin.read()

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        debug(env, "stdin was not valid JSON")
        if loud:
            typer.echo("stdin was not valid JSON", err=True)
            raise typer.Exit(1)
        raise typer.Exit(0)

    try:
        adapter = registry.get(agent)()
    except registry.UnknownAgent as exc:
        debug(env, str(exc))
        raise typer.Exit(0)

    # event() is an optional capability, probed exactly like
    # env_settings()/settings_path() in services/settings.py: an adapter
    # that lacks it, or whose implementation raises, must never be why
    # recording stops - it just cannot record, which is what "None" and a
    # caught exception both mean here.
    event_of = getattr(adapter, "event", None)
    if event_of is None:
        debug(env, f"agent '{agent}' does not support recording events")
        raise typer.Exit(0)

    try:
        harness_event = event_of(env, payload)
    except Exception as exc:
        debug(
            env,
            f"{agent} adapter's event() raised {type(exc).__name__}: {exc}",
        )
        raise typer.Exit(0)

    if harness_event is None:
        debug(env, "payload was not an event worth recording")
        raise typer.Exit(0)

    try:
        cfg = load()
        with open_session(cfg) as s:
            result = record.record(s.store, s.owner.id, harness_event, adapter.name)
    except Exception as exc:
        debug(env, f"{type(exc).__name__}: {exc}")
        raise typer.Exit(0)

    if result is None:
        debug(
            env,
            f"recording is not enabled for project "
            f"{harness_event.project!r}. Enable it with `remem record "
            f"enable --project {harness_event.project}`.",
        )
    raise typer.Exit(0)


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
    if not name:
        # No project to query at all - a printed one-liner, not a reason to
        # open a database connection (and, if Postgres is down, an error).
        typer.echo("No handoff stored for this directory.")
        return
    with _session() as s:
        try:
            entry = handoff_svc.latest(s.store, s.owner.id, project=name, topic=topic)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
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
