"""`pilot report` — read-only history. Must not mutate pilot.db."""

from __future__ import annotations

import json

from sqlmodel import func, select

from pilot.audit import Action
from pilot.memory import (
    ACTED,
    APPROVED,
    AWAITING_APPROVAL,
    DIAGNOSED,
    PROPOSED,
    RESOLVED,
    Incident,
    observe,
    transition,
)
from pilot.report import gather, render_text

WHO = "ops-agent:pilot"


async def _seed(session) -> None:
    # a resolved-after-restart incident
    a = await observe(session, "relnotes", "down", "healthz 503", WHO)
    await transition(session, a, DIAGNOSED, diagnosis="wedged", confidence=0.8)
    session.add(
        Action(target="relnotes", incident_id=a.id, principal=WHO, tool="restart", decision="allow")
    )
    await transition(session, a, PROPOSED)
    await transition(session, a, APPROVED)
    await transition(session, a, ACTED)
    await transition(session, a, "verified", last_severity="ok")
    await transition(session, a, RESOLVED, resolution="recovered after action")

    # an open incident parked on approval
    b = await observe(session, "skillforge", "down", "hard down", WHO)
    await transition(session, b, DIAGNOSED)
    await transition(session, b, PROPOSED)
    await transition(
        session, b, AWAITING_APPROVAL, plan_id="a1b2c3d4e5f6a7b8c9d0", plan_kind="rollback"
    )
    session.add(
        Action(
            target="skillforge",
            incident_id=b.id,
            principal=WHO,
            tool="rollback_plan",
            decision="planned",
            plan_id="a1b2c3d4e5f6a7b8c9d0",
        )
    )
    await session.commit()


async def test_gather_shapes_incidents_and_summary(session) -> None:
    await _seed(session)
    data = await gather(session)

    assert data["summary"]["incidents"] == 2
    assert data["summary"]["open"] == 1
    assert data["summary"]["resolved"] == 1
    assert data["summary"]["actions"] == 2

    by_id = {i["id"]: i for i in data["incidents"]}
    resolved = next(i for i in data["incidents"] if i["state"] == "resolved")
    assert resolved["target"] == "relnotes"
    assert resolved["closed_at"] is not None and resolved["open"] is False
    assert resolved["confidence"] == 0.8
    assert [a["decision"] for a in resolved["actions"]] == ["allow"]

    parked = next(i for i in data["incidents"] if i["state"] == AWAITING_APPROVAL)
    assert parked["plan_kind"] == "rollback" and parked["plan_id"] == "a1b2c3d4e5f6a7b8c9d0"
    assert parked["actions"][0]["tool"] == "rollback_plan"
    assert by_id  # both present


async def test_filters(session) -> None:
    await _seed(session)
    only_rel = await gather(session, target="relnotes")
    assert [i["target"] for i in only_rel["incidents"]] == ["relnotes"]

    open_only = await gather(session, only_open=True)
    assert [i["target"] for i in open_only["incidents"]] == ["skillforge"]

    capped = await gather(session, limit=1)
    assert len(capped["incidents"]) == 1


async def test_render_text_and_json_are_stable(session) -> None:
    await _seed(session)
    data = await gather(session)
    text = render_text(data)
    assert "recovered after action" in text
    assert "rollback a1b2c3d4e5f6a7b8c9d0" in text
    assert "escalated" in text  # summary line
    json.loads(json.dumps(data))  # round-trips


async def test_gather_does_not_mutate(session) -> None:
    await _seed(session)
    before = (
        (await session.execute(select(func.count()).select_from(Incident))).scalar_one(),
        (await session.execute(select(func.count()).select_from(Action))).scalar_one(),
    )
    await gather(session)
    await gather(session, only_open=True)
    after = (
        (await session.execute(select(func.count()).select_from(Incident))).scalar_one(),
        (await session.execute(select(func.count()).select_from(Action))).scalar_one(),
    )
    assert before == after
    assert not session.new and not session.dirty and not session.deleted
