"""Incident memory: what's wrong, since when, and how many times we've seen it.

Without this the agent is a goldfish — every tick looks like the first. An
incident opens when a target first goes non-ok, accrues ticks while it stays
bad, and closes when it recovers. Recent history is fed back into the next
assessment so "flapping for an hour" reads differently from "just now".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel, select


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Incident(SQLModel, table=True):
    __tablename__ = "incidents"

    id: int | None = Field(default=None, primary_key=True)
    target: str = Field(index=True)
    opened_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    closed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    ticks: int = 1  # observations while open
    last_severity: str = "warn"
    last_headline: str = ""

    @property
    def open(self) -> bool:
        return self.closed_at is None


async def _open_incident(session: Any, target: str) -> Incident | None:
    return (
        (
            await session.execute(
                select(Incident).where(Incident.target == target, Incident.closed_at.is_(None))
            )
        )
        .scalars()
        .first()
    )


async def observe(session: Any, target: str, severity: str, headline: str) -> Incident | None:
    """Record one observation. Opens / bumps / closes the target's incident.

    Returns the currently-open incident for the target, or ``None`` if healthy.
    """
    inc = await _open_incident(session, target)
    if severity == "ok":
        if inc is not None:
            inc.closed_at = _utcnow()
            session.add(inc)
            await session.flush()
        return None

    if inc is None:
        inc = Incident(target=target, last_severity=severity, last_headline=headline)
    else:
        inc.ticks += 1
        inc.last_severity = severity
        inc.last_headline = headline
    session.add(inc)
    await session.flush()
    return inc


async def history(session: Any, target: str, *, limit: int = 5) -> list[Incident]:
    """Recent incidents for a target, newest first (open ones included)."""
    return list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.target == target)
                .order_by(Incident.opened_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def summarize(incidents: list[Incident]) -> str:
    """A short line the assessor can read as context."""
    if not incidents:
        return "no prior incidents for this target"
    parts: list[str] = []
    for i in incidents:
        state = f"open, {i.ticks} ticks" if i.open else "resolved"
        since = i.opened_at.strftime("%H:%M")
        parts.append(f"[{state}] since {since}: {i.last_severity} — {i.last_headline}")
    return " | ".join(parts)
