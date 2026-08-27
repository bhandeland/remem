"""FastMCP frontend. Seven tools; every extra one is context the agent pays
for on every turn. Tool docstrings say WHEN to reach for each tool - that
text is the only thing steering agent behaviour."""

from __future__ import annotations

import os
from uuid import UUID

# The installed mcp package is 2.x, where `FastMCP` was renamed to
# `MCPServer` and moved to `mcp.server.mcpserver` (mcp.server.fastmcp now
# raises ModuleNotFoundError on import, pointing at this rename). The class's
# tool-registration and `list_tools()` API is unchanged from v1, so nothing
# else in this module needed to change - only this import.
from mcp.server.mcpserver import MCPServer

from remem.domain import Kind, Origin, Query
from remem.project import resolve_project
from remem.services import kb, write
from remem.services.search import find
from remem.session import open_session

mcp = MCPServer("remem")

AGENT_NAME = "claude-code"


def _default_project() -> str | None:
    """The project an agent is working in, from the server's own directory.

    Claude Code starts the MCP server in the session's directory, so this
    matches the knowledge base the SessionStart hook injects. Without it, an
    agent that omits `project` writes an entry with none - which the project's
    knowledge base will never surface, even though the write succeeded.

    Resolved from the git repository rather than the directory name, so a
    subdirectory or a worktree still files under the project it belongs to.
    """
    return resolve_project()


def _session_id() -> str | None:
    # MCP tool calls carry no session id; use one only if the environment
    # supplies it. Recording null is better than fabricating a value.
    return os.environ.get("CLAUDE_SESSION_ID")


def _invalid_kind_message(kind: str) -> str:
    valid = ", ".join(k.value for k in Kind)
    return f"invalid kind '{kind}'; expected one of: {valid}"


@mcp.tool(name="remember")
def remember_tool(
    title: str,
    body: str,
    kind: str = "memory",
    project: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Store something worth knowing later.

    Use when you learn a durable fact about this project, write down a
    convention that should be followed, or capture reference material. Do
    NOT use for transient details of the current task.

    project: omit it and this session's project is used, which is what the
    knowledge base injected at session start queries on. Only pass it to file
    something under a different project.

    kind: "memory" (something learned), "doc" (reference material), or
    "rule" (a convention that must be followed - these are always injected
    into future sessions).
    """
    try:
        parsed_kind = Kind(kind)
    except ValueError:
        return {"error": _invalid_kind_message(kind)}
    with open_session() as s:
        entry = write.remember(
            s.store, s.owner.id, title=title, body=body, kind=parsed_kind,
            project=project or _default_project(),
            tags=list(tags or []), agent=AGENT_NAME,
            session_id=_session_id(), origin=Origin.AGENT,
        )
        return {"id": str(entry.id), "title": entry.title}


@mcp.tool(name="recall")
def recall_tool(
    query: str,
    kind: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    include_handoffs: bool = False,
) -> list[dict] | dict:
    """Search stored knowledge before assuming something is unknown.

    Use at the start of work on an unfamiliar area, when the user refers to
    a past decision, or before re-deriving something. Returns snippets and
    ids; call get_entry for anything worth reading in full.

    kind: optional filter - "memory", "doc", or "rule". Omit to search
    across all kinds.

    include_handoffs: session handoffs are excluded by default because a
    project accumulates many of them. Pass true when resuming a workstream
    and looking for where it was left.

    If nothing matches exactly, this falls back to typo-tolerant matching and
    every result carries "fuzzy": true. Treat those as approximate: they may
    be what you meant, but do not cite them as certain without reading the
    entry in full via get_entry.
    """
    kinds: list[Kind] = []
    if kind is not None:
        try:
            kinds = [Kind(kind)]
        except ValueError:
            return {"error": _invalid_kind_message(kind)}
    with open_session() as s:
        hits = find(
            s.store, s.owner.id,
            Query(text=query, kinds=kinds,
                  project=project, tags=list(tags or []), limit=limit),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=include_handoffs,
        )
        return [
            {
                "id": str(h.entry.id),
                "title": h.entry.title,
                "kind": str(h.entry.kind),
                "project": h.entry.project,
                "tags": list(h.entry.tags),
                "snippet": h.snippet,
                "fuzzy": h.fuzzy,
            }
            for h in hits
        ]


@mcp.tool(name="get_entry")
def get_entry_tool(entry_id: str) -> dict:
    """Read one entry in full, by id from a recall result.

    Use when a recall snippet looks relevant and you need the whole text.
    """
    with open_session() as s:
        try:
            entry = s.store.get_entry(UUID(entry_id), s.owner.id)
        except ValueError:
            return {"error": f"'{entry_id}' is not a valid entry id"}
        if entry is None:
            return {"error": f"no entry {entry_id}"}
        return {
            "id": str(entry.id),
            "title": entry.title,
            "kind": str(entry.kind),
            "body": entry.body,
            "project": entry.project,
            "tags": list(entry.tags),
        }


@mcp.tool(name="supersede")
def supersede_tool(entry_id: str, title: str, body: str) -> dict:
    """Replace knowledge that stopped being true.

    Use when you discover a stored entry is now wrong or out of date. The
    old entry is kept but stops appearing in searches. Prefer this over
    storing a contradicting second memory.
    """
    with open_session() as s:
        try:
            entry = write.supersede(
                s.store, s.owner.id, UUID(entry_id), title=title, body=body
            )
        except (write.EntryNotFound, ValueError):
            return {"error": f"no entry {entry_id}"}
        return {"id": str(entry.id), "replaced": entry_id}


@mcp.tool(name="kb_context")
def kb_context_tool(slug: str, max_chars: int | None = None) -> str:
    """Render a knowledge base as a context block.

    Use when starting work that a knowledge base covers, to load its rules
    and accumulated knowledge at once instead of searching repeatedly.
    """
    with open_session() as s:
        try:
            collection = kb.get(s.store, s.owner.id, slug)
            entries = kb.resolve(s.store, s.owner.id, slug)
            return kb.render(
                collection, entries, max_chars or s.config.max_chars
            )
        except kb.CollectionNotFound:
            return f"No knowledge base '{slug}'. Call kb_list to see what exists."
        except kb.RulesExceedBudget as exc:
            return f"Knowledge base '{slug}' needs pruning: {exc}"


@mcp.tool(name="kb_list")
def kb_list_tool() -> list[dict]:
    """List available knowledge bases and what each covers."""
    with open_session() as s:
        return [
            {"slug": c.slug, "title": c.title,
             "description": c.description, "project": c.project}
            for c in s.store.list_collections(s.owner.id)
        ]


@mcp.tool(name="kb_pin")
def kb_pin_tool(slug: str, entry_id: str) -> dict:
    """Curate an entry into a knowledge base so it is always included.

    Use when an entry is important enough that it should appear in the
    knowledge base regardless of search ranking.
    """
    with open_session() as s:
        try:
            kb.pin(s.store, s.owner.id, slug, UUID(entry_id))
        except (kb.CollectionNotFound, ValueError):
            return {"error": f"could not pin {entry_id} to '{slug}'"}
        except kb.EntryNotFound:
            return {"error": f"no entry {entry_id}"}
        return {"pinned": entry_id, "slug": slug}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
