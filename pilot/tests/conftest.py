"""Shared test helpers — a fake `ActionExecutor` and a DB session fixture."""

from __future__ import annotations

import pytest
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sconixcore import ActionSpec, ApprovalMode, ExecutionResult, Risk, Verification
from sqlmodel import SQLModel

_V = Verification(("healthz",), within_seconds=5, attempts=1, interval_seconds=0.01)


def spec(name: str = "restart", *, approval: ApprovalMode = ApprovalMode.POLICY) -> ActionSpec:
    return ActionSpec(
        name=name,
        argv=("sx", name, "{project}"),
        risk=Risk.EXTERNAL_WRITE,
        idempotent=True,
        approval=approval,
        verification=_V,
    )


class FakeExecutor:
    """Implements the `ActionExecutor` Protocol without a real manifest."""

    def __init__(
        self,
        specs: dict[str, ActionSpec] | None = None,
        result: ExecutionResult | None = None,
    ) -> None:
        self.specs = specs if specs is not None else {"restart": spec()}
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def lookup(self, target: str, name: str) -> ActionSpec | None:
        return self.specs.get(name)

    async def execute(self, target, name, *, principal, decision=None, arguments=None):
        if name not in self.specs:
            raise KeyError(name)
        self.calls.append((target, name))
        return self._result or ExecutionResult(self.specs[name], 0, "restarted", "", 3)


@pytest.fixture()
def fake_executor() -> FakeExecutor:
    return FakeExecutor()


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
