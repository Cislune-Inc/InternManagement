"""Restart one verified DP service; disabled jobs require an explicit flag."""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = {"bot": "agent.main", "time-tracking": "agent.hours_editor",
           "integration-health": "agent.integration_health"}


def restart(repo: Path, service: str, agents: Path, *, uid: int,
            enable_disabled: bool = False, run=subprocess.run, sleep=time.sleep) -> None:
    if service not in MODULES:
        raise ValueError("Unknown DP service; no launchd changes made.")
    label = "com.pm.internmanagement." + service
    domain, target = f"gui/{uid}", f"gui/{uid}/{label}"
    plist = agents / (label + ".plist")
    payload = plistlib.loads(plist.read_bytes())
    expected = [str(repo / ".venv/bin/python"), "-m", MODULES[service]]
    if (payload.get("Label") != label or payload.get("WorkingDirectory") != str(repo)
            or payload.get("ProgramArguments", [])[:3] != expected):
        raise ValueError("Installed launch agent does not match the verified DP checkout.")

    def call(*args):
        return run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=20)

    def require(result, operation):
        if result.returncode:
            # launchd output can contain environment values: never echo it.
            raise RuntimeError(f"DP {service}: launchctl {operation} failed ({result.returncode}).")

    disabled = call("print-disabled", domain)
    require(disabled, "print-disabled")
    # macOS variants print either the boolean `true` or the word `disabled`.
    is_disabled = bool(re.search(r'"' + re.escape(label) + r'"\s*=>\s*(?:true|disabled)\b', disabled.stdout))
    loaded = call("print", target).returncode == 0
    if is_disabled and not enable_disabled:
        raise ValueError("DP service is disabled; verify the candidate/cohort then explicitly use --enable-disabled.")
    if service == "time-tracking":
        args = list(payload["ProgramArguments"])
        changed = False
        if "--host" in args:
            pos = args.index("--host") + 1
            if pos >= len(args):
                raise ValueError("Incomplete dashboard host argument")
            changed = args[pos] != "127.0.0.1"
            args[pos] = "127.0.0.1"
        else:
            args.extend(["--host", "127.0.0.1"])
            changed = True
        if changed:
            from agent.persistence import atomic_write_text
            backup = plist.with_name(plist.name + ".before-manager-guard-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            shutil.copy2(plist, backup)
            payload["ProgramArguments"] = args
            atomic_write_text(plist, plistlib.dumps(payload).decode())
            if loaded:
                require(call("bootout", target), "bootout")
                loaded = False
    if is_disabled:
        require(call("enable", target), "enable")
    if not loaded:
        # launchd can return EIO while a just-booted-out job is still unloading.
        # Retry only that transient code, bounded; never hide final failure.
        for attempt in range(3):
            result = call("bootstrap", domain, str(plist))
            if result.returncode != 5 or attempt == 2:
                require(result, "bootstrap")
                break
            sleep(2)
    # bootstrap can launch RunAtLoad jobs; kickstart provides a definite current
    # restart before the caller's process/socket verification.
    require(call("kickstart", "-k", target), "kickstart")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--service", choices=MODULES, required=True)
    parser.add_argument("--enable-disabled", action="store_true")
    args = parser.parse_args()
    restart(args.repo_root.resolve(), args.service, Path.home() / "Library/LaunchAgents",
            uid=os.getuid(), enable_disabled=args.enable_disabled)
    print("Restart requested for verified DP service:", args.service)


if __name__ == "__main__":
    main()
