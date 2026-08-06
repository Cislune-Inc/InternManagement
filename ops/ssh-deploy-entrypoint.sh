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
    exec curl --fail --silent --show-error "http://127.0.0.1:8765/health"
    ;;
  status)
    cd "${repo_root}"
    exec ops/verify-services.sh
    ;;
  revision)
    exec git -C "${repo_root}" rev-parse HEAD
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
    echo "Allowed commands: health, status, revision, deploy agent/<reviewed-branch>" >&2
    exit 2
    ;;
esac
