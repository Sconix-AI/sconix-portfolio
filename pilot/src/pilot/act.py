"""The mutating half: one real action, behind a policy gate.

``restart_app`` shells ``sx restart <app>`` — a genuine side effect on the fleet.
``make_guard`` returns the gate ``sconixapp.agent.guarded_tool`` calls before it
runs: it consults a plain allow-set and writes every decision to the audit log.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pilot.audit import record

SX = "sx"  # on PATH via ~/systems/os/bin


async def restart_app(app: str) -> str:
    """Restart the running containers for one deployed app (no rebuild). Mutating."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        SX, "restart", app,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    ms = int((time.monotonic() - started) * 1000)
    tail = out.decode()[-500:].strip()
    if proc.returncode != 0:
        return f"restart failed (exit {proc.returncode}) after {ms}ms:\n{tail}"
    return f"restarted {app} in {ms}ms:\n{tail}"


Guard = Callable[[str, dict[str, Any]], Awaitable[bool | str]]


def make_guard(session: Any, *, allow: set[str], run_result: dict[str, str]) -> Guard:
    """Gate: allow ``restart_app`` only for a target in ``allow`` (the apps this
    run assessed as warn/down, and only when --fix was given). Every check is
    audited; the outcome of an allowed call is recorded by ``run_result``."""

    async def guard(tool: str, kwargs: dict[str, Any]) -> bool | str:
        target = str(kwargs.get("app", "?"))
        args = json.dumps(kwargs, default=str)
        if tool != "restart_app":
            reason = f"no policy for tool {tool!r}"
            await record(session, target=target, tool=tool, args=args,
                         decision="denied", reason=reason)
            return reason
        if target not in allow:
            reason = "not an approved target this run (healthy, or --fix not set)"
            await record(session, target=target, tool=tool, args=args,
                         decision="denied", reason=reason)
            return reason
        await record(session, target=target, tool=tool, args=args,
                     decision="allowed", reason="warn/down + --fix")
        run_result[target] = "allowed"
        return True

    return guard
