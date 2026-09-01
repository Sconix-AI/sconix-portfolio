"""Adapter tests — how Pilot consumes a manifest-declared action through the
`ActionExecutor` seam. When `sconixcore`'s `ManifestExecutor` lands, `DEFAULT`
is swapped and these should still pass. See PILOT_SLICE4_ADAPTER.md.
"""

from __future__ import annotations

import pytest
from sconixapp.agent import guarded_tool
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sconixcore import ActionSpec, ApprovalMode, PrincipalKind, Risk, Verification
from sqlmodel import SQLModel, select

from pilot import act
from pilot.audit import Action, record
from pilot.executor import DEFAULT, ActionExecutor, ExecResult, LocalExecutor
from pilot.principal import PILOT, Principal

_V = Verification(checks=("healthz",), within_seconds=5, attempts=1, interval_seconds=0.01)


def _spec(name: str, *, approval: ApprovalMode) -> ActionSpec:
    return ActionSpec(
        name=name,
        argv=("sx", name, "{project}"),
        risk=Risk.EXTERNAL_WRITE,
        idempotent=True,
        approval=approval,
        verification=_V,
    )


class FakeExecutor:
    """Stands in for the future sconixcore ManifestExecutor."""

    def __init__(self, specs: dict[str, ActionSpec], result: ExecResult | None = None) -> None:
        self._specs = specs
        self._result = result or ExecResult(True, ("sx", "restart", "x"), "restarted x", 5)
        self.calls: list[tuple[str, str]] = []

    def lookup(self, target: str, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    async def execute(self, target, name, *, principal, decision=None, arguments=None):
        if name not in self._specs:
            raise KeyError(name)
        self.calls.append((target, name))
        return self._result


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


# --- the executor itself ------------------------------------------------------


def test_lookup_by_name() -> None:
    ex = LocalExecutor()
    assert ex.lookup("relnotes", "restart") is act.RESTART
    assert ex.lookup("relnotes", "deploy") is None


def test_default_executor_satisfies_the_protocol() -> None:
    assert isinstance(DEFAULT, ActionExecutor)


async def test_execute_binds_target_into_argv_without_a_shell(tmp_path) -> None:
    tf = tmp_path / "t.yaml"
    tf.write_text(
        "targets:\n  - name: drill\n    url: http://x\n"
        "    restart: sh -c 'printf wedged-recovered'\n"
    )
    act.TARGETS_PATH = tf
    try:
        res = await LocalExecutor().execute("drill", "restart", principal=PILOT)
        assert res.ok and res.argv == ("sh", "-c", "printf wedged-recovered")
        assert "wedged-recovered" in res.output
    finally:
        act.TARGETS_PATH = None


async def test_execute_undeclared_action_raises() -> None:
    with pytest.raises(KeyError):
        await LocalExecutor().execute("drill", "delete_env", principal=PILOT)


async def test_execute_failed_command_is_not_ok(tmp_path) -> None:
    tf = tmp_path / "t.yaml"
    tf.write_text("targets:\n  - name: drill\n    url: http://x\n    restart: sh -c 'exit 5'\n")
    act.TARGETS_PATH = tf
    try:
        res = await LocalExecutor().execute("drill", "restart", principal=PILOT)
        assert res.ok is False and "exit 5" in res.output
    finally:
        act.TARGETS_PATH = None


# --- the gate reading the manifest ------------------------------------------------


async def _decide(session, executor, *, principal=PILOT, allow=None, tool="restart"):
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
    ex = FakeExecutor({"restart": _spec("restart", approval=ApprovalMode.NEVER)})
    _out, rr, calls = await _decide(session, ex, allow=set())  # not in allow-set…
    assert calls == ["drill"] and rr["drill"] == "allow"  # …but approval=never


async def test_gate_reads_approval_always_and_denies(session) -> None:
    ex = FakeExecutor({"restart": _spec("restart", approval=ApprovalMode.ALWAYS)})
    out, _rr, calls = await _decide(session, ex)
    assert out.startswith("BLOCKED:") and "requires human approval" in out
    assert calls == []


async def test_gate_enforces_principal_scope(session) -> None:
    scoped = Principal(kind=PrincipalKind.AGENT, id="pilot", role="ops", scope=("relnotes",))
    ex = FakeExecutor({"restart": _spec("restart", approval=ApprovalMode.POLICY)})
    out, _rr, calls = await _decide(session, ex, principal=scoped)
    assert "scope does not include drill" in out and calls == []


async def test_executor_result_reaches_the_audit_row(session) -> None:
    ex = FakeExecutor(
        {"restart": _spec("restart", approval=ApprovalMode.POLICY)},
        result=ExecResult(False, ("sx", "restart", "drill"), "restart failed (exit 1)", 12),
    )
    rr: dict[str, str] = {}
    guard = act.make_guard(session, allow={"drill"}, run_result=rr, executor=ex)

    async def _fn(app: str) -> str:
        """Mirrors run.tick's _restart_and_record: gate allows, executor runs, outcome audited."""
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
    rows = await _rows(session)
    assert [r.decision for r in rows] == ["allow", "failed"]
    assert "exit 1" in rows[-1].result
