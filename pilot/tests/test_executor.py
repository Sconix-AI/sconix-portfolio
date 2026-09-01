"""Adapter tests — Pilot consuming a manifest-declared action through
`sconixcore.ManifestExecutor` and the `ActionExecutor` seam.
See PILOT_SLICE4_ADAPTER.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sconixapp.agent import guarded_tool
from sconixcore import (
    ActionError,
    ApprovalMode,
    Decision,
    DecisionOutcome,
    ExecutionResult,
    ManifestExecutor,
    PrincipalKind,
)
from sqlmodel import select

from pilot import act
from pilot.audit import Action, record
from pilot.executor import ActionExecutor
from pilot.principal import PILOT, Principal
from tests.conftest import FakeExecutor, spec

# --- a real ManifestExecutor over an in-memory manifest ----------------------

_MANIFEST = {
    "schema": "sconix.dev/project/v1",
    "kind": "application",
    "name": "Drill",
    "slug": "drill",
    "lifecycle": {"status": "live"},
    "commands": {
        "restart": {
            "run": ["printf", "recovered"],
            "risk": "external-write",
            "approval": "policy",
            "idempotent": True,
            "verify": {"checks": ["healthz"], "withinSeconds": 5, "attempts": 1},
        }
    },
}


def _resolve(target: str):
    if target != "drill":
        raise KeyError(target)
    return _MANIFEST, Path.cwd()


def _mex() -> ManifestExecutor:
    return ManifestExecutor(resolve=_resolve)


def test_manifest_executor_satisfies_the_protocol() -> None:
    assert isinstance(_mex(), ActionExecutor)


def test_lookup_reads_the_manifest() -> None:
    ex = _mex()
    action = ex.lookup("drill", "restart")
    assert action is not None and action.risk.value == "external-write"
    assert ex.lookup("drill", "deploy") is None


def _allow() -> Decision:
    return Decision(DecisionOutcome.ALLOW, PILOT, datetime.now(UTC).isoformat())


async def test_execute_runs_declared_argv_with_no_shell() -> None:
    res = await _mex().execute("drill", "restart", principal=PILOT, decision=_allow())
    assert res.ok and res.argv == ("printf", "recovered")
    assert "recovered" in res.output and res.duration_ms >= 0


async def test_execute_undeclared_action_raises() -> None:
    with pytest.raises(KeyError):
        await _mex().execute("drill", "delete_env", principal=PILOT, decision=_allow())


async def test_execute_enforces_principal_scope() -> None:
    scoped = Principal(kind=PrincipalKind.AGENT, id="pilot", role="ops", scope=("relnotes",))
    with pytest.raises(ActionError, match="scope"):
        await _mex().execute("drill", "restart", principal=scoped, decision=_allow())


# --- the gate reading the manifest via the executor --------------------------


async def _decide(
    session, executor: ActionExecutor, *, principal=PILOT, allow=None, tool="restart"
):
    rr: dict[str, str] = {}
    guard = act.make_guard(
        session,
        allow=allow if allow is not None else {"drill"},
        run_result=rr,
        principal=principal,
        executor=executor,
    )
    calls: list[str] = []

    async def _fn(app: str) -> str:
        """x."""
        calls.append(app)
        return "ran"

    _fn.__name__ = tool
    out = await guarded_tool(_fn, guard=guard).call({"app": "drill"})
    return out, rr, calls


async def test_gate_denies_action_not_in_the_manifest(session) -> None:
    out, rr, calls = await _decide(session, FakeExecutor({}))
    assert out.startswith("BLOCKED:") and "undeclared action" in out
    assert calls == [] and rr["drill"] == "deny"


async def test_gate_reads_approval_never_and_allows(session) -> None:
    ex = FakeExecutor({"restart": spec(approval=ApprovalMode.NEVER)})
    _out, rr, calls = await _decide(session, ex, allow=set())
    assert calls == ["drill"] and rr["drill"] == "allow"


async def test_gate_reads_approval_always_and_denies(session) -> None:
    ex = FakeExecutor({"restart": spec(approval=ApprovalMode.ALWAYS)})
    out, _rr, calls = await _decide(session, ex)
    assert out.startswith("BLOCKED:") and "requires human approval" in out
    assert calls == []


async def test_gate_enforces_principal_scope(session) -> None:
    scoped = Principal(kind=PrincipalKind.AGENT, id="pilot", role="ops", scope=("relnotes",))
    out, _rr, calls = await _decide(session, FakeExecutor(), principal=scoped)
    assert "scope does not include drill" in out and calls == []


async def test_executor_result_reaches_the_audit_row(session) -> None:
    failed = ExecutionResult(spec(), 1, "", "restart failed (exit 1)", 12)
    ex = FakeExecutor(result=failed)
    rr: dict[str, str] = {}
    guard = act.make_guard(session, allow={"drill"}, run_result=rr, executor=ex)

    async def _fn(app: str) -> str:
        """Mirrors run.tick's _restart_and_record."""
        res = await ex.execute(app, "restart", principal=PILOT)
        await record(
            session,
            target=app,
            tool="restart",
            decision="done" if res.ok else "failed",
            result=res.output,
        )
        return res.output

    _fn.__name__ = "restart"
    await guarded_tool(_fn, guard=guard).call({"app": "drill"})
    rows = list((await session.execute(select(Action).order_by(Action.id))).scalars().all())
    assert [r.decision for r in rows] == ["allow", "failed"]
    assert "exit 1" in rows[-1].result
