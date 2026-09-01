"""Incident lifecycle + the restart cooldown."""

from __future__ import annotations

import functools

import pytest
from sconixapp.agent import guarded_tool
from sqlmodel import select

from pilot.act import make_guard as _make_guard
from pilot.audit import Action
from pilot.memory import (
    ACTED,
    APPROVED,
    DETECTED,
    DIAGNOSED,
    PROPOSED,
    RESOLVED,
    VERIFIED,
    history,
    observe,
    summarize,
    transition,
)
from pilot.principal import PILOT
from tests.conftest import FakeExecutor

WHO = "ops-agent:pilot"
make_guard = functools.partial(_make_guard, executor=FakeExecutor())


async def _drive_to_acted(session, target="drill"):
    inc = await observe(session, target, "down", "healthz 503", WHO)
    await transition(session, inc, DIAGNOSED, diagnosis="wedged", confidence=0.7)
    await transition(session, inc, PROPOSED)
    await transition(session, inc, APPROVED)
    await transition(session, inc, ACTED)
    return inc


async def test_incident_opens_and_bumps(session) -> None:
    a = await observe(session, "relnotes", "down", "unreachable", WHO)
    assert a is not None and a.state == DETECTED and a.ticks == 1 and a.principal == WHO

    b = await observe(session, "relnotes", "down", "still unreachable", WHO)
    assert b.id == a.id and b.ticks == 2


async def test_recovers_without_action(session) -> None:
    inc = await observe(session, "relnotes", "warn", "slow", WHO)
    await transition(session, inc, DIAGNOSED)
    gone = await observe(session, "relnotes", "ok", "fine now", WHO)
    assert gone is None
    closed = (await history(session, "relnotes"))[0]
    assert closed.state == RESOLVED and closed.resolution == "recovered without action"


async def test_verify_and_resolve_after_action(session) -> None:
    await _drive_to_acted(session)
    gone = await observe(session, "drill", "ok", "healthy", WHO)
    assert gone is None
    inc = (await history(session, "drill"))[0]
    assert inc.state == RESOLVED
    assert inc.resolution == "recovered after action"
    assert inc.verified_at is not None and inc.acted_at is not None


async def test_still_bad_after_action_rolls_back_to_diagnosed(session) -> None:
    await _drive_to_acted(session)
    inc = await observe(session, "drill", "down", "still 503", WHO)
    assert inc.state == DIAGNOSED and inc.ticks == 2  # ready to escalate next


async def test_repeated_incident_is_a_new_row(session) -> None:
    first = await observe(session, "drill", "down", "x", WHO)
    await transition(session, first, DIAGNOSED)
    await observe(session, "drill", "ok", "recovered", WHO)
    second = await observe(session, "drill", "down", "again", WHO)
    assert second.id != first.id and second.ticks == 1
    assert len(await history(session, "drill")) == 2


async def test_illegal_transition_raises(session) -> None:
    inc = await observe(session, "drill", "down", "x", WHO)
    with pytest.raises(ValueError, match="illegal incident transition"):
        await transition(session, inc, VERIFIED)  # detected -> verified is not allowed


async def test_summarize_reads_as_context(session) -> None:
    assert "no prior incidents" in summarize([])
    inc = await observe(session, "skillforge", "down", "502 from edge", WHO)
    await transition(session, inc, DIAGNOSED)
    line = summarize(await history(session, "skillforge"))
    assert "diagnosed" in line and "502 from edge" in line


async def test_restart_cooldown_blocks_second_attempt(session) -> None:
    calls: list[str] = []

    async def restart(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted"

    def gtool():
        return guarded_tool(
            restart,
            guard=make_guard(
                session, allow={"relnotes"}, run_result={}, cooldown_s=600, principal=PILOT
            ),
        )

    assert await gtool().call({"app": "relnotes"}) == "restarted"
    second = await gtool().call({"app": "relnotes"})
    assert second.startswith("BLOCKED:") and "already restarted" in second
    assert calls == ["relnotes"]

    rows = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    assert [r.decision for r in rows] == ["allow", "deny"]
    assert all(r.principal == WHO for r in rows)  # label(PILOT)
