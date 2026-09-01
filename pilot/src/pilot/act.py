"""The policy gate around the agent's one mutating action.

Execution (argv, no-shell, authz) is `sconixcore.ManifestExecutor`'s job, driven
by each target's `sconix.yaml`. This module only decides *whether* an action may
run: it looks the action up via the executor, honours `spec.approval`, and adds
Pilot's local rule (allow-set + cooldown + principal scope), recording a
`sconixcore.Decision` per check.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sconixcore import ApprovalMode, Decision, DecisionOutcome, Principal
from sqlmodel import select

if TYPE_CHECKING:
    from pilot.executor import ActionExecutor

from pilot.audit import Action, record
from pilot.principal import PILOT, label

COOLDOWN_S = 600  # don't restart the same target twice within 10 min
RESTART_ACTION = "restart"  # the manifest command name

# run.py sets this to the active targets file; pilot.manifest.resolve_target reads it.
TARGETS_PATH: Path | None = None


Guard = Callable[[str, dict[str, Any]], Awaitable[bool | str]]


async def _restarted_within(session: Any, target: str, seconds: int) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    row = (
        (
            await session.execute(
                select(Action)
                .where(
                    Action.target == target,
                    Action.tool == RESTART_ACTION,
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
    executor: ActionExecutor,
    cooldown_s: int = COOLDOWN_S,
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
