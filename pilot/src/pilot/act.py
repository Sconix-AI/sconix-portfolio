"""The mutating half: one real action, behind a policy gate.

``restart_app`` shells ``sx restart <app>`` — a genuine side effect on the fleet.
``make_guard`` returns the gate ``sconixapp.agent.guarded_tool`` calls before it
runs: it consults a plain allow-set and writes every decision to the audit log.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import select

from pilot.audit import Action, record

COOLDOWN_S = 600  # don't restart the same target twice within 10 min

# run.py points this at the active targets file; a target may carry its own
# `restart:` command (the drill target heals itself that way). Absent -> the
# real fleet default, `sx restart <app>`.
TARGETS_PATH: Path | None = None


def _restart_cmd(app: str) -> list[str]:
    if TARGETS_PATH and TARGETS_PATH.exists():
        for t in yaml.safe_load(TARGETS_PATH.read_text()).get("targets", []):
            if t.get("name") == app and t.get("restart"):
                return shlex.split(t["restart"])
    return ["sx", "restart", app]


async def restart_app(app: str) -> str:
    """Restart one deployed app in place (no rebuild). Mutating."""
    cmd = _restart_cmd(app)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    ms = int((time.monotonic() - started) * 1000)
    tail = out.decode()[-500:].strip()
    if proc.returncode != 0:
        return f"restart failed (exit {proc.returncode}) after {ms}ms:\n{tail}"
    return f"restarted {app} in {ms}ms via `{' '.join(cmd)}`:\n{tail}"


Guard = Callable[[str, dict[str, Any]], Awaitable[bool | str]]


async def _restarted_within(session: Any, target: str, seconds: int) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    row = (
        (
            await session.execute(
                select(Action)
                .where(
                    Action.target == target,
                    Action.tool == "restart_app",
                    Action.decision == "allowed",
                    Action.created_at >= cutoff,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row is not None


def make_guard(
    session: Any,
    *,
    allow: set[str],
    run_result: dict[str, str],
    cooldown_s: int = COOLDOWN_S,
) -> Guard:
    """Gate: allow ``restart_app`` only for a target in ``allow`` (the apps this
    run assessed as warn/down, and only when --fix was given), and not if the
    same target was already restarted within ``cooldown_s``. Every check is
    audited; the outcome of an allowed call is recorded in ``run_result``."""

    async def guard(tool: str, kwargs: dict[str, Any]) -> bool | str:
        target = str(kwargs.get("app", "?"))
        args = json.dumps(kwargs, default=str)

        if tool != "restart_app":
            reason = f"no policy for tool {tool!r}"
        elif target not in allow:
            reason = "not an approved target this run (healthy, or --fix not set)"
        elif await _restarted_within(session, target, cooldown_s):
            reason = f"already restarted within {cooldown_s}s — a loop won't help, escalate"
        else:
            await record(
                session,
                target=target,
                tool=tool,
                args=args,
                decision="allowed",
                reason="warn/down + --fix + cooldown clear",
            )
            run_result[target] = "allowed"
            return True

        await record(session, target=target, tool=tool, args=args, decision="denied", reason=reason)
        return reason

    return guard
