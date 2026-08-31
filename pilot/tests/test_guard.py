"""No-network tests for the policy gate + audit trail (slice 2)."""

from __future__ import annotations

import pytest
from sconixapp.agent import guarded_tool
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel, select

from pilot.act import make_guard
from pilot.audit import Action


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


async def _rows(session) -> list[Action]:
    return list((await session.execute(select(Action).order_by(Action.id))).scalars().all())


async def test_gate_denies_target_not_in_allow_set(session) -> None:
    calls: list[str] = []

    async def restart_app(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted"

    guard = make_guard(session, allow=set(), run_result={})
    tool = guarded_tool(restart_app, guard=guard)

    res = await tool.call({"app": "relnotes"})
    assert res.startswith("BLOCKED:") and "not an approved target" in res
    assert calls == []

    rows = await _rows(session)
    assert [r.decision for r in rows] == ["denied"]
    assert rows[0].target == "relnotes" and rows[0].tool == "restart_app"


async def test_gate_allows_warn_or_down_target(session) -> None:
    calls: list[str] = []

    async def restart_app(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted skillforge"

    run_result: dict[str, str] = {}
    guard = make_guard(session, allow={"skillforge"}, run_result=run_result)
    tool = guarded_tool(restart_app, guard=guard)

    res = await tool.call({"app": "skillforge"})
    assert res == "restarted skillforge" and calls == ["skillforge"]
    assert run_result == {"skillforge": "allowed"}

    rows = await _rows(session)
    assert [r.decision for r in rows] == ["allowed"]
    assert rows[0].reason == "warn/down + --fix"


async def test_gate_denies_unknown_tool(session) -> None:
    async def delete_everything(app: str) -> str:
        """Nope."""
        return "should not run"

    guard = make_guard(session, allow={"skillforge"}, run_result={})
    tool = guarded_tool(delete_everything, guard=guard)

    res = await tool.call({"app": "skillforge"})
    assert res.startswith("BLOCKED:") and "no policy for tool" in res
    rows = await _rows(session)
    assert rows[0].decision == "denied" and rows[0].tool == "delete_everything"
