"""Incident memory + lifecycle.

An incident moves through an explicit state machine:

    detected -> diagnosed -> proposed -> approved -> acted -> verified -> resolved
                    |            |   \\                 |
                    |            |    -> awaiting_approval -> approved (human said yes)
                    +----------- escalated <------------+     (needs a human)

- **detected**  first non-ok observation opens the incident
- **diagnosed** the assessor produced a severity + headline + detail (+ confidence)
- **proposed**  the fix agent proposed an action (a restart, or a deploy/rollback plan)
- **awaiting_approval**  a deploy/rollback *plan* exists; a human must `sx approve` it
- **approved**  the policy gate (restart) or a human approval (deploy/rollback) cleared it
- **acted**     the action ran
- **verified**  a *later* probe confirms the target is healthy again
- **resolved**  closed — either verified, or it recovered on its own
- **escalated** the gate refused, the plan went stale, or no safe action exists

Every incident records the `Principal` that caused it. Sources of truth are this
table plus `Action` (audit) and `AgentRun` (cost/tokens) — not chat logs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel, select

DETECTED = "detected"
DIAGNOSED = "diagnosed"
PROPOSED = "proposed"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
ACTED = "acted"
VERIFIED = "verified"
RESOLVED = "resolved"
ESCALATED = "escalated"

_OPEN_STATES = {DETECTED, DIAGNOSED, PROPOSED, AWAITING_APPROVAL, APPROVED, ACTED, ESCALATED}

# forward-only; escalate is reachable from any open working state
_ALLOWED: dict[str, set[str]] = {
    DETECTED: {DIAGNOSED, ESCALATED, RESOLVED},
    DIAGNOSED: {PROPOSED, ESCALATED, RESOLVED},
    PROPOSED: {AWAITING_APPROVAL, APPROVED, ESCALATED, RESOLVED},
    AWAITING_APPROVAL: {APPROVED, ESCALATED, RESOLVED},  # human approves, or it goes stale
    APPROVED: {ACTED, ESCALATED},
    ACTED: {VERIFIED, ESCALATED, DIAGNOSED},  # DIAGNOSED = still bad next tick
    VERIFIED: {RESOLVED},
    ESCALATED: {DIAGNOSED, RESOLVED},
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _dt() -> Any:
    return Column(DateTime(timezone=True), nullable=True)


class Incident(SQLModel, table=True):
    __tablename__ = "incidents"

    id: int | None = Field(default=None, primary_key=True)
    target: str = Field(index=True)
    principal: str = "ops-agent:pilot"
    state: str = Field(default=DETECTED, index=True)

    opened_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    acted_at: datetime | None = Field(default=None, sa_column=_dt())
    verified_at: datetime | None = Field(default=None, sa_column=_dt())
    closed_at: datetime | None = Field(default=None, sa_column=_dt())

    ticks: int = 1  # observations while open
    last_severity: str = "warn"
    last_headline: str = ""
    diagnosis: str = ""
    confidence: float | None = None
    resolution: str = ""
    plan_id: str | None = Field(default=None, index=True)  # a deploy/rollback plan, if any

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


async def _save(session: Any, inc: Incident) -> Incident:
    session.add(inc)
    await session.flush()
    return inc


async def transition(session: Any, inc: Incident, to: str, **fields: Any) -> Incident:
    """Move an incident to state ``to``. Raises on an illegal transition."""
    if to != inc.state and to not in _ALLOWED.get(inc.state, set()):
        raise ValueError(f"illegal incident transition {inc.state} -> {to}")
    inc.state = to
    for k, v in fields.items():
        setattr(inc, k, v)
    if to == ACTED and inc.acted_at is None:
        inc.acted_at = _utcnow()
    if to == VERIFIED:
        inc.verified_at = _utcnow()
    if to == RESOLVED:
        inc.closed_at = _utcnow()
    return await _save(session, inc)


async def observe(
    session: Any, target: str, severity: str, headline: str, principal: str
) -> Incident | None:
    """Record one observation; open / bump / verify+resolve the target's incident.

    Returns the open incident for the target, or ``None`` when it's healthy.
    """
    inc = await _open_incident(session, target)

    if severity == "ok":
        if inc is None:
            return None
        if inc.state == ACTED:
            await transition(session, inc, VERIFIED, last_severity=severity, last_headline=headline)
            await transition(session, inc, RESOLVED, resolution="recovered after action")
            return None
        await transition(
            session,
            inc,
            RESOLVED,
            last_severity=severity,
            last_headline=headline,
            resolution="recovered without action",
        )
        return None

    if inc is None:
        return await _save(
            session,
            Incident(
                target=target,
                principal=principal,
                state=DETECTED,
                last_severity=severity,
                last_headline=headline,
            ),
        )

    inc.ticks += 1
    inc.last_severity = severity
    inc.last_headline = headline
    # a still-bad target that had already acted rolls back to diagnosed for this tick
    if inc.state == ACTED:
        inc.state = DIAGNOSED
    return await _save(session, inc)


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
        when = i.opened_at.strftime("%H:%M")
        tail = i.resolution or i.last_headline
        parts.append(f"[{i.state}, {i.ticks} ticks, since {when}] {i.last_severity} — {tail}")
    return " | ".join(parts)
