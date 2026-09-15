"""Owner-machine payroll preview over authenticated SSH; no LAN listener or key copy.

Only reconciliation reads and draft notes are forwarded, never attendance/payroll
mutations. Run on the trusted administrator's computer, not the shared kiosk.
"""
import argparse
import json
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

REMOTE = '''import json,sys,urllib.request,urllib.error
from agent.manager_auth import local_headers
r=json.load(sys.stdin)
url="http://127.0.0.1:8765"+r["path"]
headers=local_headers(url);headers["Content-Type"]="application/json"
req=urllib.request.Request(url,data=r["body"].encode() if r["method"]=="POST" else None,headers=headers,method=r["method"])
try:
 response=urllib.request.urlopen(req,timeout=25)
except urllib.error.HTTPError as exc:
 response=exc
print(json.dumps({"status":response.code,"type":response.headers.get("Content-Type","text/plain"),"body":response.read().decode()}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Verified Mini address")
    parser.add_argument("--port", type=int, default=18966)
    args = parser.parse_args()
    ssh = ["ssh", "-o", "HostName=" + args.host, "-o", "HostKeyAlias=cmacmini.local", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "don-pollo-mini", "cd /Users/pm/InternManagement && .venv/bin/python -c " + shlex.quote(REMOTE)]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            expected = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            path = urlsplit(self.path)
            if host not in expected or (origin and origin != "http://" + host) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_error(403)
                return
            allowed = path.path in {"/reconcile", "/api/reconciliation"} if self.command == "GET" else path.path == "/api/reconciliation/draft"
            if not allowed:
                self.send_error(404)
                return
            if self.command == "POST" and self.headers.get_content_type() != "application/json":
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid body")
                body = self.rfile.read(length).decode() if length else ""
                result = subprocess.run(ssh, input=json.dumps({"method": self.command, "path": self.path, "body": body}), text=True, capture_output=True, timeout=35, check=True)
                response = json.loads(result.stdout)
                content = response["body"].replace('<a href="/payroll">Legacy export</a>', '').encode()
                self.send_response(response["status"])
                self.send_header("Content-Type", response["type"])
            except (ValueError, subprocess.SubprocessError):
                content = b'{"error":"Mini unavailable. Reconnect shop network/VPN and retry; no draft was confirmed saved."}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Owner-only local preview: http://127.0.0.1:{args.port}/reconcile", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
