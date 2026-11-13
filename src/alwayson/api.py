"""Small HTTP API: read-only status/metrics plus token-protected admin operations."""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .controller import ControllerError

if TYPE_CHECKING:  # pragma: no cover
    from .platform import Platform

log = logging.getLogger("alwayson")

ADMIN_ROUTE = re.compile(r"^/api/v1/services/([A-Za-z0-9_.-]+)/(failover|freeze|unfreeze)$")
MAX_BODY = 64 * 1024


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(snapshot: Dict[str, Dict[str, Any]]) -> str:
    lines = [
        "# TYPE alwayson_service_up gauge",
        "# TYPE alwayson_service_frozen gauge",
        "# TYPE alwayson_service_active gauge",
        "# TYPE alwayson_endpoint_up gauge",
        "# TYPE alwayson_endpoint_latency_ms gauge",
    ]
    for name, svc in snapshot.items():
        s = _label(name)
        up = 1 if svc["status"] in ("healthy", "degraded") else 0
        lines.append(f'alwayson_service_up{{service="{s}"}} {up}')
        lines.append(f'alwayson_service_frozen{{service="{s}"}} {1 if svc["frozen"] else 0}')
        for ep_name, ep in svc["endpoints"].items():
            e = _label(ep_name)
            lines.append(f'alwayson_service_active{{service="{s}",endpoint="{e}"}} '
                         f'{1 if svc["active"] == ep_name else 0}')
            lines.append(f'alwayson_endpoint_up{{service="{s}",endpoint="{e}"}} '
                         f'{1 if ep["status"] == "healthy" else 0}')
            if ep["latency_ms"] is not None:
                lines.append(f'alwayson_endpoint_latency_ms{{service="{s}",endpoint="{e}"}} {ep["latency_ms"]}')
    return "\n".join(lines) + "\n"


class ApiServer:
    def __init__(self, platform: "Platform"):
        self.platform = platform
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def start(self) -> None:
        cfg = self.platform.config.api
        self.httpd = ThreadingHTTPServer((cfg.host, cfg.port), _make_handler(self.platform, cfg.token))
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="api", daemon=True)
        self._thread.start()
        mode = "admin enabled" if cfg.token else "read-only (no token configured)"
        log.info("API listening on %s:%d (%s)", cfg.host, self.port, mode)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def _make_handler(platform: "Platform", token: Optional[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AlwaysOn"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("api: " + fmt, *args)

        def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
            body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/healthz":
                self._send(200, {"ok": True})
            elif url.path == "/api/v1/status":
                self._send(200, {"services": platform.snapshot()})
            elif url.path == "/api/v1/events":
                try:
                    limit = max(1, min(int(query.get("limit", ["100"])[0]), 1000))
                except ValueError:
                    return self._send(400, {"error": "limit must be an integer"})
                service = query.get("service", [None])[0]
                events = platform.events.recent(limit, service)
                self._send(200, {"events": [e.to_dict() for e in events]})
            elif url.path == "/metrics":
                self._send(200, render_metrics(platform.snapshot()), "text/plain; version=0.0.4")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not token:
                return self._send(403, {"error": "admin API disabled: no token configured"})
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return self._send(401, {"error": "invalid or missing bearer token"})
            match = ADMIN_ROUTE.match(urlparse(self.path).path)
            if not match:
                return self._send(404, {"error": "not found"})
            name, action = match.groups()
            controller = platform.controllers.get(name)
            if controller is None:
                return self._send(404, {"error": f"unknown service '{name}'"})
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": f"invalid request body: {exc}"})
            try:
                if action == "failover":
                    target = body.get("to")
                    if not isinstance(target, str):
                        return self._send(400, {"error": "field 'to' (endpoint name) is required"})
                    result = controller.manual_failover(target, bool(body.get("force", False)), "api")
                else:
                    result = controller.set_frozen(action == "freeze", "api")
            except ControllerError as exc:
                return self._send(409, {"error": str(exc)})
            self._send(200, result)

    return Handler
