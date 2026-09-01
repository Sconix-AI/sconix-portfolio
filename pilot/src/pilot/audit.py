"""Append-only record of every action the pilot proposed, allowed, denied, ran.

This is the trail a reviewer (or an on-call human) reads after the fact: what the
agent wanted to do, whether policy let it, and what happened. Lives in the same
``pilot.db`` as the `AgentRun` accounting rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Action(SQLModel, table=True):
    __tablename__ = "actions"

    id: int | None = Field(default=None, primary_key=True)
    target: str = Field(index=True)
    incident_id: int | None = Field(default=None, index=True)
    principal: str = "ops-agent:pilot"  # who caused this
    tool: str
    args: str = "{}"  # json
    decision: str  # allow | deny | planned | done | failed
    plan_id: str | None = Field(default=None, index=True)  # deploy/rollback plan, if any
    reason: str = ""
    result: str = ""
    duration_ms: int = 0
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


async def record(session: Any, **fields: Any) -> Action:
    row = Action(**fields)
    session.add(row)
    await session.flush()
    return row
