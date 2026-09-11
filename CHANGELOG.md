# Changelog

Notable changes to remem. Versions follow [semantic versioning](https://semver.org),
against the surfaces named in the compatibility section of the README - which is
the part of this project a version number is a promise about.

## [Unreleased]

## [0.9.0] - unreleased

The first published release. remem has been in daily use on the machine that
built it since 2026-08-26; this is the point at which it is packaged for
anyone else. The version is deliberately not 1.0: the publish path itself has
never run, and a release that has never been installed from an index is not a
release anybody should be promised compatibility with.

### Knowledge

- `Entry` (note, doc, or rule) as the unit of knowledge, filed per project,
  with tags and an origin recording who wrote it.
- Collections ("knowledge bases") as a smart query plus explicitly pinned
  entries. An empty query matches nothing, and `kb` says so at creation.
- Three-tier search - exact full-text, then semantic, then trigram - each
  tier running only when the one above returned nothing. Every hit carries
  how it matched, and every frontend shows it, so an approximate result is
  never cited as certain.
- Semantic search through a local embedder, as an optional `[embed]` extra.
  Missing it costs the middle tier and nothing else.
- `remem dedupe report` for entries that say the same thing twice, and
  `remem dedupe resolve` to point one at another. Nothing is ever merged
  automatically.

### Context

- A context block injected at session start, carrying rules as title plus
  summary rather than their full bodies.
- Rules are never truncated. A knowledge base whose rules outgrow the budget
  raises rather than shipping a partial rule set, because an agent given part
  of the conventions proceeds believing it has all of them.
- `remem record status` now reports a knowledge base whose rules are
  crowding the budget, including a warning before the hard limit. Every hook
  is fail-soft by contract, so without this the failure was invisible: context
  injection stopped in every session with a zero exit and no output.
- Session handoffs, for carrying state across a `/clear`.

### Harnesses

- Adapters for Claude Code, opencode, and Cursor, registered through the
  `remem.agents` entry point and loaded lazily. A broken third-party adapter
  warns rather than breaking remem.
- Per-tool-call event recording, opt-in per project, extracted into entries
  later by an idle-triggered background pass.
- `remem doctor` to answer whether the installed configuration actually
  registers the hooks an adapter installs - a diagnostic that reads files and
  opens no database, so it still works when the system does not.
- `remem verify` to answer whether remem works when called. doctor and verify
  are a pair; neither subsumes the other.

### Documents

- `remem ingest` for markdown, one entry per heading plus an anchor per file,
  idempotent on re-ingest. Automatic re-ingest for designated paths.
- `remem memory sync` to own Claude Code's file-based memory directory as a
  generated view of a collection, adopting anything on disk it has not seen
  before it regenerates.
- `remem import claude-mem` to read claude-mem's sqlite file directly,
  reading both its pre-33 and modern schemas and naming every row it could
  not carry across.

### Interfaces

- `remem`, a Typer CLI, with `--json` on the commands worth scripting.
- An MCP server over stdio, and over streamable-HTTP with `--http` for an
  agent that cannot start a local process. The HTTP listener has no
  authentication - see the README.
- `remem config` to read and write remem's own settings and a curated set of
  Claude Code environment variables, backing up every file it writes. No
  credential or endpoint variable is settable.

### Storage

- Postgres 18 with pgvector, behind a `Store` protocol. Numbered forward-only
  migrations, applied by `remem db up` and never by anything else.
