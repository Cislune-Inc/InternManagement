#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
ssh_dir="/Users/pm/.ssh"
authorized_keys="${ssh_dir}/authorized_keys"
public_key_file="${repo_root}/ops/restricted-deploy-key.pub"
key_comment="codex-don-pollo-deploy"
expected_fingerprint="SHA256:GZ9SuGk+iCHao7oM1/TFTQtU4AXfVMCEUL+iLhoWv+Y"
mode="${1:-install}"

if [[ "${mode}" != install && "${mode}" != remove ]]; then
  echo "Usage: ops/manage-restricted-deploy-key.sh [install|remove]" >&2
  exit 2
fi

actual_fingerprint="$(ssh-keygen -lf "${public_key_file}" | awk '{print $2}')"
if [[ "${actual_fingerprint}" != "${expected_fingerprint}" ]]; then
  echo "Restricted deployment public-key fingerprint mismatch." >&2
  exit 1
fi

mkdir -p "${ssh_dir}"
chmod 700 "${ssh_dir}"
touch "${authorized_keys}"
chmod 600 "${authorized_keys}"

timestamp="$(date +%Y%m%dT%H%M%S%z)"
backup="${authorized_keys}.before-don-pollo-key.${timestamp}"
cp -p "${authorized_keys}" "${backup}"
temporary="$(mktemp "${ssh_dir}/.authorized_keys.XXXXXX")"
cleanup() {
  rm -f "${temporary}"
}
trap cleanup EXIT

grep -Fv -- " ${key_comment}" "${authorized_keys}" > "${temporary}" || true
if [[ "${mode}" == install ]]; then
  public_key="$(<"${public_key_file}")"
  print -r -- \
    "restrict,command=\"${repo_root}/ops/ssh-deploy-entrypoint.sh\" ${public_key}" \
    >> "${temporary}"
fi
chmod 600 "${temporary}"
mv "${temporary}" "${authorized_keys}"
trap - EXIT

if [[ "${mode}" == install ]]; then
  echo "Installed restricted Don Pollo deployment key. Previous file: ${backup}"
else
  echo "Removed restricted Don Pollo deployment key. Previous file: ${backup}"
fi
