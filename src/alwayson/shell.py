"""Run shell commands with a hard timeout (kills the whole process group)."""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class ShellResult:
    returncode: int
    output: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def run_shell(
    command: str, timeout: float, env: Optional[Mapping[str, str]] = None
) -> ShellResult:
    """Run `command` through the shell; stdout and stderr are merged and truncated."""
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(
        command,
        shell=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, _ = proc.communicate()
        timed_out = True
    text = out.decode("utf-8", errors="replace").strip()
    return ShellResult(proc.returncode, text[-500:], timed_out)
