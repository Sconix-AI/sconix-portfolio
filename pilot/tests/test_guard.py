"""No-network tests for the policy gate + audit trail."""

from __future__ import annotations

import functools

from sconixapp.agent import guarded_tool
from sqlmodel import select

from pilot.act import make_guard as _make_guard
from pilot.audit import Action
from tests.conftest import FakeExecutor

# these tests exercise the gate's own checks; the executor just declares `restart`
make_guard = functools.partial(_make_guard, executor=FakeExecutor())


async def _rows(session) -> list[Action]:
    return list((await session.execute(select(Action).order_by(Action.id))).scalars().all())


async def test_gate_denies_target_not_in_allow_set(session) -> None:
    calls: list[str] = []

    async def restart(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted"

    guard = make_guard(session, allow=set(), run_result={})
    tool = guarded_tool(restart, guard=guard)

    res = await tool.call({"app": "relnotes"})
    assert res.startswith("BLOCKED:") and "not an approved target" in res
    assert calls == []

    rows = await _rows(session)
    assert [r.decision for r in rows] == ["deny"]
    assert rows[0].target == "relnotes" and rows[0].tool == "restart"
    assert rows[0].principal == "ops-agent:pilot"


async def test_gate_allows_warn_or_down_target(session) -> None:
    calls: list[str] = []

    async def restart(app: str) -> str:
        """Restart one app."""
        calls.append(app)
        return "restarted skillforge"

    run_result: dict[str, str] = {}
    guard = make_guard(session, allow={"skillforge"}, run_result=run_result)
    tool = guarded_tool(restart, guard=guard)

    res = await tool.call({"app": "skillforge"})
    assert res == "restarted skillforge" and calls == ["skillforge"]
    assert run_result == {"skillforge": "allow"}

    rows = await _rows(session)
    assert [r.decision for r in rows] == ["allow"]
    assert rows[0].reason.startswith("warn/down + --fix")


async def test_gate_denies_unknown_tool(session) -> None:
    async def delete_everything(app: str) -> str:
        """Nope."""
        return "should not run"

    guard = make_guard(session, allow={"skillforge"}, run_result={})
    tool = guarded_tool(delete_everything, guard=guard)

    res = await tool.call({"app": "skillforge"})
    assert res.startswith("BLOCKED:") and "undeclared action" in res
    rows = await _rows(session)
    assert rows[0].decision == "deny" and rows[0].tool == "delete_everything"
