"""Actions: recovery commands and failover hooks (shell commands and webhooks)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .config import ActionConfig
from .shell import run_shell


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: str


class ActionRunner:
    """Runs actions. Commands receive the context as ALWAYSON_<KEY> environment variables;
    webhooks receive it as a JSON body."""

    def run(self, action: ActionConfig, context: Dict[str, str]) -> ActionResult:
        try:
            if action.type == "command":
                return self._command(action, context)
            if action.type == "webhook":
                return self._webhook(action, context)
        except Exception as exc:
            return ActionResult(False, f"{type(exc).__name__}: {exc}")
        return ActionResult(False, f"unknown action type: {action.type}")

    def run_all(self, actions: List[ActionConfig], context: Dict[str, str]) -> Tuple[bool, str]:
        """Run actions in order; stop at the first failure."""
        details = []
        for action in actions:
            result = self.run(action, context)
            label = action.command if action.type == "command" else action.url
            details.append(f"{action.type} '{label}': {'ok' if result.ok else 'FAILED'} ({result.detail})")
            if not result.ok:
                return False, "; ".join(details)
        return True, "; ".join(details)

    @staticmethod
    def _command(action: ActionConfig, context: Dict[str, str]) -> ActionResult:
        env = dict(os.environ)
        env.update({f"ALWAYSON_{k.upper()}": "" if v is None else str(v) for k, v in context.items()})
        res = run_shell(action.command or "", action.timeout, env)
        if res.timed_out:
            return ActionResult(False, f"timed out after {action.timeout}s")
        return ActionResult(res.ok, f"exit {res.returncode}" + (f": {res.output[:200]}" if res.output else ""))

    @staticmethod
    def _webhook(action: ActionConfig, context: Dict[str, str]) -> ActionResult:
        body = json.dumps({**context, "timestamp": int(time.time())}).encode("utf-8")
        request = urllib.request.Request(
            action.url or "",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "alwayson"},
        )
        try:
            with urllib.request.urlopen(request, timeout=action.timeout) as resp:
                return ActionResult(200 <= resp.status < 300, f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            return ActionResult(False, f"HTTP {exc.code}")
