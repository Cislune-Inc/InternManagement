#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
lock_path="${repo_root}/data/agent.lock"
old_pid="$(
  /usr/bin/python3 - "${lock_path}" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

try:
    print(int(json.loads(Path(sys.argv[1]).read_text())["pid"]))
except Exception:
    print("")
PY
)"

uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.bot"
launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.time-tracking"

ready=false
lock_pid=""
for _attempt in {1..45}; do
  lock_pid="$(
    /usr/bin/python3 - "${lock_path}" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

try:
    print(int(json.loads(Path(sys.argv[1]).read_text())["pid"]))
except Exception:
    print("")
PY
  )"
  if [[ -n "${lock_pid}" ]] \
    && { [[ -z "${old_pid}" ]] || [[ "${lock_pid}" != "${old_pid}" ]]; } \
    && kill -0 "${lock_pid}" 2>/dev/null \
    && ps -p "${lock_pid}" -o command= | grep -q -- "agent.main" \
    && curl --fail --silent --output /dev/null "http://127.0.0.1:8765/health"; then
    ready=true
    break
  fi
  sleep 1
done

if [[ "${ready}" != true ]]; then
  echo "Bot or dashboard did not restart with a fresh verified process." >&2
  exit 1
fi

launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.integration-health"
sleep 3
"${0:A:h}/verify-services.sh"
