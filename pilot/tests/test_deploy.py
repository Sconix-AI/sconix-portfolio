"""Slice 4b — deploy/rollback proposal + the human-approval boundary.

Never touches a live server: a fake executor + a temp SCONIX_STATE_DIR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sconixcore import ActionSpec, ApprovalMode, ExecutionResult, Risk, Verification

from pilot.deploy import (
    approval_status,
    execute_approved,
    propose,
    resume_if_approved,
    should_rollback,
)
from pilot.memory import (
    ACTED,
    APPROVED,
    AWAITING_APPROVAL,
    DIAGNOSED,
    ESCALATED,
    PROPOSED,
    Incident,
    observe,
    transition,
)

PLAN_ID = "a1b2c3d4e5f6a7b8c9d0"
_V = Verification(("healthz",), within_seconds=5, attempts=1)


def _spec(name: str, approval: ApprovalMode) -> ActionSpec:
    args = ("plan_id",) if name in ("deploy", "rollback") else ()
    return ActionSpec(
        name=name,
        argv=("sx", name, "{project}"),
        risk=Risk.EXTERNAL_WRITE,
        idempotent=False,
        approval=approval,
        verification=_V,
        arguments={a: "" for a in args},
    )


class DeployExecutor:
    """Fake: deploy_plan prints a plan id; deploy/rollback succeed unless told not to."""

    def __init__(self, *, plan_ok: bool = True, apply_ok: bool = True) -> None:
        self.plan_ok = plan_ok
        self.apply_ok = apply_ok
        self.calls: list[tuple[str, str, dict]] = []

    def lookup(self, target: str, name: str) -> ActionSpec | None:
        if name.endswith("_plan"):
            return _spec(name, ApprovalMode.NEVER)
        if name in ("deploy", "rollback", "canary", "promote", "canary_teardown"):
            return _spec(name, ApprovalMode.ALWAYS)
        return None

    async def execute(self, target, name, *, principal, decision=None, arguments=None):
        self.calls.append((target, name, dict(arguments or {})))
        if name.endswith("_plan"):
            rc = 0 if self.plan_ok else 1
            return ExecutionResult(self.lookup(target, name), rc, f"created plan {PLAN_ID}", "", 4)
        rc = 0 if self.apply_ok else 1
        out = "verified" if self.apply_ok else "verify failed"
        return ExecutionResult(self.lookup(target, name), rc, out, "", 9)


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SCONIX_STATE_DIR", str(tmp_path))
    return tmp_path


def _write(state: Path, kind: str, pid: str, body: dict) -> None:
    p = state / "deploy" / kind / f"{pid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body))


def _approval(pid: str = PLAN_ID) -> dict:
    return {
        "schema": "sconix.dev/deploy/approval/v1",
        "planId": pid,
        "outcome": "allow-once",
        "principal": {"kind": "human", "id": "yusuf", "role": "approver"},
        "reason": "ship it",
        "approvedAt": "2026-09-01T00:00:00+00:00",
    }


async def _proposed_incident(session, target="relnotes") -> Incident:
    inc = await observe(session, target, "down", "hard down", "ops-agent:pilot")
    await transition(session, inc, DIAGNOSED)
    await transition(session, inc, PROPOSED)
    return inc


# --- proposal ---------------------------------------------------------------


async def test_propose_records_plan_and_parks_incident(session) -> None:
    inc = await _proposed_incident(session)
    plan_id = await propose(
        session,
        DeployExecutor(),
        target="relnotes",
        kind="deploy",
        principal=_pilot(),
        incident=inc,
    )
    assert plan_id == PLAN_ID
    inc = await session.get(Incident, inc.id)
    assert inc.state == AWAITING_APPROVAL and inc.plan_id == PLAN_ID
    assert "sx approve" in inc.resolution

    from sqlmodel import select

    from pilot.audit import Action

    rows = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    planned = [r for r in rows if r.decision == "planned"]
    assert planned and planned[0].plan_id == PLAN_ID and planned[0].tool == "deploy_plan"


async def test_propose_failed_plan_escalates(session) -> None:
    inc = await _proposed_incident(session)
    with pytest.raises(RuntimeError):
        await propose(
            session,
            DeployExecutor(plan_ok=False),
            target="relnotes",
            kind="deploy",
            principal=_pilot(),
            incident=inc,
        )
    inc = await session.get(Incident, inc.id)
    assert inc.state == ESCALATED


# --- approval status ------------------------------------------------------------


def test_approval_status_transitions(_state_dir) -> None:
    assert approval_status(PLAN_ID) == "pending"
    _write(_state_dir, "approvals", PLAN_ID, _approval())
    assert approval_status(PLAN_ID) == "approved"
    _write(_state_dir, "executions", PLAN_ID, {"planId": PLAN_ID, "status": "executing"})
    assert approval_status(PLAN_ID) == "consumed"


# --- execute after approval -----------------------------------------------------


async def test_execute_denied_while_pending(session) -> None:
    inc = await _park(session)
    ok = await execute_approved(
        session,
        DeployExecutor(),
        target="relnotes",
        kind="deploy",
        plan_id=PLAN_ID,
        principal=_pilot(),
        incident=inc,
    )
    assert ok is False
    from sqlmodel import select

    from pilot.audit import Action

    rows = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    assert rows[-1].decision == "denied" and "pending" in rows[-1].reason


async def test_execute_runs_when_human_approved(session, _state_dir) -> None:
    inc = await _park(session)
    _write(_state_dir, "approvals", PLAN_ID, _approval())
    ex = DeployExecutor()
    ok = await execute_approved(
        session,
        ex,
        target="relnotes",
        kind="deploy",
        plan_id=PLAN_ID,
        principal=_pilot(),
        incident=inc,
    )
    assert ok is True
    apply_call = [c for c in ex.calls if c[1] == "deploy"][0]
    assert apply_call[2] == {"plan_id": PLAN_ID}
    inc = await session.get(Incident, inc.id)
    assert inc.state == ACTED


async def test_execute_consumed_plan_escalates(session, _state_dir) -> None:
    inc = await _park(session)
    _write(_state_dir, "approvals", PLAN_ID, _approval())
    _write(_state_dir, "executions", PLAN_ID, {"planId": PLAN_ID, "status": "executing"})
    ok = await execute_approved(
        session,
        DeployExecutor(),
        target="relnotes",
        kind="deploy",
        plan_id=PLAN_ID,
        principal=_pilot(),
        incident=inc,
    )
    assert ok is False
    inc = await session.get(Incident, inc.id)
    assert inc.state == ESCALATED and "consumed" in inc.resolution


# --- the "rollback beats restart" policy -------------------------------------


async def test_should_rollback_only_after_a_failed_restart(session) -> None:
    inc = await observe(session, "relnotes", "down", "hard down", "ops-agent:pilot")
    await transition(session, inc, DIAGNOSED)
    assert should_rollback(inc) is False  # no restart tried yet

    await transition(session, inc, PROPOSED)
    await transition(session, inc, APPROVED)
    await transition(session, inc, ACTED)  # a restart ran (acted_at set)
    await transition(session, inc, ESCALATED, resolution="denied: already restarted")
    assert should_rollback(inc) is True

    inc.plan_id = "deadbeefdeadbeefdead"  # a plan already exists -> don't re-propose
    assert should_rollback(inc) is False


async def test_should_not_rollback_a_recovered_target(session) -> None:
    inc = await _proposed_incident(session)
    await transition(session, inc, APPROVED)
    await transition(session, inc, ACTED)
    await transition(session, inc, ESCALATED)
    inc.last_severity = "ok"
    assert should_rollback(inc) is False


# --- resume_if_approved (what the watch loop calls each tick) ------------------


async def test_resume_noops_while_pending(session) -> None:
    inc = await _park(session)
    assert (
        await resume_if_approved(
            session, DeployExecutor(), target="relnotes", incident=inc, principal=_pilot()
        )
        is None
    )


async def test_resume_executes_once_approved(session, _state_dir) -> None:
    inc = await _park(session)
    inc.plan_kind = "rollback"
    _write(_state_dir, "approvals", PLAN_ID, _approval())
    ex = DeployExecutor()
    result = await resume_if_approved(
        session,
        ex,
        target="relnotes",
        incident=inc,
        principal=_pilot(),
        arguments={"release": "v1.2.2"},
    )
    assert result is True
    call = [c for c in ex.calls if c[1] == "rollback"][0]
    assert call[2] == {"plan_id": PLAN_ID, "release": "v1.2.2"}
    inc = await session.get(Incident, inc.id)
    assert inc.state == ACTED


# --- canary / promote / teardown (operator-initiated, same approval path) ------


async def test_canary_lifecycle_kinds_share_the_propose_path(session) -> None:
    from pilot.deploy import AUTONOMOUS_KINDS

    assert AUTONOMOUS_KINDS == frozenset({"rollback"})  # loop never proposes canary/promote

    inc = await _proposed_incident(session, target="skillforge")
    ex = DeployExecutor()
    plan_id = await propose(
        session, ex, target="skillforge", kind="canary", principal=_pilot(), incident=inc
    )
    assert plan_id == PLAN_ID
    assert ("skillforge", "canary_plan", {}) in ex.calls
    inc = await session.get(Incident, inc.id)
    assert inc.state == AWAITING_APPROVAL and inc.plan_kind == "canary"


async def test_promote_replays_stored_args_on_approval(session, _state_dir) -> None:
    inc = await _proposed_incident(session, target="relnotes")
    ex = DeployExecutor()
    await propose(
        session,
        ex,
        target="relnotes",
        kind="promote",
        principal=_pilot(),
        incident=inc,
        arguments={"canary_plan_id": "cafebabecafebabecafe"},
    )
    inc = await session.get(Incident, inc.id)
    assert json.loads(inc.plan_args) == {"canary_plan_id": "cafebabecafebabecafe"}

    _write(_state_dir, "approvals", PLAN_ID, _approval())
    ok = await resume_if_approved(session, ex, target="relnotes", incident=inc, principal=_pilot())
    assert ok is True
    promote_call = [c for c in ex.calls if c[1] == "promote"][0]
    assert promote_call[2] == {"plan_id": PLAN_ID, "canary_plan_id": "cafebabecafebabecafe"}


# --- helpers ------------------------------------------------------------------


def _pilot():
    from pilot.principal import PILOT

    return PILOT


async def _park(session) -> Incident:
    inc = await _proposed_incident(session)
    return await transition(session, inc, AWAITING_APPROVAL, plan_id=PLAN_ID)
