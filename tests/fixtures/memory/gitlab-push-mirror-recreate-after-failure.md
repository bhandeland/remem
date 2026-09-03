---
name: gitlab-push-mirror-recreate-after-failure
description: A GitLab push mirror that has already failed will not recover from a force-sync - delete and recreate it
metadata:
  type: reference
---

GitLab push mirrors report `status=to_retry` with the *previous* attempt's
`last_error` still attached. That is indistinguishable from a live failure, so
a mirror that is actually fixed still looks broken, and
`POST /projects/:id/remote_mirrors/:mirror_id/sync` does not clear the backoff.

Fix: DELETE the mirror and POST a new one. The six nighthawk-oss mirrors went
green immediately on recreate after several sync attempts had done nothing
(2026-08-29).

Related: the API has no way to update a stored mirror credential in place, so
a PAT rotation also requires delete-and-recreate. `scripts-setup-mirror.sh` in
the remem tree takes `REPLACE=1` for this.

Two diagnostic notes worth keeping:
- Reading `/repos/:owner/:repo` with a fine-grained PAT returns
  `permissions.push: true` - that is the *user's* role on the repo, not the
  token's grant, and public repos are readable by any valid token. It looks
  like proof of write access and is not. Use the `x-accepted-github-permissions`
  response header on a 403 instead; it names the exact permission required.
- GitHub ignores the `sha` field in a Contents API PUT when the path does not
  yet exist, so an "invalid sha" write probe is NOT read-only - it creates the
  file. Probe a path that is known to exist, or do not probe.
