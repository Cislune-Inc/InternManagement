#!/bin/zsh
set -euo pipefail

uid="$(id -u)"
launchctl print "gui/${uid}/com.pm.internmanagement.bot" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.time-tracking" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.backup" >/dev/null
launchctl print "gui/${uid}/com.pm.internmanagement.integration-health" >/dev/null
curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:8765/health"
grep -q "Logged in as DonPollo" "/Users/pm/InternManagement/data/launchd.stdout.log"
test -f "/Users/pm/InternManagement/data/integration_health.json"
find "/Users/pm/InternManagement/backups" -name '*.tar.gz.enc' -mmin -1560 | grep -q .

echo "Don Pollo bot, dashboard, backups, and integration monitoring are healthy."
