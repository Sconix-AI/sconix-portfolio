"""The mutating half: one typed action, behind a policy gate.

`RESTART` is a `sconixcore.ActionSpec` — the action's contract (risk, approval,
verification, side effects) declared once. `restart_app` executes it;
`make_guard` returns the gate `sconixapp.agent.guarded_tool` calls first, which
now records a `sconixcore.Decision` (outcome + accountable principal) per check.

Pilot keeps its own policy (allow-set + cooldown) local — that's still one
policy for one action; the reusable contract is the types, not the rule.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sconixcore import (
    ActionSpec,
    ApprovalMode,
    Decision,
    DecisionOutcome,
    Principal,
    Risk,
    Verification,
)
from sqlmodel import select

if TYPE_CHECKING:
    from pilot.executor import ActionExecutor

from pilot.audit import Action, record
from pilot.principal import PILOT, label

COOLDOWN_S = 600  # don't restart the same target twice within 10 min

# run.py points this at the active targets file; a target may carry its own
# `restart:` command (the drill target heals itself that way). Absent -> the
# real fleet default, `sx restart <app>`.
TARGETS_PATH: Path | None = None

RESTART = ActionSpec(
    name="restart",  # matches the `restart` command in a project's sconix.yaml
    argv=("sx", "restart", "{project}"),  # representative; LocalExecutor uses _restart_cmd()
    risk=Risk.EXTERNAL_WRITE,
    idempotent=True,
    approval=ApprovalMode.POLICY,
    verification=Verification(
        checks=("healthz", "readyz"),
        within_seconds=30,
        attempts=3,
        interval_seconds=2,
    ),
    side_effects=("in-place container restart on a remote host, no rebuild",),
    preconditions=(
        "target assessed warn/down this run",
        "cooldown clear",
        "principal in scope",
    ),
    rollback=None,
)


def _restart_cmd(app: str) -> list[str]:
    if TARGETS_PATH and TARGETS_PATH.exists():
        for t in yaml.safe_load(TARGETS_PATH.read_text()).get("targets", []):
            if t.get("name") == app and t.get("restart"):
                return shlex.split(t["restart"])
    return ["sx", "restart", app]


async def restart_app(app: str) -> str:
    """Restart one deployed app in place (no rebuild). Mutating — see RESTART."""
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
                    Action.tool == RESTART.name,
                    Action.decision == DecisionOutcome.ALLOW.value,
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
    principal: Principal = PILOT,
    incidents: dict[str, int] | None = None,
    cooldown_s: int = COOLDOWN_S,
    executor: ActionExecutor | None = None,
    decisions: dict[str, Decision] | None = None,
) -> Guard:
    """Gate for the agent's mutating tool. The action must be **declared** in the
    target's manifest (via the executor); its `approval` mode drives the check:

    - `never`  → allowed (still audited).
    - `policy` → Pilot's local policy: target in ``allow`` this run, principal in
      scope, no action within ``cooldown_s``.
    - `always` → denied — needs a human; Pilot can't self-approve.

    Records a `sconixcore.Decision` per check. ``run_result[target]`` gets the
    outcome string; ``decisions[target]`` (if given) gets the `Decision` object,
    which `execute()` needs to authorize the action."""

    if executor is None:
        from pilot.executor import DEFAULT as executor

    incidents = incidents or {}
    decisions = decisions if decisions is not None else {}
    who = label(principal)

    async def _deny_reason(tool: str, target: str) -> str:
        """Empty string == allowed."""
        spec = executor.lookup(target, tool)
        if spec is None:
            return f"undeclared action {tool!r} — not in {target}'s manifest"
        if spec.approval is ApprovalMode.NEVER:
            return ""
        if spec.approval is ApprovalMode.ALWAYS:
            return f"{tool!r} requires human approval ({spec.risk.value})"
        # ApprovalMode.POLICY — Pilot's local rule
        if target not in allow:
            return "not an approved target this run (healthy, or --fix not set)"
        if principal.scope and target not in principal.scope:
            return f"{who} scope does not include {target}"
        if await _restarted_within(session, target, cooldown_s):
            return f"already restarted within {cooldown_s}s — a loop won't help, escalate"
        return ""

    async def guard(tool: str, kwargs: dict[str, Any]) -> bool | str:
        target = str(kwargs.get("app", "?"))
        reason = await _deny_reason(tool, target)
        outcome = DecisionOutcome.ALLOW if reason == "" else DecisionOutcome.DENY
        decision = Decision(
            outcome=outcome,
            decided_by=principal,
            decided_at=datetime.now(UTC).isoformat(),
            reason=reason or "warn/down + --fix + cooldown clear",
        )
        await record(
            session,
            target=target,
            incident_id=incidents.get(target),
            principal=who,
            tool=tool,
            args=json.dumps(kwargs, default=str),
            decision=outcome.value,
            reason=decision.reason,
        )
        run_result[target] = outcome.value
        decisions[target] = decision
        return True if outcome is DecisionOutcome.ALLOW else decision.reason

    return guard
