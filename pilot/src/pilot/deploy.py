"""Deploy / rollback through the human-approval boundary.

Pilot may **propose** a plan (`deploy_plan` / `rollback_plan` — `approval: never`)
and, once a human has run `sx approve`, **execute** the approved action
(`deploy` / `rollback` — `approval: always`). Pilot never approves anything.

Plans, approvals, and executions live in Sconix's plan store
(`$SCONIX_STATE_DIR/deploy/…`, read via `sconixcore.load_record`) — Pilot only
records the plan id and the outcome on its own incident/audit trail.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from sconixcore import (
    Decision,
    DecisionOutcome,
    DeployRecordError,
    Principal,
    PrincipalKind,
    load_record,
)

from pilot.audit import record
from pilot.memory import (
    ACTED,
    APPROVED,
    AWAITING_APPROVAL,
    ESCALATED,
    PROPOSED,
    Incident,
    transition,
)

# kind -> plan action is `<kind>_plan` (approval: never); apply action is `<kind>`
# (approval: always). All follow the same propose -> human-approve -> execute path.
Kind = Literal["deploy", "rollback", "canary", "promote", "canary_teardown"]

# Only rollback is ever proposed by the autonomous loop (and only with
# --allow-rollback). canary / promote / canary_teardown are operator-initiated:
# a human kicks off `propose`, Pilot only relays the approved execution.
AUTONOMOUS_KINDS: frozenset[str] = frozenset({"rollback"})

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
    try:
        load_record("approvals", plan_id)
    except DeployRecordError:
        return "pending"
    try:
        load_record("executions", plan_id)
    except DeployRecordError:
        return "approved"
    return "consumed"


def should_rollback(incident: Incident) -> bool:
    """When does rollback beat restart? Only once a restart has been *tried this
    incident and failed to recover it* — i.e. it isn't a transient/wedged
    process. A rollback is a heavier, human-approved move for a bad release."""
    return (
        incident.open
        and incident.last_severity in ("warn", "down")
        and incident.acted_at is not None  # a restart ran
        and incident.state in (ESCALATED, "diagnosed")  # ...and didn't stick
        and incident.plan_id is None  # no plan proposed yet
    )


async def propose(
    session: Any,
    executor: Any,
    *,
    target: str,
    kind: Kind,
    principal: Principal,
    incident: Any,
    arguments: dict[str, str] | None = None,
) -> str:
    """Run ``<kind>_plan``, record the plan id, park the incident on
    ``awaiting_approval``. Returns the plan id. `canary` / `promote` /
    `canary_teardown` are operator-initiated — the watch loop only ever calls
    this with ``kind="rollback"``."""
    plan_action = f"{kind}_plan"
    if incident.state != PROPOSED:
        await transition(session, incident, PROPOSED)
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
        plan_kind=kind,
        plan_args=json.dumps(arguments or {}),
        resolution=f"awaiting human approval — sx approve {plan_id}",
    )
    return plan_id


async def resume_if_approved(
    session: Any,
    executor: Any,
    *,
    target: str,
    incident: Incident,
    principal: Principal,
    arguments: dict[str, str] | None = None,
) -> bool | None:
    """For a parked incident: if a human has now approved its plan, execute it.
    Returns `execute_approved`'s result, or None if there's nothing to do yet."""
    if incident.state != AWAITING_APPROVAL or not incident.plan_id:
        return None
    if approval_status(incident.plan_id) != "approved":
        return None
    stored = json.loads(incident.plan_args or "{}")
    return await execute_approved(
        session,
        executor,
        target=target,
        kind=incident.plan_kind or "rollback",
        plan_id=incident.plan_id,
        principal=principal,
        incident=incident,
        arguments={**stored, **(arguments or {})},
    )


async def execute_approved(
    session: Any,
    executor: Any,
    *,
    target: str,
    kind: Kind,
    plan_id: str,
    principal: Principal,
    incident: Any,
    arguments: dict[str, str] | None = None,
) -> bool:
    """If a human has approved ``plan_id``, run the ``<kind>`` action with it.
    Records stale / consumed / denied / failed / verified. Returns True on a
    verified success. Works for any kind — promotion needs its **own** fresh
    approval, bound to the verified canary."""
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
