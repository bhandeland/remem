---
name: cursor-runs-claude-code-hooks
description: Cursor 3.x loads Claude Code's ~/.claude/settings.json hooks and runs them with Cursor payloads.
metadata:
  type: project
---

Cursor 3.x reads `~/.claude/settings.json` (plus a project's `.claude/settings.json`
and `.claude/settings.local.json`) and executes those hooks itself, handing them
**Cursor** payloads. Observed live on 2026-08-30 in Cursor 3.18.9's hooks service log:
`Claude user config path: ...`, `Loaded Claude user hooks`, `Executing hook 1/2 from
claude-user config`.

So remem's Claude-Code hooks (`remem hook session-start`, `session-end`,
`session-size`) already fire inside Cursor, receive a payload shape they were never
written to parse, and exit 0 in silence because hooks are fail-soft.

**Why:** it means remem's two adapters are less independent than the
`agents/registry` seam suggests. A user with both installed gets every hook twice by
two routes; a Cursor-only user is unaffected.

**How to apply:** do not assume `~/.claude/settings.json` is Claude-Code-only when
reasoning about which hooks run where. Nothing is broken by this today - it is
undecided territory, not a bug. See [[remem-fail-soft-hides-budget-failures]].
