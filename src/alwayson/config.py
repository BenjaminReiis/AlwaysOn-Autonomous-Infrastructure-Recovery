"""Configuration loading and validation (JSON)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


class ConfigError(Exception):
    """Invalid configuration."""


NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CHECK_REQUIRED = {"http": ("url",), "tcp": ("host", "port"), "command": ("command",)}
DEFAULT_KEYS = ("interval", "timeout", "failure_threshold", "success_threshold")


@dataclass
class CheckConfig:
    type: str  # "http" | "tcp" | "command"
    timeout: float
    settings: Dict[str, Any]


@dataclass
class ActionConfig:
    type: str  # "command" | "webhook"
    timeout: float = 30.0
    command: Optional[str] = None
    url: Optional[str] = None


@dataclass
class RecoveryPolicy:
    actions: List[ActionConfig]
    max_attempts: int = 3
    backoff_seconds: List[float] = field(default_factory=lambda: [5.0, 15.0, 45.0])
    cooldown_seconds: float = 300.0


@dataclass
class EndpointConfig:
    name: str
    priority: int
    check: CheckConfig
    address: Optional[str] = None
    recovery: Optional[RecoveryPolicy] = None


@dataclass
class FailbackConfig:
    mode: str = "auto"  # "auto" | "manual"
    stable_seconds: float = 60.0


@dataclass
class FlapConfig:
    max_switches: int = 5
    window_seconds: float = 600.0


@dataclass
class ServiceConfig:
    name: str
    endpoints: List[EndpointConfig]
    interval: float = 5.0
    failure_threshold: int = 3
    success_threshold: int = 2
    failover_delay: float = 0.0
    failback: FailbackConfig = field(default_factory=FailbackConfig)
    flap: FlapConfig = field(default_factory=FlapConfig)
    recovery: Optional[RecoveryPolicy] = None
    on_failover: List[ActionConfig] = field(default_factory=list)
    hook_max_attempts: int = 5
    hook_retry_seconds: float = 10.0


@dataclass
class ApiConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8480
    token: Optional[str] = None


@dataclass
class Config:
    services: List[ServiceConfig]
    state_file: Optional[Path] = None
    event_log: Optional[Path] = None
    api: ApiConfig = field(default_factory=ApiConfig)


# --------------------------------------------------------------------- helpers
def _obj(value: Any, ctx: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx}: must be an object")
    return value


def _number(raw: Dict[str, Any], key: str, default: Any, ctx: str,
            minimum: float = 0.0, integer: bool = False):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{ctx}.{key}: must be a number")
    if value < minimum:
        raise ConfigError(f"{ctx}.{key}: must be >= {minimum}")
    if integer:
        if int(value) != value:
            raise ConfigError(f"{ctx}.{key}: must be an integer")
        return int(value)
    return float(value)


def _name(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ConfigError(f"{ctx}.name: required, letters/digits/'_'/'.'/'-' only")
    return value


def _http_url(value: Any, ctx: str) -> str:
    parsed = urlparse(value) if isinstance(value, str) else None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{ctx}: must be an http(s) URL")
    return value


# --------------------------------------------------------------------- parsers
def parse_action(raw: Any, ctx: str) -> ActionConfig:
    raw = _obj(raw, ctx)
    kind = raw.get("type")
    if kind == "command":
        cmd = raw.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ConfigError(f"{ctx}.command: must be a non-empty string")
        return ActionConfig("command", _number(raw, "timeout", 30, ctx, 0.1), command=cmd)
    if kind == "webhook":
        url = _http_url(raw.get("url"), f"{ctx}.url")
        return ActionConfig("webhook", _number(raw, "timeout", 10, ctx, 0.1), url=url)
    raise ConfigError(f"{ctx}.type: must be 'command' or 'webhook'")


def parse_actions(raw: Any, ctx: str, allow_empty: bool = False) -> List[ActionConfig]:
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise ConfigError(f"{ctx}: must be a non-empty list of actions")
    return [parse_action(a, f"{ctx}[{i}]") for i, a in enumerate(raw)]


def parse_recovery(raw: Any, ctx: str) -> RecoveryPolicy:
    raw = _obj(raw, ctx)
    actions = parse_actions(raw.get("actions"), f"{ctx}.actions")
    backoff = raw.get("backoff_seconds", [5, 15, 45])
    if (
        not isinstance(backoff, list)
        or not backoff
        or any(isinstance(b, bool) or not isinstance(b, (int, float)) or b < 0 for b in backoff)
    ):
        raise ConfigError(f"{ctx}.backoff_seconds: must be a non-empty list of numbers >= 0")
    return RecoveryPolicy(
        actions=actions,
        max_attempts=_number(raw, "max_attempts", 3, ctx, 1, integer=True),
        backoff_seconds=[float(b) for b in backoff],
        cooldown_seconds=_number(raw, "cooldown_seconds", 300, ctx, 0),
    )


def parse_check(raw: Any, ctx: str, default_timeout: float) -> CheckConfig:
    raw = _obj(raw, ctx)
    kind = raw.get("type")
    if kind not in CHECK_REQUIRED:
        raise ConfigError(f"{ctx}.type: must be one of {sorted(CHECK_REQUIRED)}")
    for key in CHECK_REQUIRED[kind]:
        if key not in raw:
            raise ConfigError(f"{ctx}: missing required field '{key}'")
    timeout = _number(raw, "timeout", default_timeout, ctx, 0.1)
    settings = {k: v for k, v in raw.items() if k not in ("type", "timeout")}

    if kind == "http":
        _http_url(settings["url"], f"{ctx}.url")
        status = settings.get("expect_status", 200)
        statuses = status if isinstance(status, list) else [status]
        if not statuses or any(
            isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599 for s in statuses
        ):
            raise ConfigError(f"{ctx}.expect_status: must be an HTTP status code or a list of them")
        settings["expect_status"] = statuses
        if settings.get("method", "GET") not in ("GET", "HEAD"):
            raise ConfigError(f"{ctx}.method: must be GET or HEAD")
    elif kind == "tcp":
        port = settings["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{ctx}.port: must be an integer between 1 and 65535")
        if not isinstance(settings["host"], str) or not settings["host"]:
            raise ConfigError(f"{ctx}.host: must be a non-empty string")
    else:
        if not isinstance(settings["command"], str) or not settings["command"].strip():
            raise ConfigError(f"{ctx}.command: must be a non-empty string")
    return CheckConfig(kind, timeout, settings)


def parse_service(raw: Any, defaults: Dict[str, Any], ctx: str) -> ServiceConfig:
    raw = _obj(raw, ctx)
    name = _name(raw.get("name"), ctx)
    merged = {**defaults, **{k: raw[k] for k in DEFAULT_KEYS if k in raw}}
    interval = _number(merged, "interval", 5, ctx, 0.05)
    timeout = _number(merged, "timeout", 3, ctx, 0.1)
    failure_threshold = _number(merged, "failure_threshold", 3, ctx, 1, integer=True)
    success_threshold = _number(merged, "success_threshold", 2, ctx, 1, integer=True)

    endpoints_raw = raw.get("endpoints")
    if not isinstance(endpoints_raw, list) or not endpoints_raw:
        raise ConfigError(f"{ctx}.endpoints: must be a non-empty list")
    endpoints: List[EndpointConfig] = []
    seen = set()
    for i, e in enumerate(endpoints_raw):
        ectx = f"{ctx}.endpoints[{i}]"
        e = _obj(e, ectx)
        ename = _name(e.get("name"), ectx)
        if ename in seen:
            raise ConfigError(f"{ctx}: duplicate endpoint name '{ename}'")
        seen.add(ename)
        address = e.get("address")
        if address is not None and not isinstance(address, str):
            raise ConfigError(f"{ectx}.address: must be a string")
        endpoints.append(
            EndpointConfig(
                name=ename,
                priority=_number(e, "priority", i + 1, ectx, 0, integer=True),
                check=parse_check(e.get("check"), f"{ectx}.check", timeout),
                address=address,
                recovery=parse_recovery(e["recovery"], f"{ectx}.recovery") if "recovery" in e else None,
            )
        )

    fb = _obj(raw.get("failback", {}), f"{ctx}.failback")
    mode = fb.get("mode", "auto")
    if mode not in ("auto", "manual"):
        raise ConfigError(f"{ctx}.failback.mode: must be 'auto' or 'manual'")
    flap = _obj(raw.get("flap_protection", {}), f"{ctx}.flap_protection")

    return ServiceConfig(
        name=name,
        endpoints=endpoints,
        interval=interval,
        failure_threshold=failure_threshold,
        success_threshold=success_threshold,
        failover_delay=_number(raw, "failover_delay", 0, ctx, 0),
        failback=FailbackConfig(mode, _number(fb, "stable_seconds", 60, f"{ctx}.failback", 0)),
        flap=FlapConfig(
            _number(flap, "max_switches", 5, f"{ctx}.flap_protection", 1, integer=True),
            _number(flap, "window_seconds", 600, f"{ctx}.flap_protection", 1),
        ),
        recovery=parse_recovery(raw["recovery"], f"{ctx}.recovery") if "recovery" in raw else None,
        on_failover=parse_actions(raw.get("on_failover", []), f"{ctx}.on_failover", allow_empty=True),
        hook_max_attempts=_number(raw, "hook_max_attempts", 5, ctx, 1, integer=True),
        hook_retry_seconds=_number(raw, "hook_retry_seconds", 10, ctx, 0),
    )


def _path(base: Path, value: Any, ctx: str) -> Optional[Path]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{ctx}: must be a non-empty string")
    p = Path(value).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def parse_config(raw: Any, base_dir: Path) -> Config:
    raw = _obj(raw, "config")
    services_raw = raw.get("services")
    if not isinstance(services_raw, list) or not services_raw:
        raise ConfigError("services: must be a non-empty list")
    defaults = _obj(raw.get("defaults", {}), "defaults")

    services: List[ServiceConfig] = []
    names = set()
    for i, s in enumerate(services_raw):
        svc = parse_service(s, defaults, f"services[{i}]")
        if svc.name in names:
            raise ConfigError(f"duplicate service name '{svc.name}'")
        names.add(svc.name)
        services.append(svc)

    api_raw = _obj(raw.get("api", {}), "api")
    token_env = api_raw.get("token_env")
    token = os.environ.get(token_env) if isinstance(token_env, str) and token_env else None
    api = ApiConfig(
        enabled=bool(api_raw.get("enabled", False)),
        host=str(api_raw.get("host", "127.0.0.1")),
        port=_number(api_raw, "port", 8480, "api", 0, integer=True),
        token=token or None,
    )
    return Config(
        services=services,
        state_file=_path(base_dir, raw.get("state_file"), "state_file"),
        event_log=_path(base_dir, raw.get("event_log"), "event_log"),
        api=api,
    )


def load_config(path) -> Config:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}")
    return parse_config(raw, base_dir=path.resolve().parent)
