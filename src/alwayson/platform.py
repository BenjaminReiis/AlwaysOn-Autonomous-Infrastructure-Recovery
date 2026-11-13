"""Platform: runs one control loop per service plus the optional admin API."""
from __future__ import annotations

import logging
import signal
import threading
from typing import Any, Dict, List, Optional

from .actions import ActionRunner
from .api import ApiServer
from .config import Config
from .controller import ProbeFactory, ServiceController
from .events import Event, EventLog
from .state import StateStore

log = logging.getLogger("alwayson")


class Platform:
    def __init__(
        self,
        config: Config,
        runner: Optional[ActionRunner] = None,
        probe_factory: Optional[ProbeFactory] = None,
    ):
        self.config = config
        self.events = EventLog(config.event_log)
        self.store = StateStore(config.state_file)
        self.controllers: Dict[str, ServiceController] = {
            s.name: ServiceController(s, self.events, self.store, runner, probe_factory)
            for s in config.services
        }
        self.stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self.api: Optional[ApiServer] = ApiServer(self) if config.api.enabled else None

    def snapshot(self) -> Dict[str, Any]:
        return {name: ctl.snapshot() for name, ctl in self.controllers.items()}

    def start_api(self) -> None:
        if self.api:
            self.api.start()

    def start(self) -> None:
        self.start_api()
        for ctl in self.controllers.values():
            thread = threading.Thread(
                target=self._loop, args=(ctl,), name=f"service-{ctl.cfg.name}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("AlwaysOn started: %d service(s)", len(self.controllers))

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        if self.api:
            self.api.stop()
        for ctl in self.controllers.values():
            ctl.close()
        log.info("AlwaysOn stopped")

    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop_event.set())
        self.start()
        try:
            while not self.stop_event.wait(0.5):
                pass
        finally:
            self.stop()

    def _loop(self, ctl: ServiceController) -> None:
        while not self.stop_event.is_set():
            try:
                ctl.tick()
            except Exception as exc:  # the loop must never die
                log.exception("tick failed for %s", ctl.cfg.name)
                self.events.emit(Event(ctl.wall(), ctl.cfg.name, "error", f"tick failed: {exc}"))
            self.stop_event.wait(ctl.cfg.interval)
