"""Event log: in-memory ring buffer plus optional JSON Lines file."""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger("alwayson")

WARNING_TYPES = {
    "endpoint_down", "failover", "failback", "manual_failover", "frozen",
    "hook_failed", "hook_gave_up", "error",
}


@dataclass(frozen=True)
class Event:
    ts: float
    service: str
    type: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "time": datetime.fromtimestamp(self.ts, timezone.utc).isoformat(timespec="seconds"),
            "service": self.service,
            "type": self.type,
            "message": self.message,
            "data": self.data,
        }


class EventLog:
    def __init__(self, path: Optional[Path] = None, maxlen: int = 1000):
        self.path = path
        self._events: Deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                except OSError as exc:
                    log.warning("could not write event log: %s", exc)
        level = logging.WARNING if event.type in WARNING_TYPES else logging.INFO
        log.log(level, "[%s] %s: %s", event.service, event.type, event.message)

    def recent(self, limit: int = 100, service: Optional[str] = None) -> List[Event]:
        with self._lock:
            items = [e for e in self._events if service is None or e.service == service]
        return items[-limit:] if limit > 0 else []
