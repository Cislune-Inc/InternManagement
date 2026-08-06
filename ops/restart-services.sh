#!/bin/zsh
set -euo pipefail

uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.bot"
launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.time-tracking"

ready=false
for _attempt in {1..30}; do
  lock_pid="$(
    /usr/bin/python3 -c '
import json
from pathlib import Path
path = Path("/Users/pm/InternManagement/data/agent.lock")
try:
    print(int(json.loads(path.read_text())["pid"]))
except Exception:
    print("")
' 2>/dev/null
  )"
  if [[ -n "${lock_pid}" ]] \
    && kill -0 "${lock_pid}" 2>/dev/null \
    && curl --fail --silent --output /dev/null "http://127.0.0.1:8765/health"; then
    ready=true
    break
  fi
  sleep 1
done

if [[ "${ready}" != true ]]; then
  echo "Bot or dashboard did not become ready before health checks." >&2
  exit 1
fi

launchctl kickstart -k "gui/${uid}/com.pm.internmanagement.integration-health"
sleep 3
"${0:A:h}/verify-services.sh"
