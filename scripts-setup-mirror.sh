#!/usr/bin/env bash
# Configure GitLab -> GitHub push mirrors for every nighthawk-oss project that
# does not already have one. Prompts once for a GitHub fine-grained PAT;
# never echoes it, never writes it to disk.
#
# Matches the existing fleet mirror exactly:
#   enabled=true  only_protected_branches=false  keep_divergent_refs=false
set -euo pipefail

GH_USER=bhandeland

# gitlab project id : repo name (identical on both sides)
PROJECTS="
80079217 fleet
77583494 kubesealpl
77305372 kubesealpy
77314371 nighthawk-blame
77317153 nighthawk-cherrypick
85882325 remem
77455977 urlcontainers
"

# Token sources, in order. The prompt needs a real terminal - run under
# Claude Code's `!` prefix there is none, read hits EOF, and `set -e` used to
# abort with no output at all. Fail loudly instead, and offer a file so the
# token never has to be typed into a transcript.
if [ -n "${TOKEN_FILE:-}" ]; then
  [ -r "$TOKEN_FILE" ] || { echo "TOKEN_FILE not readable: $TOKEN_FILE" >&2; exit 1; }
  TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
elif [ ! -t 0 ]; then
  TOKEN=$(tr -d '\r\n')          # piped in
elif read -rsp "GitHub fine-grained PAT (Contents: write): " TOKEN; then
  echo
else
  TOKEN=""
fi

if [ -z "${TOKEN:-}" ]; then
  cat >&2 <<'MSG'
No token supplied.

  interactive terminal :  ./scripts-setup-mirror.sh
  from a file          :  TOKEN_FILE=~/.gh-mirror-token ./scripts-setup-mirror.sh
  piped                :  pbpaste | ./scripts-setup-mirror.sh

Under Claude Code's `!` prefix there is no terminal to prompt on, so use
TOKEN_FILE or a pipe - and prefer those over typing the token into a chat.
MSG
  exit 1
fi
echo

printf '%s\n' "$PROJECTS" | while read -r ID NAME; do
  [ -z "${ID:-}" ] && continue

  existing=$(glab api "projects/${ID}/remote_mirrors" 2>/dev/null || echo '[]')
  count=$(printf '%s' "$existing" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')

  if [ "$count" != "0" ]; then
    if [ "${REPLACE:-0}" = "1" ]; then
      # Rotating the PAT means the stored URL holds a dead credential, and the
      # API has no way to update it in place - delete and recreate.
      for mid in $(printf '%s' "$existing" | python3 -c 'import sys,json;[print(m["id"]) for m in json.load(sys.stdin)]'); do
        glab api --method DELETE "projects/${ID}/remote_mirrors/${mid}" >/dev/null 2>&1 || true
      done
      printf '  %-22s replacing existing mirror... ' "$NAME"
    else
      printf '  %-22s skipped (mirror already configured; REPLACE=1 to recreate)\n' "$NAME"
      continue
    fi
  fi

  if glab api --method POST "projects/${ID}/remote_mirrors" \
      --field "url=https://${GH_USER}:${TOKEN}@github.com/${GH_USER}/${NAME}.git" \
      --field "enabled=true" \
      --field "only_protected_branches=false" \
      --field "keep_divergent_refs=false" >/dev/null 2>&1; then
    printf '  %-22s mirror created\n' "$NAME"
  else
    printf '  %-22s FAILED\n' "$NAME"
  fi
done

unset TOKEN
echo
echo "Verify with:  ./scripts-check-mirrors.sh"
