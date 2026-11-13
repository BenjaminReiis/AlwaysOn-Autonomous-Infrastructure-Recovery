"""Command line interface."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .platform import Platform
from .probes import build_probe

DEFAULT_EXAMPLE = {
    "state_file": "./.alwayson/state.json",
    "event_log": "./.alwayson/events.jsonl",
    "api": {"enabled": True, "host": "127.0.0.1", "port": 8480, "token_env": "ALWAYSON_TOKEN"},
    "defaults": {"interval": 5, "timeout": 3, "failure_threshold": 3, "success_threshold": 2},
    "services": [
        {
            "name": "web",
            "failback": {"mode": "auto", "stable_seconds": 60},
            "recovery": {
                "actions": [{"type": "command", "command": "echo restarting $ALWAYSON_ENDPOINT"}],
                "max_attempts": 3,
                "backoff_seconds": [5, 15, 45],
                "cooldown_seconds": 300,
            },
            "on_failover": [{"type": "command", "command": "echo switching traffic to $ALWAYSON_TO"}],
            "endpoints": [
                {"name": "primary", "priority": 1, "address": "10.0.0.10:80",
                 "check": {"type": "http", "url": "http://10.0.0.10/healthz"}},
                {"name": "standby", "priority": 2, "address": "10.0.1.10:80",
                 "check": {"type": "http", "url": "http://10.0.1.10/healthz"}},
            ],
        }
    ],
}


def _load(args) -> Config:
    return load_config(args.config)


def cmd_init(args) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.write_text(json.dumps(DEFAULT_EXAMPLE, indent=2) + "\n", encoding="utf-8")
    print(f"created {path}. Edit it, then run: alwayson validate && alwayson check")
    return 0


def cmd_validate(args) -> int:
    config = _load(args)
    for svc in config.services:
        names = ", ".join(f"{e.name}(p{e.priority})" for e in sorted(svc.endpoints, key=lambda e: e.priority))
        print(f"[{svc.name}] OK: {names}")
    print(f"configuration valid: {len(config.services)} service(s)")
    return 0


def cmd_check(args) -> int:
    """Probe every endpoint once. Changes nothing and runs no actions."""
    config = _load(args)
    failed = 0
    for svc in config.services:
        for ep in svc.endpoints:
            result = build_probe(ep.check).check()
            failed += 0 if result.ok else 1
            flag = "UP  " if result.ok else "DOWN"
            print(f"[{svc.name}/{ep.name}] {flag} {result.latency_ms:.0f}ms {result.detail}")
    return 1 if failed else 0


def cmd_run(args) -> int:
    config = _load(args)
    Platform(config).run_forever()
    return 0


def cmd_status(args) -> int:
    config = _load(args)
    if not config.state_file or not config.state_file.exists():
        print("no state file yet (is `alwayson run` running?)")
        return 1
    data = json.loads(config.state_file.read_text(encoding="utf-8"))
    print(f"state updated: {data.get('updated', '?')}")
    for name, svc in data.get("services", {}).items():
        flags = (" FROZEN" if svc.get("frozen") else "") + (" PINNED" if svc.get("pinned") else "")
        print(f"[{name}] {svc.get('status', '?').upper()} active={svc.get('active')}{flags}")
        for ep_name, ep in svc.get("endpoints", {}).items():
            mark = "*" if ep_name == svc.get("active") else " "
            print(f"  {mark} {ep_name:<16} {ep['status']:<10} {ep.get('detail', '')}")
    return 0


def cmd_events(args) -> int:
    config = _load(args)
    if not config.event_log or not config.event_log.exists():
        print("no event log yet")
        return 1
    lines = config.event_log.read_text(encoding="utf-8").splitlines()
    shown = 0
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if args.service and ev.get("service") != args.service:
            continue
        print(f"{ev['time']} [{ev['service']}] {ev['type']}: {ev['message']}")
        shown += 1
        if shown >= args.tail:
            break
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alwayson", description="Autonomous infrastructure recovery and failover.")
    p.add_argument("-c", "--config", default="alwayson.json", help="configuration file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"alwayson {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init", help="write an example configuration")
    s.add_argument("--force", action="store_true")
    sub.add_parser("validate", help="validate the configuration")
    sub.add_parser("check", help="probe every endpoint once (no side effects)")
    sub.add_parser("run", help="run the platform (monitor, recover, fail over)")
    sub.add_parser("status", help="show the last published state")
    s = sub.add_parser("events", help="show recent events")
    s.add_argument("--tail", type=int, default=20)
    s.add_argument("--service")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    handlers = {
        "init": cmd_init, "validate": cmd_validate, "check": cmd_check,
        "run": cmd_run, "status": cmd_status, "events": cmd_events,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
