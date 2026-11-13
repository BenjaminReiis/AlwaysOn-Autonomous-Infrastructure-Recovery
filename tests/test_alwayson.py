import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from alwayson.actions import ActionResult, ActionRunner
from alwayson.cli import main
from alwayson.config import ConfigError, parse_config
from alwayson.controller import ControllerError, ServiceController
from alwayson.events import EventLog
from alwayson.platform import Platform
from alwayson.probes import CommandProbe, HttpProbe, ProbeResult, TcpProbe
from alwayson.state import StateStore


# ------------------------------------------------------------------ test doubles
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeProbe:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self):
        return ProbeResult(self.ok, 1.5, "fake ok" if self.ok else "fake down")


class FakeRunner(ActionRunner):
    def __init__(self):
        self.calls = []
        self.fail = False

    def run(self, action, context):
        self.calls.append((action.command or action.url, dict(context)))
        return ActionResult(not self.fail, "fake")

    def commands(self, name):
        return [c for c in self.calls if c[0] == name]


def service_cfg(**overrides):
    raw = {
        "name": "web",
        "interval": 5,
        "failure_threshold": 2,
        "success_threshold": 1,
        "failback": {"mode": "auto", "stable_seconds": 30},
        "recovery": {
            "actions": [{"type": "command", "command": "restart"}],
            "max_attempts": 2,
            "backoff_seconds": [10, 20],
            "cooldown_seconds": 100,
        },
        "on_failover": [{"type": "command", "command": "switch"}],
        "endpoints": [
            {"name": "primary", "priority": 1, "address": "p:80", "check": {"type": "command", "command": "true"}},
            {"name": "standby", "priority": 2, "address": "s:80", "check": {"type": "command", "command": "true"}},
        ],
    }
    raw.update(overrides)
    return parse_config({"services": [raw]}, Path(".")).services[0]


class Harness:
    def __init__(self, store=None, **overrides):
        self.clock = FakeClock()
        self.events = EventLog()
        self.runner = FakeRunner()
        self.probes = {"primary": FakeProbe(), "standby": FakeProbe()}
        self.ctl = ServiceController(
            service_cfg(**overrides), self.events, store, self.runner,
            lambda ep: self.probes[ep.name], self.clock, self.clock,
        )

    def step(self, n=1, dt=5.0):
        for _ in range(n):
            self.ctl.tick()
            self.clock.advance(dt)

    def types(self):
        return [e.type for e in self.events.recent(1000)]

    @property
    def active(self):
        return self.ctl.state.active

    @property
    def status(self):
        return self.ctl.state.status


