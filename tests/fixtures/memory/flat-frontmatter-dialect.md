---
name: Flat frontmatter dialect
description: Claude Code also writes memory files with type and its own bookkeeping at the top level, with no metadata: block
type: reference
originSessionId: 00000000-0000-0000-0000-000000000000
---

Two frontmatter dialects exist in the corpus on disk. This one puts `type:` and
Claude Code's bookkeeping keys at the top level of the frontmatter rather than
indented under a `metadata:` line.

Sixteen real files in two project directories are in this dialect, so it is not
hypothetical: parsing it as though only the indented form existed loses the type
and every bookkeeping key with it.
