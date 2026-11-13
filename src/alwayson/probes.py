"""Health probes: HTTP, TCP and command."""
from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol

from .config import CheckConfig
from .shell import run_shell


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    latency_ms: float
    detail: str


class Probe(Protocol):
    def check(self) -> ProbeResult: ...


def _ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 2)


class HttpProbe:
    def __init__(
        self,
        url: str,
        timeout: float,
        expect_status: Optional[List[int]] = None,
        expect_body: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        verify_tls: bool = True,
        method: str = "GET",
    ):
        self.url = url
        self.timeout = timeout
        self.expect_status = set(expect_status or [200])
        self.expect_body = expect_body
        self.headers = dict(headers or {})
        self.verify_tls = verify_tls
        self.method = method

    def check(self) -> ProbeResult:
        start = time.monotonic()
        request = urllib.request.Request(
            self.url, method=self.method, headers={"User-Agent": "alwayson", **self.headers}
        )
        context = None
        if self.url.startswith("https") and not self.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        want_body = bool(self.expect_body) and self.method != "HEAD"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as resp:
                status = resp.status
                body = resp.read(65536).decode("utf-8", "replace") if want_body else ""
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(65536).decode("utf-8", "replace") if want_body else ""
            except Exception:
                body = ""
        except Exception as exc:  # URLError, timeouts, TLS errors, ...
            return ProbeResult(False, _ms(start), f"{type(exc).__name__}: {exc}")
        latency = _ms(start)
        if status not in self.expect_status:
            return ProbeResult(False, latency, f"HTTP {status} (expected {sorted(self.expect_status)})")
        if self.expect_body and self.method != "HEAD" and self.expect_body not in body:
            return ProbeResult(False, latency, "response body did not contain the expected text")
        return ProbeResult(True, latency, f"HTTP {status}")


class TcpProbe:
    def __init__(self, host: str, port: int, timeout: float):
        self.host, self.port, self.timeout = host, port, timeout

    def check(self) -> ProbeResult:
        start = time.monotonic()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                pass
        except OSError as exc:
            return ProbeResult(False, _ms(start), f"{type(exc).__name__}: {exc}")
        return ProbeResult(True, _ms(start), f"connected to {self.host}:{self.port}")


class CommandProbe:
    def __init__(self, command: str, timeout: float):
        self.command, self.timeout = command, timeout

    def check(self) -> ProbeResult:
        start = time.monotonic()
        res = run_shell(self.command, self.timeout)
        if res.timed_out:
            return ProbeResult(False, _ms(start), f"timed out after {self.timeout}s")
        detail = f"exit {res.returncode}" + (f": {res.output[:200]}" if res.output else "")
        return ProbeResult(res.ok, _ms(start), detail)


def build_probe(check: CheckConfig) -> Probe:
    s = check.settings
    if check.type == "http":
        return HttpProbe(
            s["url"], check.timeout, s.get("expect_status"), s.get("expect_body"),
            s.get("headers"), s.get("verify_tls", True), s.get("method", "GET"),
        )
    if check.type == "tcp":
        return TcpProbe(str(s["host"]), int(s["port"]), check.timeout)
    if check.type == "command":
        return CommandProbe(s["command"], check.timeout)
    raise ValueError(f"unknown check type: {check.type}")
