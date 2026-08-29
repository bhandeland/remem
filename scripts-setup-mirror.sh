#!/usr/bin/env bash
# One-shot: point gitlab.com/nighthawk-oss/remem at github.com/bhandeland/remem
# as a push mirror. Prompts for a GitHub fine-grained PAT; never echoes it and
# never writes it to disk.
set -euo pipefail

PROJECT_ID=85882325
GH_USER=bhandeland
GH_REPO=remem

read -rsp "GitHub fine-grained PAT (Contents: write on ${GH_USER}/${GH_REPO}): " TOKEN
echo

glab api --method POST "projects/${PROJECT_ID}/remote_mirrors" \
  --field "url=https://${GH_USER}:${TOKEN}@github.com/${GH_USER}/${GH_REPO}.git" \
  --field "enabled=true" \
  --field "only_protected_branches=false" \
  --field "keep_divergent_refs=false"

unset TOKEN
echo
echo "Mirror created. Trigger the first sync with:"
echo "  glab api --method POST projects/${PROJECT_ID}/mirror/pull  # (or just push a commit)"
