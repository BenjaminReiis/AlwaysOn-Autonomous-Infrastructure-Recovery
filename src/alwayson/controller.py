"""Per-service control loop: probing, recovery, failover and failback."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from .actions import ActionRunner
from .config import EndpointConfig, ServiceConfig
from .events import Event, EventLog
from .probes import Probe, ProbeResult, build_probe
from .state import EndpointState, EndpointStatus, PendingHooks, ServiceState, StateStore

log = logging.getLogger("alwayson")

HEALTHY = EndpointStatus.HEALTHY
UNHEALTHY = EndpointStatus.UNHEALTHY
UNKNOWN = EndpointStatus.UNKNOWN

ProbeFactory = Callable[[EndpointConfig], Probe]


class ControllerError(Exception):
    """A manual operation was rejected."""


def default_probe_factory(endpoint: EndpointConfig) -> Probe:
    return build_probe(endpoint.check)


class ServiceController:
    """Owns the state machine of one service.

    Each `tick()` probes every endpoint, applies the thresholds, then (unless frozen)
    fails over / fails back, runs recovery actions and retries pending hooks.
    `clock` measures durations (monotonic); `wall` timestamps events. Both are injectable.
    """

    def __init__(
        self,
        cfg: ServiceConfig,
        events: EventLog,
        store: Optional[StateStore] = None,
        runner: Optional[ActionRunner] = None,
        probe_factory: Optional[ProbeFactory] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.events = events
        self.store = store
        self.runner = runner or ActionRunner()
        self.clock = clock
        self.wall = wall
        self.lock = threading.RLock()
        factory = probe_factory or default_probe_factory
        self._by_name = {ep.name: ep for ep in cfg.endpoints}
        self._ordered = sorted(cfg.endpoints, key=lambda e: e.priority)  # stable: ties keep config order
        self._probes = {ep.name: factory(ep) for ep in cfg.endpoints}
        self._pool = ThreadPoolExecutor(
            max_workers=len(cfg.endpoints), thread_name_prefix=f"probe-{cfg.name}"
        )
        self.state = ServiceState(cfg.name, {ep.name: EndpointState(ep.name) for ep in cfg.endpoints})
        self._restore()

    # ------------------------------------------------------------------ public
    def tick(self) -> None:
        results = self._probe_all()
        with self.lock:
            now = self.clock()
            for name, result in results.items():
                self._record(name, result, now)
            if not self.state.frozen:
                self._manage_active(now)
                for ep in self.cfg.endpoints:
                    self._maybe_recover(ep, now)
            self._retry_hooks(now)
            self._update_status()
            self._publish()

    def manual_failover(self, to: str, force: bool = False, actor: str = "operator") -> Dict[str, Any]:
        with self.lock:
            if to not in self._by_name:
                raise ControllerError(f"unknown endpoint '{to}'")
            if to == self.state.active:
                raise ControllerError(f"'{to}' is already active")
            if not force and self.state.endpoints[to].status != HEALTHY:
                raise ControllerError(f"'{to}' is not healthy (use force to override)")
            self._switch(to, "manual_failover", f"requested by {actor}", self.clock(), automatic=False)
            self._update_status()
            self._publish()
            return self.snapshot()

    def set_frozen(self, frozen: bool, actor: str = "operator") -> Dict[str, Any]:
        with self.lock:
            if frozen != self.state.frozen:
                self.state.frozen = frozen
                if not frozen:
                    self.state.switch_times.clear()
                self._emit(
                    "frozen" if frozen else "unfrozen",
                    f"automation {'paused' if frozen else 'resumed'} by {actor}",
                )
                self._publish()
            return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            st = self.state
            return {
                "name": self.cfg.name,
                "status": st.status,
                "active": st.active,
                "frozen": st.frozen,
                "pinned": st.pinned,
                "last_change": st.last_change,
                "endpoints": {
                    ep.name: {
                        "priority": ep.priority,
                        "address": ep.address,
                        "status": st.endpoints[ep.name].status.value,
                        "latency_ms": st.endpoints[ep.name].last_latency_ms,
                        "detail": st.endpoints[ep.name].last_detail,
                        "consecutive_failures": st.endpoints[ep.name].consecutive_failures,
                        "recovery_attempts": st.endpoints[ep.name].recovery_attempts,
                        "last_check": st.endpoints[ep.name].last_check_at,
                    }
                    for ep in self.cfg.endpoints
                },
            }

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    # --------------------------------------------------------------- internals
    def _emit(self, type_: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.events.emit(Event(self.wall(), self.cfg.name, type_, message, data or {}))

    def _publish(self) -> None:
        if self.store:
            self.store.update(self.cfg.name, self.snapshot())

    def _restore(self) -> None:
        if not self.store:
            return
        saved = self.store.restore(self.cfg.name)
        active = saved.get("active")
        if active in self._by_name:
            self.state.active = active
            self.state.pinned = bool(saved.get("pinned", False))
            self._emit("restored", f"restored active endpoint '{active}' from the state file", {"active": active})
        if saved.get("frozen") is True:
            self.state.frozen = True
            self._emit("frozen", "automation is paused (restored from the state file)")

    def _probe_all(self) -> Dict[str, ProbeResult]:
        futures = {name: self._pool.submit(probe.check) for name, probe in self._probes.items()}
        results: Dict[str, ProbeResult] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = ProbeResult(False, 0.0, f"probe error: {exc}")
        return results

    def _record(self, name: str, result: ProbeResult, now: float) -> None:
        es = self.state.endpoints[name]
        es.last_check_at = self.wall()
        es.last_latency_ms = result.latency_ms
        es.last_detail = result.detail
        if result.ok:
            es.consecutive_successes += 1
            es.consecutive_failures = 0
            if es.status != HEALTHY and es.consecutive_successes >= self.cfg.success_threshold:
                previous, attempts = es.status, es.recovery_attempts
                es.status, es.healthy_since, es.unhealthy_since = HEALTHY, now, None
                es.recovery_attempts, es.next_recovery_at = 0, 0.0
                note = f" after {attempts} recovery attempt(s)" if attempts else ""
                self._emit("endpoint_up", f"'{name}' is healthy{note}",
                           {"endpoint": name, "previous": previous.value})
        else:
            es.consecutive_failures += 1
            es.consecutive_successes = 0
            if es.status != UNHEALTHY and es.consecutive_failures >= self.cfg.failure_threshold:
                es.status, es.unhealthy_since, es.healthy_since = UNHEALTHY, now, None
                self._emit("endpoint_down", f"'{name}' is unhealthy: {result.detail}",
                           {"endpoint": name, "detail": result.detail})

    def _best_healthy(self, exclude: Optional[str] = None) -> Optional[str]:
        for ep in self._ordered:
            if ep.name != exclude and self.state.endpoints[ep.name].status == HEALTHY:
                return ep.name
        return None

    def _failback_candidate(self, now: float) -> Optional[str]:
        active_priority = self._by_name[self.state.active].priority  # type: ignore[index]
        for ep in self._ordered:
            if ep.priority >= active_priority:
                break
            es = self.state.endpoints[ep.name]
            if (
                es.status == HEALTHY
                and es.healthy_since is not None
                and now - es.healthy_since >= self.cfg.failback.stable_seconds
            ):
                return ep.name
        return None

    def _flap_guard(self, now: float) -> bool:
        """True (and freezes automation) when too many switches happened recently."""
        times = self.state.switch_times
        while times and now - times[0] > self.cfg.flap.window_seconds:
            times.popleft()
        if len(times) >= self.cfg.flap.max_switches:
            self.state.frozen = True
            self._emit(
                "frozen",
                f"automation frozen: {len(times)} switches within {self.cfg.flap.window_seconds:g}s (flapping)",
                {"switches": len(times)},
            )
            return True
        return False

    def _manage_active(self, now: float) -> None:
        st = self.state
        if st.active is None:
            candidate = self._best_healthy()
            if candidate:
                self._switch(candidate, "election", "initial election", now, automatic=True)
            return
        active = st.endpoints[st.active]
        if active.status == UNHEALTHY:
            down_for = now - (active.unhealthy_since if active.unhealthy_since is not None else now)
            if down_for < self.cfg.failover_delay:
                return
            candidate = self._best_healthy(exclude=st.active)
            if candidate and not self._flap_guard(now):
                self._switch(candidate, "failover", f"'{st.active}' is unhealthy: {active.last_detail}",
                             now, automatic=True)
        elif active.status == HEALTHY and self.cfg.failback.mode == "auto" and not st.pinned:
            candidate = self._failback_candidate(now)
            if candidate and not self._flap_guard(now):
                self._switch(candidate, "failback", f"'{candidate}' has been healthy for "
                             f"{self.cfg.failback.stable_seconds:g}s", now, automatic=True)

    def _switch(self, to: str, kind: str, reason: str, now: float, automatic: bool) -> None:
        st = self.state
        previous = st.active
        st.active = to
        st.pinned = not automatic
        st.last_change = self.wall()
        if automatic and kind in ("failover", "failback"):
            st.switch_times.append(now)
        self._emit(kind, f"{previous or '-'} -> {to}: {reason}",
                   {"from": previous, "to": to, "reason": reason})
        self._run_hooks(kind, previous, to, reason, attempt=1)

    # -------------------------------------------------------------------- hooks
    def _run_hooks(self, kind: str, previous: Optional[str], to: str, reason: str, attempt: int) -> None:
        if not self.cfg.on_failover:
            return
        ep = self._by_name[to]
        context = {
            "service": self.cfg.name, "event": kind, "from": previous or "", "to": to,
            "endpoint": to, "address": ep.address or "", "reason": reason,
        }
        ok, detail = self.runner.run_all(self.cfg.on_failover, context)
        if ok:
            self.state.pending_hooks = None
            self._emit("hooks_ok", f"switch hooks succeeded for '{to}'", {"to": to, "attempt": attempt})
        else:
            self.state.pending_hooks = PendingHooks(
                kind, previous, to, reason, attempt, self.clock() + self.cfg.hook_retry_seconds
            )
            self._emit("hook_failed", f"switch hooks failed (attempt {attempt}): {detail}",
                       {"to": to, "attempt": attempt})

    def _retry_hooks(self, now: float) -> None:
        pending = self.state.pending_hooks
        if pending is None or now < pending.next_at:
            return
        if pending.target != self.state.active:
            self.state.pending_hooks = None  # superseded by a newer switch
            return
        if pending.attempts >= self.cfg.hook_max_attempts:
            self.state.pending_hooks = None
            self._emit("hook_gave_up",
                       f"switch hooks for '{pending.target}' still failing after {pending.attempts} attempts; "
                       "manual action required", {"to": pending.target})
            return
        self._run_hooks(pending.kind, pending.previous, pending.target, pending.reason,
                        attempt=pending.attempts + 1)

    # ----------------------------------------------------------------- recovery
    def _maybe_recover(self, ep: EndpointConfig, now: float) -> None:
        es = self.state.endpoints[ep.name]
        if es.status != UNHEALTHY:
            return
        policy = ep.recovery or self.cfg.recovery
        if policy is None:
            return
        if es.recovery_attempts >= policy.max_attempts:
            if es.last_recovery_at is not None and now - es.last_recovery_at >= policy.cooldown_seconds:
                es.recovery_attempts = 0  # cooldown elapsed: try again
            else:
                return
        if now < es.next_recovery_at:
            return
        attempt = es.recovery_attempts + 1
        context = {
            "service": self.cfg.name, "event": "recovery", "endpoint": ep.name,
            "address": ep.address or "", "attempt": str(attempt), "reason": es.last_detail,
        }
        ok, detail = self.runner.run_all(policy.actions, context)
        done = self.clock()
        es.recovery_attempts = attempt
        es.last_recovery_at = done
        es.next_recovery_at = done + policy.backoff_seconds[min(attempt - 1, len(policy.backoff_seconds) - 1)]
        self._emit(
            "recovery_attempt",
            f"recovery attempt {attempt}/{policy.max_attempts} for '{ep.name}' "
            f"{'ran' if ok else 'FAILED'}: {detail}",
            {"endpoint": ep.name, "attempt": attempt, "ok": ok},
        )

    # ------------------------------------------------------------------- status
    def _update_status(self) -> None:
        st = self.state
        if st.active is None:
            unknown = any(e.status == UNKNOWN for e in st.endpoints.values())
            new = "starting" if unknown else "outage"
        else:
            active = st.endpoints[st.active].status
            if active == UNKNOWN:
                new = "starting"
            elif active == UNHEALTHY:
                new = "outage"
            else:
                new = "healthy" if st.active == self._ordered[0].name else "degraded"
        if new != st.status:
            old, st.status = st.status, new
            self._emit("service_status", f"{old} -> {new}", {"from": old, "to": new})
