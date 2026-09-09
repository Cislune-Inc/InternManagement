from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from .runtime import InternManagementRuntime
from .manager_exceptions import (
    build_manager_exceptions_payload,
    render_manager_exceptions_html,
)
from .system_health import build_system_health_payload, render_system_health_html
from .payroll_dashboard import (
    build_payroll_dashboard_payload,
    render_payroll_dashboard_html,
    resolve_payroll_download,
)
from .payroll_export import PayrollExporter
from .payroll_review import record_review_resolution
from .time_tracking_dashboard import (
    build_time_tracking_dashboard_payload,
    render_time_tracking_dashboard_html,
)
from .time_utils import resolve_timezone
from .work_dashboard import build_work_dashboard_payload, render_work_dashboard_html
from .worker_portal import WorkerPortalService
from .portfolio import load_portfolio, render_portfolio


class HoursEditorService:
    def __init__(self, runtime: InternManagementRuntime) -> None:
        self.runtime = runtime
        self.worker_portal = WorkerPortalService(runtime)

    def _reference_now(self) -> datetime:
        return datetime.now(tz=resolve_timezone(self.runtime.runtime_timezone_name()))

    def _storage_root(self) -> Path:
        storage_root = self.runtime._storage_root_path()
        if storage_root is None:
            raise RuntimeError("Storage root is unavailable.")
        return storage_root

    def build_dashboard_payload(self) -> dict[str, Any]:
        return build_time_tracking_dashboard_payload(
            self._storage_root(),
            runtime=self.runtime,
            reference_now=self._reference_now(),
            editor_mode=True,
        )

    def render_dashboard_html(self) -> str:
        return render_time_tracking_dashboard_html(
            self.build_dashboard_payload(),
            editor_mode=True,
        )

    def build_work_dashboard_payload(self) -> dict[str, Any]:
        return asyncio.run(
            build_work_dashboard_payload(
                self._storage_root(),
                runtime=self.runtime,
                reference_now=self._reference_now(),
            )
        )

    def render_work_dashboard_html(self) -> str:
        return render_work_dashboard_html(self.build_work_dashboard_payload())

    def build_portfolio_payload(self) -> dict[str, Any]:
        return load_portfolio(self._storage_root())

    def render_portfolio_html(self) -> str:
        return render_portfolio(self.build_portfolio_payload())

    def build_payroll_dashboard_payload(self) -> dict[str, Any]:
        return build_payroll_dashboard_payload(self._storage_root())

    def render_payroll_dashboard_html(self) -> str:
        return render_payroll_dashboard_html(self.build_payroll_dashboard_payload())

    def build_system_health_payload(self) -> dict[str, Any]:
        return build_system_health_payload(self.runtime)

    def render_system_health_html(self) -> str:
        return render_system_health_html(self.build_system_health_payload())

    def build_manager_exceptions_payload(self) -> dict[str, Any]:
        return asyncio.run(
            build_manager_exceptions_payload(
                self.runtime,
                self._storage_root(),
                reference_now=self._reference_now(),
            )
        )

    def render_manager_exceptions_html(self) -> str:
        return render_manager_exceptions_html(
            self.build_manager_exceptions_payload()
        )

    def build_worker_portal_payload(self, token: str) -> dict[str, Any]:
        return asyncio.run(self.worker_portal.build_payload(token))

    def render_worker_portal_html(self, token: str) -> str:
        return self.worker_portal.render_html(self.build_worker_portal_payload(token))

    def apply_worker_portal_action(
        self,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return asyncio.run(self.worker_portal.apply_action(token, payload))

    def resolve_operational_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = str(payload.get("fingerprint") or "").strip()
        if not fingerprint:
            raise ValueError("fingerprint is required.")
        resolved = self.runtime.state_store.resolve_operational_issue(fingerprint)
        if not resolved:
            raise ValueError("The issue is already resolved or no longer exists.")
        return {"resolved": True, "fingerprint": fingerprint}

    def resolve_slack_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = str(payload.get("fingerprint") or "").strip()
        channel_id = str(payload.get("channel_id") or "").strip()
        resolved_by = str(payload.get("resolved_by") or "").strip()
        if not fingerprint or not channel_id or not resolved_by:
            raise ValueError("fingerprint, channel_id, and resolved_by are required.")
        issue = next(
            (
                item
                for item in self.runtime.state_store.list_operational_issues(
                    status="open",
                    limit=500,
                )
                if item.get("fingerprint") == fingerprint
            ),
            None,
        )
        if issue is None or issue.get("category") != "slack_route_uncertain":
            raise ValueError("The Slack routing issue is no longer available.")
        details = issue.get("details")
        details = details if isinstance(details, dict) else {}
        route = next(
            (
                item
                for item in self.runtime.config.slack.project_routes
                if item.channel_id == channel_id
            ),
            None,
        )
        override = self.runtime.set_slack_route_override(
            user_key=str(details.get("user_key") or ""),
            session_date=str(details.get("session_date") or ""),
            task_id=str(details.get("active_task_id") or ""),
            channel_id=channel_id,
            label=route.label if route else "",
            resolved_by=resolved_by,
        )
        self.runtime.state_store.resolve_operational_issue(fingerprint)
        return {"resolved": True, "override": override}

    def resolve_work_assignment(self, payload: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(
            self.runtime.apply_operator_task_correction(
                user_key=str(payload.get("user_key") or ""),
                session_date=str(payload.get("session_date") or ""),
                task_id=str(payload.get("task_id") or ""),
                corrected_by=str(payload.get("corrected_by") or ""),
                reason=str(payload.get("reason") or ""),
                now=self._reference_now(),
            )
        )

    def payroll_download(self, filename: str) -> Path | None:
        return resolve_payroll_download(self._storage_root(), filename)

    def resolve_payroll_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        dashboard = self.build_payroll_dashboard_payload()
        summary = dashboard.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        week_ending = str(summary.get("week_ending") or "")
        user_key = str(payload.get("user_key") or "").strip()
        session_date = str(payload.get("session_date") or "").strip()
        rows = dashboard.get("payroll_rows")
        rows = rows if isinstance(rows, list) else []
        row = next(
            (
                item
                for item in rows
                if str(item.get("user_key") or "") == user_key
                and str(item.get("session_date") or "") == session_date
            ),
            None,
        )
        if not week_ending or row is None:
            raise ValueError("The selected payroll review row is no longer available.")
        record = record_review_resolution(
            self._storage_root(),
            week_ending=week_ending,
            row=row,
            resolved_by=str(payload.get("resolved_by") or ""),
            note=str(payload.get("note") or ""),
            remember_similar=bool(payload.get("remember_similar")),
        )
        summary = asyncio.run(
            PayrollExporter(self.runtime).export(datetime.fromisoformat(week_ending).date())
        )
        return {"resolved": True, "record": record, "summary": summary}

    def preview_edit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(
            self.runtime.preview_manual_time_edit(
                str(payload.get("user_key") or ""),
                str(payload.get("session_date") or ""),
                _coerce_segments(payload.get("segments")),
                edited_by=str(payload.get("edited_by") or ""),
                reason=str(payload.get("reason") or ""),
                now=self._reference_now(),
            )
        )

    def apply_edit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(
            self.runtime.apply_manual_time_edit(
                str(payload.get("user_key") or ""),
                str(payload.get("session_date") or ""),
                _coerce_segments(payload.get("segments")),
                edited_by=str(payload.get("edited_by") or ""),
                reason=str(payload.get("reason") or ""),
                now=self._reference_now(),
            )
        )


def _coerce_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def build_request_handler(service: HoursEditorService, *, manager_key: str | None = None) -> type[BaseHTTPRequestHandler]:
    from .manager_auth import authorized, load_key
    key = manager_key if manager_key is not None else load_key()
    class HoursEditorHandler(BaseHTTPRequestHandler):
        server_version = "InternManagementHoursEditor/1.0"

        def _access(self) -> bool:
            parsed = urlparse(self.path)
            host = urlparse("http://" + self.headers.get("Host", ""))
            if (self.client_address[0] != "127.0.0.1" or host.hostname not in {"127.0.0.1", "localhost"}
                    or host.username or host.password or host.path or host.query or host.fragment):
                self._respond_error(HTTPStatus.FORBIDDEN, "Use the local manager address through SSH.")
                return False
            if self.command == "GET" and parsed.path == "/livez" and not parsed.query:
                return True  # Process readiness only; no operational/personnel data.
            if parsed.path not in {"/portal", "/api/portal-data", "/api/portal/action"}:
                if not authorized(self.headers.get("Authorization", ""), key):
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="Don Pollo manager", charset="UTF-8"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin is not None and origin != "http://" + self.headers.get("Host", ""):
                    self._respond_error(HTTPStatus.FORBIDDEN, "Cross-origin changes are not allowed.")
                    return False
                if self.headers.get_content_type() != "application/json":
                    self._respond_error(HTTPStatus.BAD_REQUEST, "Use application/json.")
                    return False
            return True

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._access():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/livez":
                self._respond_json({"ready": True})
                return
            query = parse_qs(parsed.query)
            token = str((query.get("token") or [""])[0])
            if parsed.path in {"/", "/time"}:
                self._respond_html(service.render_dashboard_html())
                return
            if parsed.path == "/work":
                self._respond_html(service.render_work_dashboard_html())
                return
            if parsed.path in {"/portfolio", "/api/portfolio-data"}:
                try:
                    if parsed.path == "/portfolio":
                        self._respond_html(service.render_portfolio_html())
                    else:
                        self._respond_json(service.build_portfolio_payload())
                except (OSError, ValueError):
                    self._respond_error(HTTPStatus.SERVICE_UNAVAILABLE, "Portfolio snapshot unavailable; timekeeping is independent.")
                return
            if parsed.path == "/payroll":
                self._respond_html(service.render_payroll_dashboard_html())
                return
            if parsed.path == "/health":
                self._respond_html(service.render_system_health_html())
                return
            if parsed.path == "/exceptions":
                self._respond_html(service.render_manager_exceptions_html())
                return
            if parsed.path == "/portal":
                try:
                    self._respond_html(service.render_worker_portal_html(token), no_store=True)
                except ValueError as exc:
                    self._respond_error(HTTPStatus.FORBIDDEN, str(exc))
                return
            if parsed.path == "/api/dashboard-data":
                self._respond_json(service.build_dashboard_payload())
                return
            if parsed.path == "/api/work-dashboard-data":
                self._respond_json(service.build_work_dashboard_payload())
                return
            if parsed.path == "/api/payroll-data":
                self._respond_json(service.build_payroll_dashboard_payload())
                return
            if parsed.path == "/api/health":
                self._respond_json(service.build_system_health_payload())
                return
            if parsed.path == "/api/exceptions":
                self._respond_json(service.build_manager_exceptions_payload())
                return
            if parsed.path == "/api/portal-data":
                try:
                    self._respond_json(service.build_worker_portal_payload(token), no_store=True)
                except ValueError as exc:
                    self._respond_error(HTTPStatus.FORBIDDEN, str(exc))
                return
            if parsed.path.startswith("/payroll/files/"):
                filename = parsed.path.rsplit("/", 1)[-1]
                download = service.payroll_download(filename)
                if download is None:
                    self._respond_error(HTTPStatus.NOT_FOUND, "Payroll file not found.")
                    return
                self._respond_file(download)
                return
            self._respond_error(HTTPStatus.NOT_FOUND, "Not found.")

        def do_POST(self) -> None:  # noqa: N802
            if not self._access():
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            token = str((query.get("token") or [""])[0])
            if parsed.path not in {
                "/api/edit-preview",
                "/api/edit-apply",
                "/api/payroll-review-resolve",
                "/api/issues/resolve",
                "/api/routes/resolve",
                "/api/work-assignment/resolve",
                "/api/portal/action",
            }:
                self._respond_error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            try:
                payload = self._read_json_body()
                if parsed.path == "/api/edit-preview":
                    result = service.preview_edit(payload)
                elif parsed.path == "/api/portal/action":
                    result = service.apply_worker_portal_action(token, payload)
                elif parsed.path == "/api/payroll-review-resolve":
                    result = service.resolve_payroll_review(payload)
                elif parsed.path == "/api/issues/resolve":
                    result = service.resolve_operational_issue(payload)
                elif parsed.path == "/api/routes/resolve":
                    result = service.resolve_slack_route(payload)
                elif parsed.path == "/api/work-assignment/resolve":
                    result = service.resolve_work_assignment(payload)
                else:
                    result = service.apply_edit(payload)
            except ValueError as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except RuntimeError as exc:
                self._respond_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._respond_json(result)

        def log_message(self, format: str, *args: Any) -> None:
            return None

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 65536 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid request body size or encoding.")
            raw = self.rfile.read(length)
            try:
                loaded = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Request body must be valid JSON.") from exc
            if not isinstance(loaded, dict):
                raise ValueError("Request body must be a JSON object.")
            return loaded

        def _respond_html(self, html: str, *, no_store: bool = False) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if no_store:
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _respond_json(self, payload: dict[str, Any], *, no_store: bool = False) -> None:
            encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _respond_file(self, path: Path) -> None:
            content = path.read_bytes()
            content_type = (
                "application/json"
                if path.suffix == ".json"
                else "text/csv; charset=utf-8"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"',
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _respond_error(self, status: HTTPStatus, message: str) -> None:
            encoded = json.dumps({"error": message}, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return HoursEditorHandler


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local dashboard for editing past workday hours.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Defaults to 8765.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Manager dashboard must bind to loopback; use an SSH tunnel for remote access.")
    load_dotenv()
    runtime = InternManagementRuntime()
    try:
        asyncio.run(runtime.refresh_configuration(force=True))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    service = HoursEditorService(runtime)
    server = ThreadingHTTPServer((args.host, args.port), build_request_handler(service))
    url = f"http://{args.host}:{args.port}/"
    print(f"Hours editor running at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
