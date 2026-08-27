# remem config command - design

Date: 2026-08-27
Status: approved, ready for implementation planning
Builds on: docs/superpowers/specs/2026-08-26-remem-design.md

## Purpose

remem already writes the user's Claude Code configuration - it registers an MCP
server, three hooks, and four skills. But it can only do that once, at install
time, and it can only write the things it installs. Everything else about how
Claude Code behaves in a session - how long a bash command may run, how much
output comes back, when the context auto-compacts - is set by hand in a file
remem is already editing.

At the same time remem's own settings (`REMEM_DSN`, `REMEM_CAPTURE_MODEL`, the
turn-warning thresholds) are documented as environment variables and read from
a `config.toml` that nothing in remem can write.

This design adds one command that reads and writes both:

```
remem config list [--agent claude-code]
remem config get  KEY
remem config set  KEY VALUE
remem config unset KEY
```

### Why this is remem's business at all

Claude Code ships `/config` and settings.json is a documented file anyone can
edit. This command duplicates that, and the duplication is deliberate but not
free: remem takes on a schema Anthropic changes without telling us.

It earns its place on one thing that neither `/config` nor a text editor can
do - **reporting where a value actually comes from, across both tools, with the
two precedence rules made explicit**. See "The precedence asymmetry" below.
That is the feature. The `set` verb is table stakes.

## Requirements

1. One command routes to two files by key name, and never guesses.
2. An unrecognised key is refused, with the supported set named in the error.
3. remem never writes a credential or an endpoint to a file on disk.
4. `list` reports effective value *and* source (environment, file, or default).
5. A write that will be silently shadowed says so.
6. Works with Postgres down. This service touches no store.
7. Fail-loud. It is an explicit user command, not a hook.

## Non-goals

- Project or local settings scope. User scope only, matching the adapter's
  existing `UnsupportedScope` rule for everything but `user`.
- Editing non-`env` settings.json keys - permissions, hooks, model, statusline.
  Hooks are the adapter's job; the rest is `/config`'s.
- Reading or migrating Claude Code's `.claude.json`. That file is described in
  the docs as one Claude Code "writes for itself".
- Any auth, credential, or endpoint variable. See "Excluded by design".
- Preserving comments in `config.toml`. A rewrite loses them; we back up first.

## Layering

Follows the existing seams exactly. Each layer calls downward only.

```
frontend:  cli.py                     `remem config ...` - parses, formats
service:   services/settings.py       routing, validation, precedence, writes
adapter:   agents/claude_code/        the env-var table (data, no policy)
shared:    jsonfile.py                safe read/backup/write of a JSON file
```

**The table lives on the adapter, not in the service.** `BASH_DEFAULT_TIMEOUT_MS`
is a fact about Claude Code, not about remem. Putting it in `services/` would
make the service layer the place that knows every agent's env schema - the exact
coupling `agents/registry.py` exists to prevent. A future Codex adapter ships
its own table and `remem config` works for it with no service change.

`agents/base.py` gains one optional capability:

```python
def env_settings(self) -> Mapping[str, EnvVar]: ...
```

Optional means the service probes for it (`getattr(adapter, "env_settings", None)`)
rather than requiring it: adapters that predate this capability, including any
third-party one already shipped, keep working and report "no settable env vars".
Adding it to the Protocol as a required method would break them at import time,
which the registry contract forbids - a broken third-party adapter must warn,
not break remem.

The **service** owns every policy decision: the routing rule, reject-unknown,
validation, the shadowing check, and backup-before-write.

## Routing

By key name, in this order:

1. `REMEM_*`, or the bare `config.toml` spelling of one -> `config.toml`.
2. A key in the resolved adapter's `env_settings()` -> the `env` block of
   `settings.json`.
3. Anything else -> `UnknownSetting`, naming the supported keys.

remem's own file keys are unprefixed (`max_chars`, not `REMEM_MAX_CHARS`) while
its documentation is written in env-var spelling. Both are accepted as input;
the file always receives the unprefixed spelling.

`settings.json` is located with `resolve_paths()`, so `CLAUDE_CONFIG_DIR` is
honoured here for free.

### Not settable, in either direction

`REMEM_CONFIG` and `CLAUDE_CONFIG_DIR` both name the file that would store
them. Writing either one inside the file it points at is a chicken-and-egg that
reads as broken. Both are rejected with an explanation pointing at the shell.
The Claude Code docs say the same thing about `CLAUDE_CONFIG_DIR`: set it in
the shell, not a settings file.

## The precedence asymmetry

The two targets resolve in **opposite directions**, and this is the single most
confusing thing about the feature:

| Target | Wins |
|---|---|
| remem (`config.py`) | environment variable **over** `config.toml` |
| Claude Code | `settings.json` **over** shell export |

The consequence: `remem config set REMEM_MAX_CHARS 8000` while `REMEM_MAX_CHARS`
is exported writes the file correctly and changes nothing observable. That is a
silent no-op, and this repository has been bitten by silent no-ops repeatedly
(the `XDG_CACHE_HOME` gotcha, the pre-`CLAUDE_CONFIG_DIR` install).

So `set` inspects the live environment for the key it just wrote:

- remem key, present in the environment -> warn that the export shadows the file
  and the new value will not take effect until it is unset.
- Claude Code key, present in the environment -> note the converse: the value
  just written **overrides** that export, which is usually intended.

`list` renders the same information as a `source` column.

## The env-var table

