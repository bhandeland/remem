---
name: remem-prime
description: >
  Use at the start of a session, typically right after /clear, when resuming a
  known workstream - the user says "remem-prime <topic>", "pick up where we
  left off on X", or "get up to speed on X". Not for exploring an unfamiliar
  codebase and not for brand-new topics with no stored history.
---

# Prime a Topic

Rehydrate a fresh session from stored knowledge, not by exploring the
repository. Exploration output is re-sent every turn for the rest of the
session; a handoff is a one-time read of a few hundred tokens. That difference
is the entire reason this skill exists.

## Steps

1. **Read the handoff.** It carries Done / In flight / Next steps / Gotchas and
   is the primary source.

   ```bash
   remem handoff latest --topic gitlab-ci
   ```

2. **Check for anything newer.** Entries written after the handoff supersede
   what it says:

   ```bash
   remem search "gitlab-ci" --limit 10
   ```

3. **Brief the user** in a few lines: state of the world, the next steps from
   the handoff unless something newer contradicts them, and the gotchas. Then
   ask which step to start - or start the obvious one if they already said.

## Constraints

- No repository exploration while priming. No greps, no file-tree walks, no
  reading source files. Get code-level detail later, once work has started and
  you know which file you need.
- Sources conflict - newest wins, and name the conflict in one line rather than
  silently picking.
- Nothing stored at all: say so and ask for a pointer. Do not go looking.
