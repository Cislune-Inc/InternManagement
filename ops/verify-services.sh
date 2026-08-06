#!/bin/zsh
set -euo pipefail

uid="$(id -u)"
launchctl print "gui/${uid}/com.pm.internmanagement.bot" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.time-tracking" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.backup" >/dev/null
launchctl print "gui/${uid}/com.pm.internmanagement.integration-health" >/dev/null
curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:8765/health"
grep -q "Logged in as DonPollo" "/Users/pm/InternManagement/data/launchd.stdout.log"
if grep -q '^SLACK_APP_TOKEN=xapp-' "/Users/pm/InternManagement/.env"; then
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
  [[ -n "${lock_pid}" ]]
  grep -q "Slack Socket Mode receiver connected pid=${lock_pid}\." \
    "/Users/pm/InternManagement/data/launchd.stderr.log"
fi
test -f "/Users/pm/InternManagement/data/integration_health.json"
find "/Users/pm/InternManagement/backups" -name '*.tar.gz.enc' -mmin -1560 | grep -q .

echo "Don Pollo bot, dashboard, backups, and integration monitoring are healthy."
