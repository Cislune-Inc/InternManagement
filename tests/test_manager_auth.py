import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from agent.hours_editor import build_request_handler
from agent.manager_auth import auth_header, authorized, load_key, local_headers
from ops.protect_manager import protect

KEY = "test-only-credential-with-entropy-placeholder"


@pytest.fixture
def server():
    class Service:
        def render_dashboard_html(self):
            return "manager-control"
        def render_portfolio_html(self):
            return "portfolio-manager-only"
        def build_portfolio_payload(self):
            return {"schema_version": 1, "tasks": []}
        def build_worker_portal_payload(self, token):
            if token != "worker-signed-test-token":
                raise ValueError("Denied")
            return {"worker": "own-scope"}
        def preview_edit(self, payload):
            return {"preview": True}
    http = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(Service(), manager_key=KEY))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(http.server_port)
    http.shutdown()
    thread.join()
    http.server_close()


@pytest.mark.parametrize("path", ["/", "/time", "/work", "/portfolio", "/api/portfolio-data", "/payroll", "/health", "/exceptions",
    "/api/dashboard-data", "/api/work-dashboard-data", "/api/payroll-data", "/api/health", "/api/exceptions", "/payroll/files/private.csv"])
def test_every_manager_read_denies_anonymous_local_kiosk(server, path):
    response = requests.get(server + path, timeout=2)
    assert response.status_code == 401
    assert not response.content
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/api/edit-preview", "/api/edit-apply", "/api/payroll-review-resolve",
    "/api/issues/resolve", "/api/routes/resolve", "/api/work-assignment/resolve"])
def test_every_manager_write_denies_anonymous(server, path):
    assert requests.post(server + path, json={}, timeout=2).status_code == 401


def test_authenticated_control_origin_and_worker_token_boundary(server):
    headers = {"Authorization": auth_header(KEY)}
    assert requests.get(server + "/", headers=headers, timeout=2).text == "manager-control"
    assert requests.post(server + "/api/edit-preview", json={}, headers={**headers, "Origin": server}, timeout=2).status_code == 200
    for origin in ("null", "http://evil.example", server + ".evil.example"):
        assert requests.post(server + "/api/edit-preview", json={}, headers={**headers, "Origin": origin}, timeout=2).status_code == 403
    assert requests.post(server + "/api/edit-preview", data="{}", headers=headers, timeout=2).status_code == 400
    assert requests.get(server + "/", headers={**headers, "Host": "evil.example"}, timeout=2).status_code == 403
    assert requests.get(server + "/api/portal-data", timeout=2).status_code == 403
    assert requests.get(server + "/api/portal-data?token=worker-signed-test-token", timeout=2).json() == {"worker": "own-scope"}
    assert requests.get(server + "/api/health?token=worker-signed-test-token", timeout=2).status_code == 401
    assert requests.get(server + "/livez", timeout=2).json() == {"ready": True}


def test_missing_invalid_credentials_fail_closed_and_headers_stay_local(tmp_path, monkeypatch):
    for value in ("", "Basic ???", auth_header("wrong"), "Bearer " + KEY):
        assert not authorized(value, KEY)
    assert not authorized(auth_header(KEY), None)
    monkeypatch.chdir(tmp_path)
    for name in ("config", "data", "storage", "backups", "secrets"):
        (tmp_path / name).mkdir()
    protect(tmp_path, password=KEY)
    assert load_key() == KEY
    assert local_headers("http://127.0.0.1:8765/api/health")
    for url in ("https://evil.example", "http://127.0.0.1:9876/", "http://localhost:8765/", "http://127.0.0.1@evil.example:8765/"):
        assert local_headers(url) == {}
    (tmp_path / "secrets").chmod(0o755)
    assert load_key() is None


def test_portfolio_requires_manager_auth_and_has_no_write_route(server):
    headers = {"Authorization": auth_header(KEY)}
    assert requests.get(server + "/portfolio", headers=headers, timeout=2).text == "portfolio-manager-only"
    assert requests.get(server + "/api/portfolio-data", headers=headers, timeout=2).json()["tasks"] == []
    assert requests.get(server + "/portfolio?token=worker-signed-test-token", timeout=2).status_code == 401
    assert requests.post(server + "/api/portfolio-data", headers=headers, json={}, timeout=2).status_code == 404
