"""Installs remem into Cursor: hook entries merged into hooks.json, and a
generated rules file.

The contrast with the other two adapters is the point. opencode gets one
file remem owns outright; Claude Code gets entries merged into two JSON
files it does not own. Cursor gets both halves - `hooks.json` is
user-owned and shared, so it is merged and backed up, while the generated
`.mdc` is machine-owned and overwritten, because remem is the only thing
that ever writes it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from remem.agents.base import RECORD_NOTE, HarnessEvent, Identity, InstallReport
from remem.domain import EventKind
from remem.project import resolve_project

#: The payload keys Cursor uses, named once so the seam has exactly one
#: line to change per key. Read off
#: docs/superpowers/notes/2026-08-29-cursor-payloads.md, which is itself
#: source-derived (read out of Cursor's own payload-constructing code, not
#: captured from a live session - see that note's provenance section) -
#: tests/test_cursor_event.py is what stands between a rename here and a
#: harness that silently records nothing.
SESSION_KEY = "session_id"
ROOT_KEY = "workspace_roots"

#: Cursor's own constructor computes `session_id` as
#: `(the hook-specific payload's own session_id) ?? conversation_id`, i.e.
#: `conversation_id` is the older name for the same value and the fallback
#: this adapter takes when `SESSION_KEY` is absent. Nothing here has been
#: observed against a live Cursor session (see the payload note's
#: provenance section), so this is cheap insurance against the envelope
#: shipping under its previous name rather than a documented alternative
#: shape.
_SESSION_FALLBACK_KEY = "conversation_id"


class CursorAdapter:
    name = "cursor"

    #: Cursor hooks this adapter records, mapped to event kinds. Anything
    #: not in this table returns None.
    #:
    #: `sessionStart` is deliberately absent: it injects rather than
    #: records, so it never reaches event() - exactly as opencode's
    #: experimental.chat.system.transform is absent from its table.
    #:
    #: `postToolUse` rather than the three specific after* hooks: one
    #: parser instead of three, and no gap opens when Cursor adds a tool
    #: type. `afterAgentThought` is omitted because reasoning text is
    #: high-volume and low-signal for extraction. Every hook here is one
    #: Cursor does NOT wait on - see hooks.BLOCKING_HOOKS - and
    #: tests/test_cursor_event.py asserts both properties so a future edit
    #: that reaches for a blocking hook, or a hook Cursor does not emit,
    #: fails loudly instead of shipping a silent dead loop.
    EVENT_KINDS = {
        "postToolUse": EventKind.TOOL_CALL,
        "beforeSubmitPrompt": EventKind.MESSAGE,
        "afterAgentResponse": EventKind.MESSAGE,
    }

    def identity(self, env: Mapping[str, str], payload: dict) -> Identity:
        return Identity(
            agent=self.name,
            session_id=self._session_id(payload),
            # The repository's name, not the directory's - resolving
            # through the git common directory files a subdirectory and a
            # worktree under the repository they belong to, the same as
            # every other write path in remem.
            project=self._project(payload),
        )

    def _session_id(self, payload: dict) -> str | None:
        # See _SESSION_FALLBACK_KEY's comment: Cursor's own constructor
        # falls back to conversation_id when session_id is unset, so this
        # adapter does too.
        return payload.get(SESSION_KEY) or payload.get(_SESSION_FALLBACK_KEY)

    def _root(self, payload: dict) -> Path | None:
        """The workspace root as a path, or None. Distinct from
        `_project`, which turns the same value into a knowledge base slug -
        injection needs the directory, recording needs the name."""
        root = payload.get(ROOT_KEY)
        # Cursor's constructor builds this as a `.map()` over the
        # workspace's folders, so it is always a list of path strings in
        # practice - never a bare string. The string branch below is
        # measured insurance, not speculative generality: this shape has
        # never been observed against a live Cursor session (see the
        # payload note's provenance section), only read out of the
        # constructor's source, so tolerating the simpler shape costs
        # nothing and guards against that reading being wrong.
        #
        # For a genuine multi-root workspace this takes the first folder
        # and silently ignores the rest. That is a reasonable default, not
        # an oversight: the first entry is the folder the session was
        # started in, which is the one both recording (the project a
        # session's events belong to) and injection (where Cursor will
        # look for the rules file) actually need.
        if isinstance(root, list):
            root = root[0] if root else None
        return Path(root) if root else None

    def _project(self, payload: dict) -> str | None:
        root = self._root(payload)
        return resolve_project(root) if root else None

    def event(self, env: Mapping[str, str], payload: dict) -> HarnessEvent | None:
        """Read one Cursor hook payload as an event, or None.

        The payload is passed through WHOLE, for the same reason the other
        adapters do it: the extractor is the half of this pipeline meant to
        be fixable and re-runnable without re-recording anything, and an
        adapter that pruned fields here would cap what any future extractor
        could ever see. That includes `user_email`, which Cursor's
        constructor puts on every one of these hooks' payloads - unlike the
        Claude Code adapter's events, a recorded Cursor event will carry
        the user's email address as a side effect of this design. Flagged
        here so the next reader meets it deliberately; nothing in this
        task acts on it.
        """
        kind = self.EVENT_KINDS.get(payload.get("hook_event_name", ""))
        if kind is None:
            return None
        identity = self.identity(env, payload)
        if not identity.session_id:
            # Without a session id the event cannot be grouped, and
            # extraction is triggered per session by idleness. Recording it
            # would be storage with no reader.
            return None
        return HarnessEvent(
            kind=kind,
            session_id=identity.session_id,
            project=identity.project,
            tool=payload.get("tool_name"),
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
        )

    def inject(
        self,
        block: str,
        payload: dict,
        note: Callable[[str], None] | None = None,
    ) -> str | None:
        """Write the context block where Cursor will read it.

        The optional injection capability, probed with getattr exactly as
        event() is - Claude Code does not implement it, because stdout is
        the frontend's job there. Returning the path is what lets the CLI
        report where the block went without knowing what it wrote.

        `note` is the same escape hatch as `services/context.block()`'s:
        this method has no business deciding where a message is shown, so
        it hands the reason to whatever the caller passes and lets the
        frontend decide (REMEM_HOOK_DEBUG for the CLI, a list's `append`
        for a test). It is how the caller learns that the rules file was
        written but the exclude line was not - a repository with no
        `.git`, or one where the line is already present - so the fact is
        not simply dropped on the floor.

        Does not itself guarantee against raising: `Path.mkdir` and
        `Path.write_text` can still raise (`PermissionError`, a full disk).
        What is guaranteed is that failure is *safe*, not that it cannot
        happen - `hook_context` in cli.py wraps this call and degrades to
        the stdout path, exactly like every other optional capability here.
        """
        from remem.agents.cursor import rules

        if not block:
            # No knowledge base matched. Writing an empty rules file would
            # hand Cursor a rule that says nothing, every session.
            return None
        root = self._root(payload)
        if root is None:
            return None
        path = rules.write(root, block)
        if not rules.exclude(root) and note is not None:
            note(
                f"wrote {path} but did not add it to git's info/exclude "
                "(no repository here, or the line was already present)"
            )
        return str(path)

    def install(
        self,
        scope: str = "user",
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> InstallReport:
        """Merge remem's hook entries into hooks.json, then verify.

        Writes nothing else. The rules file is deliberately NOT written
        here: it is written at sessionStart, from the workspace the session
        actually opened, which is what makes one user-scope install work
        for every repository.
        """
        from remem.agents.cursor.install import ENTRIES, hooks_path, merge

        home = home or Path.home()
        env = os.environ if env is None else env

        # hooks_path raises for an unknown scope, so by the time anything
        # is written the scope is known good.
        path = hooks_path(scope, home=home, cwd=Path.cwd())
        path.parent.mkdir(parents=True, exist_ok=True)
        document, backup = merge(path, ENTRIES)
        path.write_text(json.dumps(document, indent=2) + "\n")

        report = InstallReport(agent=self.name)
        report.actions.append(
            f"Merged {len(ENTRIES)} hook entries into {path}"
        )
        if backup is not None:
            # A .bak nobody is told about is barely a safety net.
            report.actions.append(f"Backed up the previous file to {backup}")
        report.notes.append(RECORD_NOTE)
        report.notes.append(
            "The knowledge base block is written to .cursor/rules/remem.mdc "
            "at every session start, and added to .git/info/exclude so it "
            "stays out of git."
        )

        # Verification last, deliberately, and never raising - a failure
        # here becomes a warning folded into this same report, exactly as
        # both other adapters do it.
        verification = self.verify(env=env, home=home)
        report.actions.extend(verification.actions)
        report.warnings.extend(verification.warnings)
        return report

    def verify(
        self, env: Mapping[str, str] | None = None, home: Path | None = None
    ) -> InstallReport:
        """Record an event, read it back, delete it.

        `home` is accepted for symmetry with install() but unused: this is
        a database round-trip, not a file-system one. What it proves and
        why it is shaped this way lives in agents/verify.round_trip,
        shared with every adapter.
        """
        from remem.agents.verify import round_trip

        return round_trip(self.name, env)