```python
@dataclass(frozen=True, slots=True)
class EnvVar:
    name: str
    kind: Kind          # INT | BOOL | PRESENCE | ENUM | STR
    help: str           # one line; makes `config list` self-documenting
    minimum: int | None = None
    maximum: int | None = None
    default: str | None = None   # documented default, shown when unset
    duration: bool = False       # accepts 10m / 30s as well as raw ms
```

v1 contents, the ergonomics-and-limits set:

| Key | Kind | Range | Default |
|---|---|---|---|
| `BASH_DEFAULT_TIMEOUT_MS` | int, duration | > 0 | 120000 |
| `BASH_MAX_TIMEOUT_MS` | int, duration | > 0 | 600000 |
| `BASH_MAX_OUTPUT_LENGTH` | int | 1-150000 | 30000 |
| `API_TIMEOUT_MS` | int, duration | > 0 | 600000 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | int | 1-100 | - |
| `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` | int, duration | > 0 | 600000 |
| `CLAUDE_AFK_TIMEOUT_MS` | int, duration | >= 0 | - |
| `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR` | bool | - | - |
| `DISABLE_TELEMETRY` | presence | - | - |
| `DISABLE_ERROR_REPORTING` | presence | - | - |

remem's own keys are derived from `Config` and the `DEFAULT_*` constants in
`config.py` rather than duplicated, so a new setting there appears in
`remem config list` automatically.

### Excluded by design

Never in this table, and the exclusion carries a comment saying why so that
nobody helpfully adds them later:

- **Every auth variable** - `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `AWS_BEARER_TOKEN_BEDROCK`, the Foundry and federation keys. remem must never
  be the tool that writes a credential into a JSON file on disk.
- **Every endpoint variable** - the `*_BASE_URL` family. Repointing an agent at
  a different inference endpoint is not a knowledge-store concern, and getting
  it wrong silently exfiltrates prompts.

### Two traps the table has to encode

- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` can only **lower** the built-in default.
  Setting it to 100 is accepted by Claude Code and does nothing. `list` says so
  rather than letting the user believe it took.
- The `DISABLE_*` pair are **presence-only**: any non-empty value enables the
  disabling. So `set DISABLE_TELEMETRY 0` would *disable telemetry*, the
  opposite of how it reads. Presence keys reject `0` and `false` with an error
  pointing at `unset`.

Encoding these is the return on choosing a curated allowlist over passthrough.

## Values

**Duration sugar.** Keys marked `duration` accept `10m`, `30s`, `500ms`, or a
plain integer of milliseconds. The resolved value is echoed back. Six-digit
millisecond literals invite an off-by-one-zero, and the table already knows
which keys are milliseconds.

**Empty string is not unset.** `set KEY ""` writes `"KEY": ""`, which is Claude
Code's documented way to neutralise a shell variable the user does not control.
`unset KEY` removes the key. Distinct operations, distinct results.

**Validation happens before any write.** A rejected value never reaches a file,
and the file's mtime is unchanged.

## Writing the files

**`settings.json`** is edited through the existing safe pattern: read, back up
once, merge, write. Unrelated keys survive; invalid JSON is backed up and
replaced with a warning.

`_read_json`, `_write_json`, and `_backup_once` currently live in `adapter.py`
wired to `InstallReport`. They move to an agent-neutral `remem/jsonfile.py` that
returns warnings instead of mutating a report, and both callers use it. This is
a targeted refactor in service of the current goal: the alternative is a second
copy of the backup logic.

**`config.toml`** is written with `tomli-w`, a new dependency. `tomllib` has no
writer, and the DSN can contain a password with quotes or backslashes - TOML
string escaping is the wrong thing to hand-roll. The file is backed up first,
because a rewrite loses comments and formatting, and the command says where the
backup went.

## Errors

Fail-loud, like `remem handoff write` and unlike every hook in this repository.
A non-zero exit and a message on stderr.

| Condition | Result |
|---|---|
| Unknown key | `UnknownSetting`, listing supported keys |
| `REMEM_CONFIG` / `CLAUDE_CONFIG_DIR` | refused, points at the shell |
| Out of range, wrong type | refused, states the constraint; no write |
| `0`/`false` on a presence key | refused, points at `unset` |
| `--agent` names no registered adapter | refused, lists registered adapters |
| Adapter has no `env_settings()` | reported as "no settable env vars" |

## Testing

No database. Filesystem and pure functions only.

- routing: a `REMEM_*` key reaches `config.toml`, a Claude Code key reaches
  `settings.json`, an unknown key raises and names the supported set;
- both input spellings (`REMEM_MAX_CHARS`, `max_chars`) reach the same file key;
- the shadowing warning fires when the injected environment holds the key just
  written, and stays silent when it does not - both directions;
- `set KEY ""` and `unset KEY` produce different files;
- presence keys reject `0` and `false`;
- range violations reject **and leave the file byte-identical**;
- duration sugar: `10m` resolves to 600000; a bare integer still works; a
  suffix on a non-duration key is refused;
- `settings.json` edits preserve unrelated keys and back up first;
- `config.toml` round-trips a DSN containing a quote and a backslash - the case
  that justified the dependency;
- `CLAUDE_CONFIG_DIR` set redirects the `settings.json` write, via the existing
  `resolve_paths()`;
- everything runs with Postgres unreachable.

Environment is injected, never read from `os.environ` inside a test - the
pattern `config.load()` and now `adapter.install()` already use.

## Open questions

None. Resolved during design:

- Both targets, one command, routed by key name.
- Curated allowlist; unknown keys refused.
- User scope only.
- `tomli-w` over a hand-rolled writer.
- Duration suffixes accepted on millisecond keys.
