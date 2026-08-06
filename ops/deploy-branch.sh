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

git fetch --prune origin "${branch}"
candidate="$(git rev-parse --verify FETCH_HEAD)"
git merge-base --is-ancestor HEAD "${candidate}"

.venv/bin/python -m agent.backup
latest_backup="$(find backups -name '*.tar.gz.enc' -type f -print0 | xargs -0 ls -t | head -1)"
if [[ -z "${latest_backup}" ]]; then
  echo "No encrypted backup was created; deployment stopped." >&2
  exit 1
fi
.venv/bin/python -m agent.backup --verify "${latest_backup}"

git merge --ff-only "${candidate}"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q agent
ops/restart-services.sh

echo "Deployed ${branch} at ${candidate}."
