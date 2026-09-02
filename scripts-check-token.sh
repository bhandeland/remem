#!/usr/bin/env bash
# Does a GitHub PAT actually have contents=write on the mirror repos?
# Prompts for the token; never echoes it, never writes it to disk.
# Uses a deliberately invalid blob sha so nothing is ever created: a
# permissions failure (403) is distinguishable from an accepted-but-invalid
# write (422), which is what "has write access" looks like.
set -euo pipefail
read -rsp "GitHub PAT: " T; echo; echo

for r in fleet kubesealpl kubesealpy nighthawk-blame nighthawk-cherrypick remem urlcontainers; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
    -H "Authorization: Bearer $T" -H "Accept: application/vnd.github+json" \
    -d '{"message":"probe","content":"cHJvYmU=","sha":"0000000000000000000000000000000000000000"}' \
    "https://api.github.com/repos/bhandeland/$r/contents/.probe")
  case "$code" in
    422|409) printf '  %-22s OK    (contents=write granted)\n' "$r" ;;
    403)     printf '  %-22s NO    (contents=write MISSING)\n' "$r" ;;
    404)     printf '  %-22s NO    (repo not in token scope)\n' "$r" ;;
    401)     printf '  %-22s NO    (token invalid or revoked)\n' "$r" ;;
    *)       printf '  %-22s ?     (HTTP %s)\n' "$r" "$code" ;;
  esac
done
unset T
