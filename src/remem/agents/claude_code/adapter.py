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
    EnvVar,
    HarnessEvent,
    Identity,
    InstallReport,
    UnsupportedScope,
)
from remem.agents.claude_code.env_vars import CLAUDE_CODE_ENV_VARS
from remem.domain import EventKind
from remem.project import resolve_project

HOOK_COMMAND = "remem hook session-start"
SESSION_END_COMMAND = "remem hook session-end"
SESSION_SIZE_COMMAND = "remem hook session-size"

SLUG_CONVENTION = (
    "The SessionStart hook injects the knowledge base whose slug matches the "
    "session's repository name - create one with `remem kb new <repo-name>`. A subdirectory or a worktree resolves to the same name."
)

CAPTURE_NOTE = (
    "Automatic capture is OFF until you enable it per project: "
    "`remem capture enable --project <name>`. Nothing is recorded from a "
    "project you did not choose."
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
        paths = resolve_paths(home, os.environ if env is None else env)
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
        report.notes.append(CAPTURE_NOTE)
        report.notes.append(HANDOFF_NOTE)
        report.notes.append(CONFIG_NOTE)
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
        for event, command, timeout in (
            ("SessionStart", HOOK_COMMAND, 10),
            ("SessionEnd", SESSION_END_COMMAND, 10),
            # Runs on every prompt, so it gets the shortest timeout of the
            # three; it reads one file and never opens Postgres.
            ("UserPromptSubmit", SESSION_SIZE_COMMAND, 5),
        ):
            groups = hooks.setdefault(event, [])
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
