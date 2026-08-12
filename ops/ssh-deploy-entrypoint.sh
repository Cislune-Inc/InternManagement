#!/bin/zsh
set -euo pipefail

repo_root="/Users/pm/InternManagement"
original_command="${SSH_ORIGINAL_COMMAND:-}"
audit_log="${repo_root}/data/ssh-deploy-audit.log"
mkdir -p "${audit_log:h}"
print -r -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) user=${USER:-unknown} command=${original_command:-none}" \
  >> "${audit_log}"

case "${original_command}" in
  health)
    cd "${repo_root}"
    exec .venv/bin/python ops/print-health-summary.py
    ;;
  status)
    cd "${repo_root}"
    exec ops/verify-services.sh
    ;;
  revision)
    exec git -C "${repo_root}" rev-parse HEAD
    ;;
  seed-management)
    cd "${repo_root}"
    exec .venv/bin/python -m ops.seed_project_management_tasks --apply
    ;;
  seed-management-plan)
    cd "${repo_root}"
    exec .venv/bin/python -m ops.seed_project_management_tasks
    ;;
  time-integrity-plan)
    cd "${repo_root}"
    exec env PYTHONPATH=. .venv/bin/python ops/repair_time_integrity.py
    ;;
  time-integrity-apply)
    cd "${repo_root}"
    .venv/bin/python -m agent.backup
    backup_candidates=(backups/*.tar.gz.enc(N.om))
    latest_backup="${backup_candidates[1]:-}"
    if [[ -z "${latest_backup}" ]]; then
      echo "No encrypted backup was created; time-integrity repair stopped." >&2
      exit 1
    fi
    .venv/bin/python -m agent.backup --verify "${latest_backup}"
    exec env PYTHONPATH=. .venv/bin/python ops/repair_time_integrity.py --apply
    ;;
  deploy\ agent/*)
    branch="${original_command#deploy }"
    if [[ ! "${branch}" =~ '^agent/[A-Za-z0-9._/-]+$' ]]; then
      echo "Invalid reviewed branch." >&2
      exit 2
    fi
    cd "${repo_root}"
    exec ops/deploy-branch.sh "${branch}"
    ;;
  *)
    echo "Allowed commands: health, status, revision, seed-management-plan, seed-management, time-integrity-plan, time-integrity-apply, deploy agent/<reviewed-branch>" >&2
    exit 2
    ;;
esac
