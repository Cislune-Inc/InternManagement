#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
uid="$(id -u)"
[[ "$(git -C "${repo_root}" branch --show-current)" == "main" ]]
[[ -z "$(git -C "${repo_root}" status --porcelain)" ]]
launchctl print "gui/${uid}/com.pm.internmanagement.bot" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.time-tracking" | grep -q "state = running"
launchctl print "gui/${uid}/com.pm.internmanagement.backup" >/dev/null
launchctl print "gui/${uid}/com.pm.internmanagement.integration-health" >/dev/null
curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:8765/health"
lock_path="${repo_root}/data/agent.lock"
stdout_log="${repo_root}/data/launchd.stdout.log"
slack_only="$("${repo_root}/.venv/bin/python" -c 'import json,sys; print("yes" if json.load(open(sys.argv[1])).get("slack",{}).get("work_intake_beta_slack_user_ids") else "no")' "${repo_root}/config/agent.config.json")"
if [[ "${slack_only}" != "yes" ]]; then
  grep -q "Logged in as DonPollo" "${stdout_log}"
else
  curl --fail --silent --show-error --max-time 5 --output /dev/null "http://127.0.0.1:8766/"
fi
if [[ "${slack_only}" == "yes" ]] || grep -q '^SLACK_APP_TOKEN=xapp-' "${repo_root}/.env"; then
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
  [[ -n "${lock_pid}" ]]
  kill -0 "${lock_pid}" 2>/dev/null
  ps -p "${lock_pid}" -o command= | grep -q -- "agent.main"
  tail -n 1000 "${stdout_log}" \
    | grep -q "Slack Socket Mode receiver connected pid=${lock_pid}\."
fi
test -f "${repo_root}/data/integration_health.json"
find "${repo_root}/backups" -name '*.tar.gz.enc' -mmin -1560 | grep -q .

echo "Don Pollo bot, dashboard, backups, and integration monitoring are healthy."
