"""Installs remem into opencode: one generated plugin file, and nothing else.

The contrast with the Claude Code adapter is the point. That one writes an
MCP registration, four hook entries and a skills tree into two JSON files it
does not own. This one drops a single file into a directory opencode scans,
which means there is no user configuration to merge, to back up, or to
corrupt.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Mapping

from remem.agents.base import RECORD_NOTE, HarnessEvent, Identity, InstallReport
from remem.agents.opencode.install import plugin_dir
from remem.domain import EventKind
from remem.project import resolve_project


class OpenCodeAdapter:
    name = "opencode"

    #: opencode plugin hooks this adapter records, mapped to event kinds.
    #: Anything not in this table returns None. The keys must stay a subset
    #: of remem.agents.opencode.hooks.HOOK_NAMES - subscribing to a hook
    #: opencode does not emit is the silent dead loop this whole design
    #: exists to prevent, and tests/test_opencode_hooks_contract.py is what
    #: makes that loud.
    #:
    #: experimental.chat.system.transform is deliberately absent: it injects
    #: rather than records, so it never reaches event().
    EVENT_KINDS = {
        "tool.execute.after": EventKind.TOOL_CALL,
        "chat.message": EventKind.MESSAGE,
    }

    def install(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport:
        """Write the plugin, unconditionally, to the directory the scope names.

        No merge, no version marker, no prompt: `plugin_dir` raises for a
        scope it does not know, so by the time this runs the only question
        left is whether a file is already there, and the answer does not
        matter. remem owns remem.js - a user who wants to keep local edits
        should not put them there.
        """
        home = home or Path.home()
        env = os.environ if env is None else env
        target_dir = plugin_dir(scope, home=home, cwd=Path.cwd())
        target_dir.mkdir(parents=True, exist_ok=True)

        target = target_dir / "remem.js"
        source = resources.files("remem.agents.opencode") / "plugin.js"
        target.write_text(source.read_text())

        report = InstallReport(agent=self.name)
        report.actions.append(f"Installed the opencode plugin in {target}")
        report.notes.append(RECORD_NOTE)
        return report

    def identity(self, env: Mapping[str, str], payload: dict) -> Identity:
        cwd = payload.get("cwd")
        return Identity(
            agent=self.name,
            session_id=payload.get("sessionID"),
            # The repository's name, not the directory's. opencode hands the
            # plugin both `directory` and `worktree`; whichever the plugin
            # sent, resolving through the git common directory files a
            # subdirectory and a worktree under the repository they belong
            # to, the same as every other write path in remem.
            project=resolve_project(Path(cwd)) if cwd else None,
        )

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one opencode plugin payload as an event, or None.

        The payload is passed through WHOLE, for the same reason the Claude
        Code adapter does it: the extractor is the half of this pipeline
        meant to be fixable and re-runnable without re-recording anything,
        and an adapter that pruned fields here would cap what any future
        extractor could ever see.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook", ""))
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
            # None for chat.message, which carries no tool. The column is
            # nullable precisely so a non-tool event does not have to invent
            # a value.
            tool=payload.get("tool"),
            project=identity.project,
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )
