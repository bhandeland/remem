"""Installs remem into Claude Code: MCP server, SessionStart hook, skill."""

from __future__ import annotations

import json
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Mapping

from remem.agents.base import Identity, InstallReport, UnsupportedScope
from remem.project import resolve_project

HOOK_COMMAND = "remem hook session-start"
SESSION_END_COMMAND = "remem hook session-end"

SLUG_CONVENTION = (
    "The SessionStart hook injects the knowledge base whose slug matches the "
    "session's directory name - create one with `remem kb new <dirname>`."
)

CAPTURE_NOTE = (
    "Automatic capture is OFF until you enable it per project: "
    "`remem capture enable --project <name>`. Nothing is recorded from a "
    "project you did not choose."
)


def _backup(path: Path) -> Path:
    ts = int(time.time())
    target = path.with_suffix(path.suffix + f".bak{ts}")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_suffix(path.suffix + f".bak{ts}-{counter}")
    shutil.copy2(path, target)
    return target


def _backup_once(path: Path, backed_up: set[Path]) -> None:
    """Back up path if it exists on disk and hasn't already been backed up
    during this install run (avoids a redundant second backup of a file
    _read_json already snapshotted because it was corrupt)."""
    if path.exists() and path not in backed_up:
        _backup(path)
        backed_up.add(path)


def _read_json(path: Path, report: InstallReport, backed_up: set[Path]) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _backup_once(path, backed_up)
        report.warnings.append(
            f"{path} was not valid JSON. It has been backed up and replaced; "
            "check the backup for anything you need."
        )
        return {}


def _write_json(path: Path, data: dict, backed_up: set[Path]) -> None:
    _backup_once(path, backed_up)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


class ClaudeCodeAdapter:
    name = "claude-code"

    def install(self, scope: str = "user", home: Path | None = None) -> InstallReport:
        if scope != "user":
            # Project scope would mean .mcp.json and .claude/settings.json in
            # the repository; v1 only writes the user-level files.
            raise UnsupportedScope(
                f"scope '{scope}' is not supported; only 'user' is implemented"
            )
        home = home or Path.home()
        report = InstallReport(agent=self.name)
        backed_up: set[Path] = set()

        self._install_mcp(home, report, backed_up)
        self._install_hook(home, report, backed_up)
        self._install_skill(home, report)
        report.notes.append(SLUG_CONVENTION)
        report.notes.append(CAPTURE_NOTE)
        return report

    def _install_mcp(self, home: Path, report: InstallReport, backed_up: set[Path]) -> None:
        path = home / ".claude.json"
        config = _read_json(path, report, backed_up)
        servers = config.setdefault("mcpServers", {})
        servers["remem"] = {"command": "remem", "args": ["serve"]}
        _write_json(path, config, backed_up)
        report.actions.append(f"Registered the remem MCP server in {path}")

    def _install_hook(self, home: Path, report: InstallReport, backed_up: set[Path]) -> None:
        path = home / ".claude" / "settings.json"
        settings = _read_json(path, report, backed_up)
        hooks = settings.setdefault("hooks", {})

        changed = False
        for event, command, timeout in (
            ("SessionStart", HOOK_COMMAND, 10),
            ("SessionEnd", SESSION_END_COMMAND, 10),
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
            _write_json(path, settings, backed_up)

    def _install_skill(self, home: Path, report: InstallReport) -> None:
        target = home / ".claude" / "skills" / "remem"
        target.mkdir(parents=True, exist_ok=True)
        source = resources.files("remem.agents.claude_code") / "skill" / "SKILL.md"
        (target / "SKILL.md").write_text(source.read_text())
        report.actions.append(f"Installed the remem skill in {target}")

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
