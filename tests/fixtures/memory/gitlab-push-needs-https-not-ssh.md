---
name: gitlab-push-needs-https-not-ssh
description: Pushing to gitlab.com/nighthawk-oss from this Mac requires HTTPS plus the glab credential helper - SSH authenticates as the wrong GitLab account
metadata:
  type: project
---

This machine's SSH key is registered to GitLab account `bhandeland-wreckcheck`,
while `glab` is authenticated as `brandon.handeland` - the account with push
rights to the `nighthawk-oss` group. So `git@gitlab.com:nighthawk-oss/*` always
fails with "You are not allowed to push code to this project", even though
`glab` can create the project over the API moments earlier.

Compounding it: the macOS keychain holds a stale `oauth2` credential for
gitlab.com that shadows glab's token, so simply switching the remote to HTTPS
still gets "HTTP Basic: Access denied".

The working setup, applied per repo (2026-08-29, on `remem`):

    git remote set-url origin https://gitlab.com/nighthawk-oss/<project>.git
    git config --local --add credential."https://gitlab.com".helper ""
    git config --local --add credential."https://gitlab.com".helper '!glab auth git-credential'

The empty first value resets the inherited helper chain so `osxkeychain` is
bypassed for gitlab.com in that repo only - the stale entry is left alone in
case it serves the other account elsewhere.

Expect to redo this on every fresh clone from nighthawk-oss. See
[[gitlab-push-mirror-recreate-after-failure]].
