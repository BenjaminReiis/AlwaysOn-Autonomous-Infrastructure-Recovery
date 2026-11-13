"""Runtime state and its persistence."""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, Optional

log = logging.getLogger("alwayson")


class EndpointStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass
class EndpointState:
    name: str
    status: EndpointStatus = EndpointStatus.UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_check_at: Optional[float] = None  # wall clock
    last_latency_ms: Optional[float] = None
    last_detail: str = ""
    healthy_since: Optional[float] = None  # controller clock
    unhealthy_since: Optional[float] = None  # controller clock
    recovery_attempts: int = 0
    last_recovery_at: Optional[float] = None
    next_recovery_at: float = 0.0


@dataclass
class PendingHooks:
    kind: str
    previous: Optional[str]
    target: str
    reason: str
    attempts: int
    next_at: float


@dataclass
class ServiceState:
    name: str
    endpoints: Dict[str, EndpointState]
    active: Optional[str] = None
    status: str = "starting"
    frozen: bool = False
    pinned: bool = False  # active endpoint chosen manually: no automatic failback
    last_change: Optional[float] = None  # wall clock time of the last switch
    switch_times: Deque[float] = field(default_factory=deque)
    pending_hooks: Optional[PendingHooks] = None


class StateStore:
    """Writes a JSON snapshot of all services; restores `active`, `frozen` and `pinned`."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._lock = threading.Lock()
        self._services: Dict[str, Dict[str, Any]] = {}
        self._persisted: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            services = data.get("services", {})
            return services if isinstance(services, dict) else {}
        except (OSError, ValueError):
            return {}

    def restore(self, service: str) -> Dict[str, Any]:
        saved = self._persisted.get(service, {})
        return saved if isinstance(saved, dict) else {}

    def update(self, service: str, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._services[service] = snapshot
            self._flush()

    def _flush(self) -> None:
        if not self.path:
            return
        payload = {
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "services": {**self._persisted, **self._services},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not write state file: %s", exc)
