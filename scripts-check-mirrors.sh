#!/usr/bin/env bash
# Report mirror health for every nighthawk-oss project. Mirrors fail silently -
# GitLab disables one after repeated auth failures and tells nobody - so this
# is worth running after a PAT rotation.
set -euo pipefail
exec python3 - <<'PY'
import json, subprocess

def api(path):
    r = subprocess.run(["glab", "api", path], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return []

for p in sorted(api("groups/nighthawk-oss/projects?per_page=50"),
                key=lambda x: x["path"]):
    mirrors = api(f"projects/{p['id']}/remote_mirrors")
    if not mirrors:
        print(f"  {p['path']:24} NO MIRROR")
        continue
    for m in mirrors:
        ok = m.get("enabled") and not m.get("last_error")
        last = (m.get("last_successful_update_at") or "never")[:19]
        print(f"  {p['path']:24} {'ok ' if ok else 'BAD'} "
              f"status={m.get('update_status')} last={last} "
              f"err={m.get('last_error') or '-'}")
PY
