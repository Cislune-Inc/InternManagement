from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from .persistence import atomic_write_json
from .runtime import InternManagementRuntime

_DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765/api/health"


async def run_integration_checks(
    runtime: InternManagementRuntime,
    *,
    dashboard_url: str = _DEFAULT_DASHBOARD_URL,
    backups_path: Path = Path("backups"),
    repo_path: Path | None = None,
    now: datetime | None = None,
    request: Callable[..., requests.Response] = requests.request,
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    checks = [
        _check_database(runtime),
        _check_bot_lock(runtime),
        _check_backup(backups_path, reference),
        _check_production_checkout(repo_path),
        _http_check(
            "dashboard",
            "GET",
            dashboard_url,
            request=request,
        ),
        _http_check(
            "discord",
            "GET",
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {os.environ.get('DISCORD_BOT_TOKEN', '')}"},
            request=request,
            enabled=bool(os.environ.get("DISCORD_BOT_TOKEN")),
        ),
        _http_check(
            "slack",
            "POST",
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {os.environ.get('SLACK_BOT_TOKEN', '')}"},
            request=request,
            enabled=bool(runtime.slack),
            slack_response=True,
        ),
        _http_check(
            "clickup",
            "GET",
            "https://api.clickup.com/api/v2/team",
            headers={"Authorization": os.environ.get("CLICKUP_API_TOKEN", "")},
            request=request,
            enabled=bool(runtime.clickup),
        ),
        _http_check(
            "openai",
            "GET",
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
            request=request,
            enabled=bool(os.environ.get("OPENAI_API_KEY")),
        ),
    ]
    for check in checks:
        name = str(check["name"])
        if check["status"] == "ok":
            runtime.operations.resolve("integration_health", name)
            continue
        if check["status"] == "disabled":
            continue
        await runtime.operations.report(
            category="integration_health",
            severity=(
                "critical"
                if name in {"database", "bot", "dashboard", "production_checkout"}
                else "warning"
            ),
            summary=f"{name.title()} health check failed.",
            details={
                "check": name,
                "error": check.get("error"),
                "checked_at": reference.isoformat(),
            },
            fingerprint_parts=(name,),
            now=reference,
        )
    return {
        "generated_at": reference.isoformat(),
        "overall_status": (
            "ok"
            if all(check["status"] in {"ok", "disabled"} for check in checks)
            else "error"
        ),
        "checks": checks,
    }


def _check_database(runtime: InternManagementRuntime) -> dict[str, Any]:
    started = time.monotonic()
    try:
        snapshot = runtime.state_store.health_snapshot()
        path = Path(snapshot["database_path"])
        probe = path.parent / ".health-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return _result("database", "ok", started, details=snapshot)
    except Exception as exc:
        return _result("database", "error", started, error=str(exc))


def _check_bot_lock(runtime: InternManagementRuntime) -> dict[str, Any]:
    started = time.monotonic()
    lock_path = runtime.bootstrap.state_db_path.parent / "agent.lock"
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        os.kill(pid, 0)
        return _result("bot", "ok", started, details={"pid": pid})
    except Exception as exc:
        return _result("bot", "error", started, error=str(exc))


def _check_backup(
    backups_path: Path,
    now: datetime,
    *,
    maximum_age: timedelta = timedelta(hours=26),
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        latest = max(
            backups_path.glob("*.tar.gz.enc"),
            key=lambda path: path.stat().st_mtime,
        )
        created_at = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
        age = now.astimezone(timezone.utc) - created_at
        if age > maximum_age:
            raise RuntimeError(f"Latest encrypted backup is {age} old.")
        return _result(
            "backup",
            "ok",
            started,
            details={"path": str(latest.resolve()), "created_at": created_at.isoformat()},
        )
    except Exception as exc:
        return _result("backup", "error", started, error=str(exc))


def _check_production_checkout(repo_path: Path | None) -> dict[str, Any]:
    started = time.monotonic()
    if repo_path is None:
        return _result("production_checkout", "disabled", started)
    try:
        root = repo_path.resolve()
        branch = _git_output(root, "branch", "--show-current")
        if branch != "main":
            raise RuntimeError(
                f"Production checkout is on {branch or 'a detached HEAD'}, not main."
            )
        dirty = _git_output(root, "status", "--porcelain")
        if dirty:
            raise RuntimeError("Production checkout contains uncommitted tracked changes.")
        revision = _git_output(root, "rev-parse", "HEAD")
        return _result(
            "production_checkout",
            "ok",
            started,
            details={"branch": branch, "revision": revision},
        )
    except Exception as exc:
        return _result("production_checkout", "error", started, error=str(exc))


def _git_output(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _http_check(
    name: str,
    method: str,
    url: str,
    *,
    request: Callable[..., requests.Response],
    headers: dict[str, str] | None = None,
    enabled: bool = True,
    slack_response: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    if not enabled:
        return _result(name, "disabled", started)
    try:
        response = request(method, url, headers=headers, timeout=15)
        response.raise_for_status()
        if slack_response:
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "Slack auth.test failed."))
        return _result(
            name,
            "ok",
            started,
            details={"status_code": response.status_code},
        )
    except Exception as exc:
        return _result(name, "error", started, error=str(exc))


def _result(
    name: str,
    status: str,
    started: float,
    *,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "details": details or {},
        "error": error,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Don Pollo integration smoke checks.")
    parser.add_argument("--dashboard-url", default=_DEFAULT_DASHBOARD_URL)
    parser.add_argument("--backups", type=Path, default=Path("backups"))
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/integration_health.json"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    runtime = InternManagementRuntime()
    asyncio.run(runtime.refresh_configuration(force=True))
    payload = asyncio.run(
        run_integration_checks(
            runtime,
            dashboard_url=args.dashboard_url,
            backups_path=args.backups,
            repo_path=args.repo,
        )
    )
    atomic_write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if payload["overall_status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
