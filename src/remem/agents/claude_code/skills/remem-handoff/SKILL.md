---
name: remem-handoff
description: >
  Use when a task or workstream is wrapping up, when the session-size warning
  has fired, when the user says "handoff", "wrap up", or "let's stop here", or
  before suggesting /clear at a task boundary. Writes the session's state into
  remem so it can be resumed after clearing.
---

# Session Handoff

Persist session state so the session can be `/clear`ed without losing anything,
then point at the resume path. `remem-handoff` -> `/clear` -> `remem-prime`
makes session boundaries cheap.

## Steps

1. **Pick the topic slug** for this session's work - `gitlab-ci`, `search`,
   `extraction`. It defaults to the project name, which is right for a repository
   with one workstream. Ask only if genuinely ambiguous.

2. **Write the handoff.** Four sections, in this order, always all four:

   - **Done** - completed work, with URLs and ids as plain text, not markdown
     links.
   - **In flight** - running pipelines, pending reviews, background agents,
     with the ids needed to check on them.
   - **Next steps** - numbered, concrete, in priority order.
   - **Gotchas** - non-obvious constraints found this session that are not yet
     stored as memories or rules.

   ```bash
   remem handoff write --topic gitlab-ci --body "$(cat <<'EOF'
   ## Done
   ...
   ## In flight
   ...
   ## Next steps
   ...
   ## Gotchas
   ...
   EOF
   )"
   ```

   Writing a handoff supersedes the previous one for that topic. There is no
   need to clean up old ones.

3. **Fold durable facts into remem proper.** A gotcha that will still be true
   next month belongs in a note or a rule, not in a handoff that the next
   handoff supersedes:

   ```bash
   remem remember "Runner cache key must include the lockfile hash" --body "..."
   remem rule "Never rerun a release pipeline; tag a new patch instead" --body "..."
   ```

4. **Close.** Print the entry id and the exact resume path: `/clear`, then the
   remem-prime skill with this topic.

## Constraints

- The handoff is the complete resume payload. Write it so a fresh session needs
  nothing else to continue - no "as discussed above".
- Do not start new work during a handoff. If the user asks for one more thing,
  do it, then hand off.
- If `remem handoff write` fails, say so plainly and do not suggest `/clear`.
  Unlike remem's hooks, this command is loud on purpose: the context is about
  to be thrown away.
