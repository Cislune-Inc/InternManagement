#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
branch="${1:-}"

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
  trap 'rm -f "${reconciliation_index}"' EXIT
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
  trap - EXIT
  git merge --ff-only "${reconciliation_commit}"
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q agent
.venv/bin/python ops/apply_production_controls.py --apply
ops/restart-services.sh

echo "Deployed ${branch} at ${candidate}."
