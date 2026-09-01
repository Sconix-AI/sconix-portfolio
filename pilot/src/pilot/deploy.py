"""Deploy / rollback through the human-approval boundary.

Pilot may **propose** a plan (`deploy_plan` / `rollback_plan` — `approval: never`)
and, once a human has run `sx approve`, **execute** the approved action
(`deploy` / `rollback` — `approval: always`). Pilot never approves anything.

Plans, approvals, and executions live in Sconix's plan store
(`$SCONIX_STATE_DIR/deploy/…`, read via `sconixcore.deploy`) — Pilot only
records the plan id and the outcome on its own incident/audit trail.

Command keys are the agent-safe snake_case names; the real app manifests adopt
them in a Codex commit — until then this module is exercised only against a
fake executor + a temp state dir (see `tests/test_deploy.py`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from sconixcore import Decision, DecisionOutcome, Principal, PrincipalKind

from pilot.audit import record
from pilot.memory import ACTED, APPROVED, AWAITING_APPROVAL, ESCALATED, transition

DEPLOY_PLAN = "deploy_plan"
ROLLBACK_PLAN = "rollback_plan"
DEPLOY = "deploy"
ROLLBACK = "rollback"

_PLAN_ID = re.compile(r"\b([0-9a-f]{20})\b")  # sconixcore plan ids: 20 hex chars

ApprovalStatus = Literal["pending", "approved", "consumed"]


def _plan_id_from(output: str) -> str:
    m = _PLAN_ID.search(output or "")
    if not m:
        raise ValueError(f"no plan id in plan output: {output[:200]!r}")
    return m.group(1)


def _principal_from(record_value: dict[str, Any]) -> Principal:
    p = record_value.get("principal", {})
    return Principal(
        kind=PrincipalKind(p.get("kind", "human")),
        id=p.get("id", "unknown"),
        role=p.get("role"),
        intent=p.get("intent"),
    )


def approval_status(plan_id: str) -> ApprovalStatus:
    """Read Sconix's plan store: has a human approved this plan, and is it still
    good for one execution?"""
    from sconixcore.deploy import DeployRecordError, load_record

    try:
        load_record("approvals", plan_id)
    except DeployRecordError:
        return "pending"
    try:
        load_record("executions", plan_id)
    except DeployRecordError:
        return "approved"
    return "consumed"


async def propose(
    session: Any,
    executor: Any,
    *,
    target: str,
    kind: Literal["deploy", "rollback"],
    principal: Principal,
    incident: Any,
    arguments: dict[str, str] | None = None,
) -> str:
    """Run ``<kind>_plan``, record the plan id, park the incident on
    ``awaiting_approval``. Returns the plan id."""
    plan_action = DEPLOY_PLAN if kind == "deploy" else ROLLBACK_PLAN
    res = await executor.execute(target, plan_action, principal=principal, arguments=arguments)
    if not res.ok:
        await record(
            session,
            target=target,
            incident_id=incident.id,
            principal=str(principal),
            tool=plan_action,
            decision="failed",
            result=res.output[:500],
        )
        await transition(session, incident, ESCALATED, resolution=f"{plan_action} failed")
        raise RuntimeError(f"{plan_action} failed: {res.output[:200]}")

    plan_id = _plan_id_from(res.output)
    await record(
        session,
        target=target,
        incident_id=incident.id,
        principal=str(principal),
        tool=plan_action,
        decision="planned",
        plan_id=plan_id,
        result=res.output[:500],
    )
    await transition(
        session,
        incident,
        AWAITING_APPROVAL,
        plan_id=plan_id,
        resolution=f"awaiting human approval — sx approve {plan_id}",
    )
    return plan_id


async def execute_approved(
    session: Any,
    executor: Any,
    *,
    target: str,
    kind: Literal["deploy", "rollback"],
    plan_id: str,
    principal: Principal,
    incident: Any,
    arguments: dict[str, str] | None = None,
) -> bool:
    """If a human has approved ``plan_id``, run the ``<kind>`` action with it.
    Records stale / consumed / denied / failed / verified. Returns True on a
    verified success."""
    status = approval_status(plan_id)
    if status != "approved":
        await record(
            session,
            target=target,
            incident_id=incident.id,
            principal=str(principal),
            tool=kind,
            decision="denied",
            plan_id=plan_id,
            reason=f"plan {plan_id} is {status}",
        )
        if status == "consumed":
            await transition(session, incident, ESCALATED, resolution="plan already consumed")
        return False

    from sconixcore.deploy import load_record

    approver = _principal_from(load_record("approvals", plan_id))
    decision = Decision(
        outcome=DecisionOutcome.ALLOW_ONCE,
        decided_by=approver,
        decided_at=datetime.now(UTC).isoformat(),
        reason=f"human approval {plan_id}",
    )
    await transition(session, incident, APPROVED)

    args = {"plan_id": plan_id, **(arguments or {})}
    res = await executor.execute(
        target, kind, principal=principal, decision=decision, arguments=args
    )
    await record(
        session,
        target=target,
        incident_id=incident.id,
        principal=str(principal),
        tool=kind,
        decision="done" if res.ok else "failed",
        plan_id=plan_id,
        result=res.output[:500],
    )
    if res.ok:
        await transition(session, incident, ACTED)
    else:
        await transition(session, incident, ESCALATED, resolution=f"{kind} --approve failed")
    return res.ok