# ----------------------------------------------------------------- controller
class TestController(unittest.TestCase):
    def test_initial_election_prefers_priority(self):
        h = Harness()
        h.step(1)
        self.assertEqual(h.active, "primary")
        self.assertEqual(h.status, "healthy")
        _, ctx = h.runner.commands("switch")[0]
        self.assertEqual((ctx["event"], ctx["to"], ctx["address"]), ("election", "primary", "p:80"))

    def test_failover_to_standby_and_background_recovery(self):
        h = Harness()
        h.step(1)
        h.probes["primary"].ok = False
        h.step(2)  # failure_threshold = 2
        self.assertEqual(h.active, "standby")
        self.assertEqual(h.status, "degraded")
        self.assertIn("failover", h.types())
        ctx = h.runner.commands("switch")[-1][1]
        self.assertEqual((ctx["event"], ctx["from"], ctx["to"]), ("failover", "primary", "standby"))
        restarts = h.runner.commands("restart")
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0][1]["endpoint"], "primary")

    def test_outage_when_no_healthy_candidate(self):
        h = Harness()
        h.step(1)
        h.probes["primary"].ok = h.probes["standby"].ok = False
        h.step(3)
        self.assertEqual(h.active, "primary")  # nothing better to switch to
        self.assertEqual(h.status, "outage")
        self.assertNotIn("failover", h.types())

    def test_failover_delay_waits(self):
        h = Harness(failover_delay=20)
        h.step(1)
        h.probes["primary"].ok = False
        h.step(3, dt=5)  # unhealthy at ~t=1010, delay not yet over
        self.assertEqual(h.active, "primary")
        h.step(5, dt=5)
        self.assertEqual(h.active, "standby")

    def test_recovery_backoff_max_attempts_and_cooldown(self):
        h = Harness()
        h.step(1)
        h.probes["primary"].ok = False
        h.step(20)  # lots of ticks within the cooldown window
        self.assertEqual(len(h.runner.commands("restart")), 2)  # max_attempts
        h.clock.advance(150)  # beyond cooldown_seconds
        h.step(1)
        self.assertEqual(len(h.runner.commands("restart")), 3)

    def test_recovery_ends_when_endpoint_returns(self):
        h = Harness()
        h.step(1)
        h.probes["primary"].ok = False
        h.step(2)
        h.probes["primary"].ok = True
        h.step(1)
        self.assertEqual(h.ctl.state.endpoints["primary"].recovery_attempts, 0)
        up = [e for e in h.events.recent(100) if e.type == "endpoint_up"][-1]
        self.assertIn("recovery attempt", up.message)

    def test_auto_failback_after_stable_period(self):
        h = Harness()
        h.step(1)
        h.probes["primary"].ok = False
        h.step(2)
        self.assertEqual(h.active, "standby")
        h.probes["primary"].ok = True
        h.step(1)
        self.assertEqual(h.active, "standby")  # not stable yet
        h.step(8)
        self.assertEqual(h.active, "primary")
        self.assertIn("failback", h.types())
        self.assertEqual(h.status, "healthy")

    def test_manual_failback_mode_never_returns_by_itself(self):
        h = Harness(failback={"mode": "manual"})
        h.step(1)
        h.probes["primary"].ok = False
        h.step(2)
        h.probes["primary"].ok = True
        h.step(30)
        self.assertEqual(h.active, "standby")

    def test_flap_protection_freezes_automation(self):
        h = Harness(
            failback={"mode": "auto", "stable_seconds": 0},
            flap_protection={"max_switches": 2, "window_seconds": 600},
        )
        h.step(1)
        p = h.probes["primary"]
        p.ok = False
        h.step(2)  # switch 1: failover
        p.ok = True
        h.step(1)  # switch 2: failback
        self.assertEqual(h.active, "primary")
        p.ok = False
        h.step(3)  # would be switch 3 -> frozen instead
        self.assertTrue(h.ctl.state.frozen)
        self.assertEqual(h.active, "primary")
        self.assertIn("frozen", h.types())
        h.ctl.set_frozen(False)
        h.step(1)
        self.assertEqual(h.active, "standby")

    def test_freeze_blocks_automation(self):
        h = Harness()
        h.step(1)
        h.ctl.set_frozen(True)
        h.probes["primary"].ok = False
        h.step(5)
        self.assertEqual(h.active, "primary")
        h.ctl.set_frozen(False)
        h.step(1)
        self.assertEqual(h.active, "standby")

    def test_hook_failure_is_retried_then_gives_up(self):
        h = Harness(hook_max_attempts=3, hook_retry_seconds=10)
        h.runner.fail = True
        h.step(30, dt=5)
        self.assertEqual(len(h.runner.commands("switch")), 3)
        self.assertEqual(h.types().count("hook_failed"), 3)
        self.assertIn("hook_gave_up", h.types())

    def test_hook_recovers_on_retry(self):
        h = Harness(hook_retry_seconds=10)
        h.runner.fail = True
        h.step(1)
        h.runner.fail = False
        h.step(5, dt=5)
        self.assertIn("hooks_ok", h.types())
        self.assertIsNone(h.ctl.state.pending_hooks)

    def test_manual_failover_pins_and_validates(self):
        h = Harness()
        h.step(1)
        with self.assertRaises(ControllerError):
            h.ctl.manual_failover("nope")
        with self.assertRaises(ControllerError):
            h.ctl.manual_failover("primary")  # already active
        h.probes["standby"].ok = False
        h.step(2)
        with self.assertRaises(ControllerError):
            h.ctl.manual_failover("standby")  # unhealthy
        h.ctl.manual_failover("standby", force=True)
        self.assertEqual(h.active, "standby")
        self.assertTrue(h.ctl.state.pinned)
        h.probes["standby"].ok = True
        h.step(30)
        self.assertEqual(h.active, "standby")  # pinned: no automatic failback

    def test_state_is_restored(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "state.json"
        h1 = Harness(store=StateStore(path))
        h1.step(1)
        h1.ctl.manual_failover("standby")
        h1.ctl.set_frozen(True)
        h2 = Harness(store=StateStore(path))
        self.assertEqual(h2.active, "standby")
        self.assertTrue(h2.ctl.state.frozen)
        self.assertTrue(h2.ctl.state.pinned)


# ----------------------------------------------------------------- probes/actions
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/ok" else 503
        body = b"all systems go" if self.path == "/ok" else b"down"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class TestProbesAndActions(unittest.TestCase):
    def setUp(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _H)
        self.httpd.received = []
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def test_http_probe(self):
        self.assertTrue(HttpProbe(self.base + "/ok", 2).check().ok)
        self.assertTrue(HttpProbe(self.base + "/ok", 2, expect_body="systems go").check().ok)
        self.assertFalse(HttpProbe(self.base + "/ok", 2, expect_body="nope").check().ok)
        bad = HttpProbe(self.base + "/bad", 2).check()
        self.assertFalse(bad.ok)
        self.assertIn("503", bad.detail)
        self.assertTrue(HttpProbe(self.base + "/bad", 2, expect_status=[503]).check().ok)
        self.assertFalse(HttpProbe("http://127.0.0.1:1/", 1).check().ok)

    def test_tcp_probe(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        self.assertTrue(TcpProbe("127.0.0.1", port, 1).check().ok)
        srv.close()
        self.assertFalse(TcpProbe("127.0.0.1", port, 1).check().ok)

    def test_command_probe(self):
        self.assertTrue(CommandProbe("exit 0", 2).check().ok)
        self.assertIn("exit 3", CommandProbe("echo boom; exit 3", 2).check().detail)
        start = time.monotonic()
        res = CommandProbe("sleep 5", 0.3).check()
        self.assertFalse(res.ok)
        self.assertLess(time.monotonic() - start, 3)

    def test_command_action_gets_context_env(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        cfg = service_cfg(on_failover=[
            {"type": "command", "command": f'echo "$ALWAYSON_SERVICE:$ALWAYSON_TO:$ALWAYSON_FROM" > {out}/r.txt'}
        ])
        ok, _ = ActionRunner().run_all(cfg.on_failover, {"service": "web", "to": "standby", "from": "primary"})
        self.assertTrue(ok)
        self.assertEqual((out / "r.txt").read_text().strip(), "web:standby:primary")

    def test_webhook_action_and_failure(self):
        cfg = service_cfg(on_failover=[{"type": "webhook", "url": self.base + "/hook"}])
        ok, _ = ActionRunner().run_all(cfg.on_failover, {"service": "web", "event": "failover"})
        self.assertTrue(ok)
        self.assertEqual(self.httpd.received[0]["event"], "failover")
        bad = service_cfg(on_failover=[{"type": "webhook", "url": "http://127.0.0.1:1/x", "timeout": 1}])
        self.assertFalse(ActionRunner().run_all(bad.on_failover, {})[0])

    def test_run_all_stops_at_first_failure(self):
        cfg = service_cfg(on_failover=[
            {"type": "command", "command": "exit 1"},
            {"type": "command", "command": "echo never"},
        ])
        ok, detail = ActionRunner().run_all(cfg.on_failover, {})
        self.assertFalse(ok)
        self.assertNotIn("never", detail)


# ---------------------------------------------------------------------- config
class TestConfig(unittest.TestCase):
    def bad(self, **svc):
        raw = {"name": "w", "endpoints": [{"name": "a", "check": {"type": "command", "command": "true"}}]}
        raw.update(svc)
        with self.assertRaises(ConfigError):
            parse_config({"services": [raw]}, Path("."))

    def test_validation(self):
        with self.assertRaises(ConfigError):
            parse_config({}, Path("."))
        self.bad(name="bad name!")
        self.bad(endpoints=[])
        self.bad(failback={"mode": "sometimes"})
        self.bad(interval=0)
        self.bad(recovery={"actions": []})
        self.bad(on_failover=[{"type": "webhook", "url": "ftp://x"}])
        self.bad(endpoints=[{"name": "a", "check": {"type": "tcp", "host": "h", "port": 99999}}])
        self.bad(endpoints=[{"name": "a", "check": {"type": "http", "url": "nope"}}])
        self.bad(endpoints=[
            {"name": "a", "check": {"type": "command", "command": "true"}},
            {"name": "a", "check": {"type": "command", "command": "true"}},
        ])

    def test_defaults_and_overrides(self):
        raw = {
            "defaults": {"interval": 7, "failure_threshold": 4},
            "services": [{"name": "w", "interval": 2, "endpoints": [
                {"name": "a", "check": {"type": "command", "command": "true"}}]}],
        }
        svc = parse_config(raw, Path(".")).services[0]
        self.assertEqual((svc.interval, svc.failure_threshold), (2.0, 4))

    def test_api_token_from_env(self):
        os.environ["AO_TEST_TOKEN"] = "s3cret"
        self.addCleanup(os.environ.pop, "AO_TEST_TOKEN", None)
        raw = {"api": {"enabled": True, "token_env": "AO_TEST_TOKEN"}, "services": [
            {"name": "w", "endpoints": [{"name": "a", "check": {"type": "command", "command": "true"}}]}]}
        self.assertEqual(parse_config(raw, Path(".")).api.token, "s3cret")


# ------------------------------------------------------------------------- api
def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class TestApi(unittest.TestCase):
    def make(self, token):
        if token:
            os.environ["AO_API_TOKEN"] = token
            self.addCleanup(os.environ.pop, "AO_API_TOKEN", None)
        raw = {
            "api": {"enabled": True, "port": 0, "token_env": "AO_API_TOKEN"},
            "services": [{
                "name": "web", "failure_threshold": 1, "success_threshold": 1,
                "endpoints": [
                    {"name": "primary", "check": {"type": "command", "command": "true"}},
                    {"name": "standby", "check": {"type": "command", "command": "true"}},
                ],
            }],
        }
        probes = {"primary": FakeProbe(), "standby": FakeProbe()}
        platform = Platform(parse_config(raw, Path(".")), FakeRunner(), lambda ep: probes[ep.name])
        platform.start_api()
        self.addCleanup(platform.stop)
        platform.controllers["web"].tick()
        return platform, f"http://127.0.0.1:{platform.api.port}"

    def test_read_endpoints_and_metrics(self):
        _, base = self.make(token=None)
        self.assertEqual(http("GET", base + "/healthz")[0], 200)
        status = json.loads(http("GET", base + "/api/v1/status")[1])
        self.assertEqual(status["services"]["web"]["active"], "primary")
        events = json.loads(http("GET", base + "/api/v1/events?limit=5&service=web")[1])
        self.assertTrue(events["events"])
        metrics = http("GET", base + "/metrics")[1]
        self.assertIn('alwayson_service_active{service="web",endpoint="primary"} 1', metrics)
        self.assertEqual(http("GET", base + "/nope")[0], 404)

    def test_admin_disabled_without_token(self):
        _, base = self.make(token=None)
        self.assertEqual(http("POST", base + "/api/v1/services/web/freeze", token="x")[0], 403)

    def test_admin_operations(self):
        platform, base = self.make(token="s3cret")
        url = base + "/api/v1/services/web"
        self.assertEqual(http("POST", url + "/freeze")[0], 401)
        self.assertEqual(http("POST", url + "/freeze", token="wrong")[0], 401)
        code, body = http("POST", url + "/failover", token="s3cret", body={"to": "standby"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["active"], "standby")
        self.assertEqual(http("POST", url + "/failover", token="s3cret", body={"to": "ghost"})[0], 409)
        self.assertEqual(http("POST", url + "/failover", token="s3cret", body={})[0], 400)
        self.assertEqual(http("POST", base + "/api/v1/services/ghost/freeze", token="s3cret")[0], 404)
        self.assertEqual(http("POST", url + "/freeze", token="s3cret")[0], 200)
        self.assertTrue(platform.controllers["web"].state.frozen)
        self.assertEqual(http("POST", url + "/unfreeze", token="s3cret")[0], 200)
        self.assertFalse(platform.controllers["web"].state.frozen)


# ----------------------------------------------------------- end-to-end / CLI
def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestEndToEnd(unittest.TestCase):
    def test_real_failover_with_tcp_endpoints(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        servers = {}
        for name in ("primary", "standby"):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            s.listen()
            servers[name] = s
            self.addCleanup(s.close)
        marker = tmp / "switched.txt"
        raw = {
            "state_file": "state.json",
            "event_log": "events.jsonl",
            "services": [{
                "name": "svc", "interval": 0.1, "timeout": 0.5,
                "failure_threshold": 2, "success_threshold": 1,
                "failback": {"mode": "manual"},
                "on_failover": [{"type": "command", "command": f'echo "$ALWAYSON_TO" >> {marker}'}],
                "endpoints": [
                    {"name": n, "priority": i + 1,
                     "check": {"type": "tcp", "host": "127.0.0.1", "port": servers[n].getsockname()[1]}}
                    for i, n in enumerate(("primary", "standby"))
                ],
            }],
        }
        platform = Platform(parse_config(raw, tmp))
        platform.start()
        self.addCleanup(platform.stop)
        ctl = platform.controllers["svc"]
        self.assertTrue(wait_for(lambda: ctl.state.active == "primary"))
        servers["primary"].close()
        self.assertTrue(wait_for(lambda: ctl.state.active == "standby"), "failover did not happen")
        self.assertTrue(wait_for(lambda: marker.exists() and "standby" in marker.read_text()))
        self.assertEqual(ctl.state.status, "degraded")
        types = [e.type for e in platform.events.recent(100)]
        self.assertIn("failover", types)
        saved = json.loads((tmp / "state.json").read_text())
        self.assertEqual(saved["services"]["svc"]["active"], "standby")
        self.assertIn("failover", (tmp / "events.jsonl").read_text())


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = str(self.tmp / "alwayson.json")

    def test_init_validate(self):
        self.assertEqual(main(["-c", self.cfg, "init"]), 0)
        self.assertEqual(main(["-c", self.cfg, "init"]), 1)
        self.assertEqual(main(["-c", self.cfg, "validate"]), 0)
        self.assertEqual(main(["-c", str(self.tmp / "missing.json"), "validate"]), 2)

    def test_check_reports_down_endpoints(self):
        raw = {"services": [{"name": "w", "endpoints": [
            {"name": "up", "check": {"type": "command", "command": "exit 0"}},
            {"name": "down", "check": {"type": "command", "command": "exit 1"}}]}]}
        Path(self.cfg).write_text(json.dumps(raw))
        self.assertEqual(main(["-c", self.cfg, "check"]), 1)
        raw["services"][0]["endpoints"].pop()
        Path(self.cfg).write_text(json.dumps(raw))
        self.assertEqual(main(["-c", self.cfg, "check"]), 0)

    def test_status_and_events_without_data(self):
        raw = {"state_file": "s.json", "event_log": "e.jsonl", "services": [{"name": "w", "endpoints": [
            {"name": "a", "check": {"type": "command", "command": "true"}}]}]}
        Path(self.cfg).write_text(json.dumps(raw))
        self.assertEqual(main(["-c", self.cfg, "status"]), 1)
        self.assertEqual(main(["-c", self.cfg, "events"]), 1)
        ctl = Platform(parse_config(raw, self.tmp)).controllers["w"]
        ctl.tick()
        ctl.tick()  # success_threshold defaults to 2
        self.assertEqual(main(["-c", self.cfg, "status"]), 0)
        self.assertEqual(main(["-c", self.cfg, "events", "--tail", "5"]), 0)


if __name__ == "__main__":
    unittest.main()
