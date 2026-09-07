#!/bin/zsh
set -euo pipefail

if [[ -z "${DON_POLLO_DEPLOY_STABLE_COPY:-}" ]]; then
  repo_root="${0:A:h:h}"
  stable_script="$(mktemp "${TMPDIR:-/tmp}/don-pollo-deploy.XXXXXX")"
  cp "${0:A}" "${stable_script}"
  chmod 700 "${stable_script}"
  exec env \
    DON_POLLO_DEPLOY_STABLE_COPY=1 \
    DON_POLLO_DEPLOY_REPO_ROOT="${repo_root}" \
    DON_POLLO_DEPLOY_STABLE_PATH="${stable_script}" \
    /bin/zsh "${stable_script}" "$@"
fi

repo_root="${DON_POLLO_DEPLOY_REPO_ROOT:?}"
stable_script="${DON_POLLO_DEPLOY_STABLE_PATH:?}"
reconciliation_index=""
maintenance_started=0

cleanup() {
  if [[ "${maintenance_started}" == "1" ]]; then
    .venv/bin/python ops/deploy_maintenance.py stop >/dev/null 2>&1 || true
  fi
  [[ -z "${reconciliation_index}" ]] || rm -f "${reconciliation_index}"
  rm -f "${stable_script}"
}
trap cleanup EXIT

branch="${1:-}"
[[ $# -eq 0 ]] || shift
controls_args=()
restart_args=()
primary_admin_only=0
enrollment_args=()
defer_before=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      [[ $# -ge 2 ]] || exit 2
      controls_args=(--base-url "$2")
      shift 2 ;;
    --primary-admin-only) primary_admin_only=1; shift ;;
    --primary-admin-slack-id)
      [[ $# -ge 2 ]] || exit 2
      enrollment_args=(--primary-admin-slack-id "$2"); shift 2 ;;
    --enable-disabled) restart_args=(--enable-disabled); shift ;;
    --defer-primary-legacy-before)
      [[ $# -ge 2 ]] || exit 2
      defer_before="$2"; shift 2 ;;
    *) echo "Unknown deployment option." >&2; exit 2 ;;
  esac
done

if [[ ! "${branch}" =~ '^agent/[A-Za-z0-9._/-]+$' ]]; then
  echo "Usage: ops/deploy-branch.sh agent/<reviewed-branch>" >&2
  exit 2
fi

cd "${repo_root}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to deploy from a dirty production worktree." >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Production must be on the main branch before deployment." >&2
  exit 1
fi

git fetch --prune origin \
  "+refs/heads/${branch}:refs/remotes/origin/${branch}" \
  "+refs/heads/main:refs/remotes/origin/main"
candidate="$(git rev-parse --verify "refs/remotes/origin/${branch}")"
origin_main="$(git rev-parse --verify refs/remotes/origin/main)"

# Production temporarily contains five live commits that predate GitHub recovery.
# Only reconcile histories when both sides descend from the verified GitHub main.
git merge-base --is-ancestor "${origin_main}" HEAD
git merge-base --is-ancestor "${origin_main}" "${candidate}"

.venv/bin/python -m agent.backup
backup_candidates=(backups/*.tar.gz.enc(N.om))
latest_backup="${backup_candidates[1]:-}"
if [[ -z "${latest_backup}" ]]; then
  echo "No encrypted backup was created; deployment stopped." >&2
  exit 1
fi
.venv/bin/python -m agent.backup --verify "${latest_backup}"

if git merge-base --is-ancestor HEAD "${candidate}"; then
  git merge --ff-only "${candidate}"
else
  reconciliation_index="$(mktemp)"
  GIT_INDEX_FILE="${reconciliation_index}" git read-tree "${candidate}^{tree}"
  workflow_path=".github/workflows/test.yml"
  if git cat-file -e "HEAD:${workflow_path}" 2>/dev/null; then
    workflow_blob="$(git rev-parse "HEAD:${workflow_path}")"
    workflow_mode="$(git ls-tree HEAD -- "${workflow_path}" | awk '{print $1}')"
    GIT_INDEX_FILE="${reconciliation_index}" git update-index \
      --add \
      --cacheinfo "${workflow_mode}" "${workflow_blob}" "${workflow_path}"
  fi
  reconciliation_tree="$(GIT_INDEX_FILE="${reconciliation_index}" git write-tree)"
  reconciliation_commit="$(
    print -r -- "Reconcile reviewed GitHub candidate with recovered production history" \
      | git commit-tree "${reconciliation_tree}" -p HEAD -p "${candidate}"
  )"
  rm -f "${reconciliation_index}"
  reconciliation_index=""
  git merge --ff-only "${reconciliation_commit}"
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q agent
.venv/bin/python ops/apply_production_controls.py --apply "${controls_args[@]}"
if [[ "${primary_admin_only}" == "1" ]]; then
  PYTHONPATH=. .venv/bin/python ops/enable_slack_clock_beta.py --primary-admin-only --apply "${enrollment_args[@]}"
fi
state_db="$(.venv/bin/python -c 'import json; print(json.load(open("bootstrap.local.json"))["state_db_path"])')"
if [[ -n "${defer_before}" ]]; then
  [[ "${primary_admin_only}" == "1" ]] || { echo "Historical deferral requires an explicit primary-admin pilot." >&2; exit 2; }
  PYTHONPATH=. .venv/bin/python ops/defer_legacy_shifts.py --state-db "${state_db}" \
    --primary-admin --before-date "${defer_before}" --apply
fi
if [[ "${primary_admin_only}" == "1" ]]; then
  PYTHONPATH=. .venv/bin/python ops/slack_beta_preflight.py --state-db "${state_db}" --primary-admin-only
fi
# Compensation classification is a reviewed roster decision, not a deployment
# inference. Do not run either the legacy roster-default writer (which also
# re-adds workers) or compensation inference during a code deployment.
.venv/bin/python ops/deploy_maintenance.py start --minutes 15
maintenance_started=1
ops/restart-services.sh "${restart_args[@]}"
.venv/bin/python ops/deploy_maintenance.py stop
maintenance_started=0

echo "Deployed ${branch} at ${candidate}."
