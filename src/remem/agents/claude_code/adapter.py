"""Installs remem into Claude Code: MCP server, hooks, and bundled skills."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Mapping

from remem import jsonfile
from remem.agents.base import (
    RECORD_NOTE,
    EnvVar,
    ExpectedHook,
    HarnessEvent,
    Identity,
    InstallReport,
    UnsupportedScope,
)
from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.agents.verify import VERIFY_PROJECT, round_trip
from remem.domain import EventKind
from remem.project import resolve_project

HOOK_COMMAND = "remem hook session-start"
RECORD_EVENT_COMMAND = "remem hook record-event"
SESSION_SIZE_COMMAND = "remem hook session-size"

#: Commands a previous remem wrote for a hook, which this install migrates in
#: place. `remem hook session-end` predates the idle trigger and survives as a
#: back-compat alias running exactly what `record-event` runs (see cli.py), so
#: a settings.json written before the events pipeline names it. Merely
#: *tolerating* it is not enough: the membership test below would still not
#: find the canonical command, would append beside the alias, and both would
#: fire - `events` has no unique constraint, so every session close would
#: write a duplicate row for the extractor to read twice. Rewriting is also
#: what stops the alias from having to live forever.
LEGACY_COMMANDS: dict[str, tuple[str, ...]] = {
    "SessionEnd": ("remem hook session-end",),
}

# VERIFY_PROJECT used to be defined here; it now lives in remem.agents.verify,
# shared with every adapter's round-trip. Re-exported, not used here:
# tests/test_claude_code_events_install.py still imports it from this
# module. Naming it in __all__ is what makes that deliberate rather than a
# stray import a linter should remove.
__all__ = ["ClaudeCodeAdapter", "VERIFY_PROJECT"]

SLUG_CONVENTION = (
    "The SessionStart hook injects the knowledge base whose slug matches the "
    "session's repository name - create one with `remem kb new <repo-name>`. A subdirectory or a worktree resolves to the same name."
)

HANDOFF_NOTE = (
    "Long sessions get a handoff reminder at 150 turns, then every 50 - "
    "tune it with REMEM_TURN_WARN_AT and REMEM_TURN_WARN_EVERY."
)

CONFIG_NOTE = (
    "Tune remem and Claude Code together with `remem config list` - it shows "
    "every setting, its value, and whether that value came from the "
    "environment, a file, or a default."
)


CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"


@dataclass(frozen=True, slots=True)
class ClaudePaths:
    """The three files an install writes, and where CLAUDE_CONFIG_DIR moves them.

    Transcribed from the Claude Code binary (checked against 2.1.247), which
    resolves the two roots from the same variable but not in the same way:

        settings/skills  ->  CLAUDE_CONFIG_DIR || join(home, ".claude")
        .claude.json     ->  join(CLAUDE_CONFIG_DIR || home, ".claude.json")

    Set the variable and all three collapse into it. Leave it unset and
    .claude.json sits *beside* ~/.claude rather than inside it. Deriving
    global_json from config_dir would therefore be wrong in the common case,
    which is why both roots are kept.

    Getting any of this wrong fails silently: the hooks are fail-soft by
    contract and an unregistered MCP server just never starts.
    """

    config_dir: Path
    global_json: Path
    relocated: bool

    @property
    def settings(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def skills(self) -> Path:
        return self.config_dir / "skills"


def resolve_paths(home: Path, env: Mapping[str, str]) -> ClaudePaths:
    # An empty value counts as unset. Path("") is the current working
    # directory, so honouring it would scatter an install wherever the user
    # happened to be standing.
    configured = env.get(CONFIG_DIR_VAR, "").strip()
    if configured:
        root = Path(configured)
        return ClaudePaths(root, root / ".claude.json", relocated=True)
    return ClaudePaths(home / ".claude", home / ".claude.json", relocated=False)


@dataclass(frozen=True, slots=True)
class ClaudeHook:
    """One hook entry, as this adapter installs it.

    Carries `timeout`, which `ExpectedHook` deliberately does not: it is a
    fact about Claude Code's hook runner and means nothing to another
    harness. `expected()` projects it away.
    """

    event: str
    command: str
    timeout: int
    required: bool
    provides: str

    def expected(self) -> ExpectedHook:
        return ExpectedHook(
            event=self.event,
            command=self.command,
            required=self.required,
            provides=self.provides,
        )


#: The one table. `_install_hook` writes from it and `hook_state` checks
#: against it, so the installer and the check cannot disagree about which
#: hooks exist - the same reason `extraction.awaiting_sessions` is the only
#: place the attempt-cap rule is evaluated.
HOOK_ENTRIES: tuple[ClaudeHook, ...] = (
    ClaudeHook(
        "SessionStart", HOOK_COMMAND, 10, True,
        "context injection, and the spawn that drains the extraction backlog",
    ),
    # A hint, not a requirement: extraction runs on an idle timer now, so a
    # harness with no SessionEnd loses no events at all - only the
    # promptness of the timer. It records through the same command and the
    # same event() mapping as PostToolUse.
    ClaudeHook(
        "SessionEnd", RECORD_EVENT_COMMAND, 10, False,
        "a prompt end-of-session record; the idle timer covers it either way",
    ),
    # Runs once per tool call and does one INSERT, so it gets the short
    # budget UserPromptSubmit has, not the 10s SessionStart needs.
    ClaudeHook(
        "PostToolUse", RECORD_EVENT_COMMAND, 5, True,
        "every tool call - without it nothing is recorded at all",
    ),
    # Runs on every prompt, so it gets the shortest timeout of the four; it
    # reads one file and never opens Postgres.
    ClaudeHook(
        "UserPromptSubmit", SESSION_SIZE_COMMAND, 5, False,
        "the handoff size warning on long sessions",
    ),
)


class ClaudeCodeAdapter:
    name = "claude-code"

    #: Claude Code hook events this adapter records, mapped to event kinds.
    #: Anything not in this table returns None - an adapter that recorded
    #: every hook it was ever handed would fill `events` with lifecycle
    #: noise the extractor then has to read past.
    EVENT_KINDS = {
        "PostToolUse": EventKind.TOOL_CALL,
        "SessionEnd": EventKind.SESSION_END,
    }

    def install(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport:
        if scope != "user":
            # Project scope would mean .mcp.json and .claude/settings.json in
            # the repository; v1 only writes the user-level files.
            raise UnsupportedScope(
                f"scope '{scope}' is not supported; only 'user' is implemented"
            )
        home = home or Path.home()
        env = os.environ if env is None else env
        paths = resolve_paths(home, env)
        report = InstallReport(agent=self.name)
        backed_up: set[Path] = set()

        self._install_mcp(paths, report, backed_up)
        self._install_hook(paths, report, backed_up)
        self._install_skill(paths, report)
        if paths.relocated:
            # Otherwise a relocated install looks identical to a normal one and
            # the user has no way to tell where their config actually went.
            report.notes.append(
                f"{CONFIG_DIR_VAR} is set, so everything above was written "
                f"under {paths.config_dir} rather than ~/.claude."
            )
        report.notes.append(SLUG_CONVENTION)
        report.notes.append(RECORD_NOTE)
        report.notes.append(HANDOFF_NOTE)
        report.notes.append(CONFIG_NOTE)

        # Verification is the last step, deliberately: it is a live round
        # trip through the database, and every file above should already be
        # written and reported on before it runs. It never raises - a
        # failure here becomes a warning, folded into this same report -
        # because an install that dies proving it works is worse than one
        # that finishes and admits it could not prove anything.
        verification = self.verify(env=env, home=home)
        report.actions.extend(verification.actions)
        report.warnings.extend(verification.warnings)
        return report

    def _install_mcp(
        self, paths: ClaudePaths, report: InstallReport, backed_up: set[Path]
    ) -> None:
        path = paths.global_json
        config, warnings = jsonfile.read_json(path, backed_up)
        report.warnings.extend(warnings)
        servers = config.setdefault("mcpServers", {})
        servers["remem"] = {"command": "remem", "args": ["serve"]}
        jsonfile.write_json(path, config, backed_up)
        report.actions.append(f"Registered the remem MCP server in {path}")

    def _install_hook(
        self, paths: ClaudePaths, report: InstallReport, backed_up: set[Path]
    ) -> None:
        path = paths.settings
        settings, warnings = jsonfile.read_json(path, backed_up)
        report.warnings.extend(warnings)
        hooks = settings.setdefault("hooks", {})

        changed = False
        for entry in HOOK_ENTRIES:
            event, command, timeout = entry.event, entry.command, entry.timeout
            groups = hooks.setdefault(event, [])

            # Normalise remem's own entries before testing membership, so a
            # file naming only the old command upgrades rather than
            # accumulating, and one naming both collapses. Scoped to the
            # commands remem writes: settings.json is shared, and an install
            # that tidied away entries it did not write would be worse than
            # the duplicate it set out to fix.
            ours = tuple(LEGACY_COMMANDS.get(event, ())) + (command,)
            repaired = False
            seen = False
            for group in groups:
                kept = []
                for h in group.get("hooks", []):
                    if h.get("command") not in ours:
                        kept.append(h)
                        continue
                    if seen:
                        # A second remem entry on this event fires a second
                        # time; events has no unique constraint to catch the
                        # duplicate row that follows.
                        repaired = True
                        continue
                    seen = True
                    if h.get("command") != command or h.get("timeout") != timeout:
                        h["command"] = command
                        h["timeout"] = timeout
                        repaired = True
                    kept.append(h)
                group["hooks"] = kept
            # A group whose only entry was a duplicate of ours is now empty
            # and would otherwise linger as a hook that runs nothing.
            groups[:] = [g for g in groups if g.get("hooks")]

            if repaired:
                changed = True
                report.actions.append(
                    f"Repaired the {event} hook in {path} (now `{command}`, once)"
                )
                # The canonical command is present by construction now;
                # falling through would only add a second, contradictory
                # "already registered" line to the same report.
                continue

            already = any(
                command in h.get("command", "")
                for group in groups
                for h in group.get("hooks", [])
            )
            if already:
                report.actions.append(f"{event} hook already registered")
                continue
            groups.append(
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": command, "timeout": timeout}
                    ],
                }
            )
            changed = True
            report.actions.append(f"Registered the {event} hook in {path}")

        if changed:
            jsonfile.write_json(path, settings, backed_up)

    def _install_skill(self, paths: ClaudePaths, report: InstallReport) -> None:
        """Install every bundled skill directory.

        Iterating rather than naming one file: a later skill is a new
        directory under skills/ and nothing else.
        """
        root = paths.skills
        source_root = resources.files("remem.agents.claude_code") / "skills"
        for skill_dir in sorted(source_root.iterdir(), key=lambda p: p.name):
            if not skill_dir.is_dir():
                continue
            target = root / skill_dir.name
            shutil.copytree(skill_dir, target, dirs_exist_ok=True)
            report.actions.append(f"Installed the {skill_dir.name} skill in {target}")

    def identity(self, env: Mapping[str, str], payload: dict) -> Identity:
        cwd = payload.get("cwd")
        return Identity(
            agent=self.name,
            session_id=payload.get("session_id"),
            # The repository's name, not the directory's: a session started in
            # a subdirectory or a worktree belongs to the same project, and
            # using the directory name meant it injected nothing and captured
            # under a project nobody had enabled - silently, since the hook is
            # fail-soft.
            project=resolve_project(Path(cwd)) if cwd else None,
        )

    def settings_path(self, home: Path, env: Mapping[str, str]) -> Path:
        """Where `remem config` writes this agent's env block.

        The same resolver install() uses, so CLAUDE_CONFIG_DIR moves both and
        the two commands can never disagree about which file they mean.
        """
        return resolve_paths(home, env).settings

    def env_settings(self) -> Mapping[str, EnvVar]:
        """The environment variables `remem config` may write for this agent.

        Data, not policy: routing, validation and precedence all live in
        services/settings.py. The adapter only answers what exists.
        """
        return CLAUDE_CODE_ENV_VARS

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one Claude Code hook payload as an event, or None.

        The payload is passed through WHOLE. Picking fields out here would
        make this adapter the thing that decides what the extractor is
        allowed to see, and the extractor is the half of this pipeline meant
        to be fixable and re-runnable without re-recording anything.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook_event_name", ""))
        if kind is None:
            return None
        identity = self.identity(env, payload)
        if not identity.session_id:
            # Without a session id the event cannot be grouped for
            # extraction, so recording it would be storage with no reader.
            return None
        return HarnessEvent(
            kind=kind,
            session_id=identity.session_id,
            project=identity.project,
            tool=payload.get("tool_name"),
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )

    def verify(
        self, env: Mapping[str, str] | None = None, home: Path | None = None
    ) -> InstallReport:
        """Record an event, read it back, delete it.

        `home` is accepted for symmetry with `install()` but unused: this is
        a database round-trip, not a file-system one. The round-trip itself
        - what it proves and why it is shaped the way it is - lives in
        `remem.agents.verify.round_trip`, shared with every adapter.
        """
        return round_trip(self.name, env)
