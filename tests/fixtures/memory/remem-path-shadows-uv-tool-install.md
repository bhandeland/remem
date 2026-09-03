---
name: remem-path-shadows-uv-tool-install
description: remem was installed editable in BOTH ~/.venvs/default and the uv tool dir; the venv copy won on PATH and lacked the embed extra. Fixed 2026-09-02 by uninstalling the venv copy.
metadata: 
  node_type: memory
  type: project
  originSessionId: a2932ed1-1cfb-404a-b6f1-981ed1e311b6
  modified: 2026-09-02T13:02:18.326Z
---

Two editable installs of the same repo existed at once:

- `~/.venvs/default/bin/remem` - PATH position 2, no `[embed]` extra
- `~/.local/bin/remem` (uv tool) - PATH position 4, has `[embed]`

The venv copy shadowed the uv-tool one, so `remem embed` failed with "The
local embedder needs the 'embed' extra" no matter how often
`uv tool install --editable '.[embed]'` was re-run. The install kept
succeeding; PATH kept resolving elsewhere. Both copies were editable against
the same source, so the symptom was purely about which one's *dependencies*
were on hand - which is why it looked like an install bug rather than a PATH
one.

**Fixed 2026-09-02:** `uv pip uninstall --python ~/.venvs/default/bin/python
remem`. Bare `remem` now resolves to the uv tool install, `remem embed`
works, and `remem doctor` reports all eight hook registrations ok.

**It comes back** if anything runs `pip install -e .` (or `uv sync` in a way
that targets that venv) while `~/.venvs/default` is active - the repo's own
`uv sync` targets `.venv`, so that is safe. The tell is `which -a remem`
listing more than the `~/.local/bin` entry.

This mattered beyond the embedder: CLAUDE.md registers the MCP server and
every hook as bare `remem`, so all of them were running the extra-less copy.

Related: [[remem-fail-soft-hides-budget-failures]]
