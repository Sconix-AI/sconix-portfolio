"""No-network tests for incident memory + the restart cooldown (slice 3)."""

from __future__ import annotations

import pytest
from sconixapp.agent import guarded_tool
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel, select

from pilot.act import make_guard
from pilot.audit import Action
from pilot.memory import history, observe, summarize


@pytest.fixture()
async def session():
    init_engine("sqlite+aiosqlite:///:memory:")
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    agen = get_session()
    s = await agen.__anext__()
    try:
        yield s
    finally:
        await s.rollback()
        await dispose_engine()


async def test_incident_opens_bumps_and_closes(session) -> None:
    a = await observe(session, "relnotes", "down", "unreachable")
    assert a is not None and a.ticks == 1 and a.open

    b = await observe(session, "relnotes", "down", "still unreachable")
    assert b.id == a.id and b.ticks == 2  # same incident, bumped

    c = await observe(session, "relnotes", "ok", "recovered")
    assert c is None
    closed = (await history(session, "relnotes"))[0]
    assert closed.open is False and closed.ticks == 2

    # a fresh failure opens a NEW incident, not a reopen
    d = await observe(session, "relnotes", "warn", "slow again")
    assert d.id != a.id and d.ticks == 1
    assert len(await history(session, "relnotes")) == 2


async def test_summarize_reads_as_context(session) -> None:
    assert "no prior incidents" in summarize([])
    await observe(session, "skillforge", "down", "502 from edge")
    line = summarize(await history(session, "skillforge"))
    assert "open, 1 ticks" in line and "502 from edge" in line


async def test_restart_cooldown_blocks_second_attempt(session) -> None:
    calls: list[str] = []

    async def restart_app(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted"

    def gtool():
        return guarded_tool(
            restart_app,
            guard=make_guard(session, allow={"relnotes"}, run_result={}, cooldown_s=600),
        )

    assert await gtool().call({"app": "relnotes"}) == "restarted"
    second = await gtool().call({"app": "relnotes"})
    assert second.startswith("BLOCKED:") and "already restarted" in second
    assert calls == ["relnotes"]

    decisions = [
        r.decision
        for r in (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    ]
    assert decisions == ["allowed", "denied"]
