"""Read-only: what has Pilot seen and done?

    uv run python -m pilot.report                 # everything, newest first
    uv run python -m pilot.report --open          # only unresolved incidents
    uv run python -m pilot.report --target relnotes
    uv run python -m pilot.report --json          # structured, for agents/CI
    uv run python -m pilot.report --db /path/to/pilot.db

Never opens a write transaction; never mutates a row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sconixapp.agent import AgentRun
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel, func, select

from pilot.audit import Action
from pilot.memory import Incident

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "pilot.db"


def _target_of(area: str) -> str:
    return area.split(":", 1)[1] if ":" in area else area


async def gather(
    session: Any, *, target: str | None = None, only_open: bool = False, limit: int = 50
) -> dict[str, Any]:
    q = select(Incident).order_by(Incident.id.desc())
    if target:
        q = q.where(Incident.target == target)
    if only_open:
        q = q.where(Incident.closed_at.is_(None))
    incidents = list((await session.execute(q.limit(limit))).scalars().all())
    ids = [i.id for i in incidents]

    actions: list[Action] = []
    if ids:
        actions = list(
            (
                await session.execute(
                    select(Action).where(Action.incident_id.in_(ids)).order_by(Action.id)
                )
            )
            .scalars()
            .all()
        )
    by_incident: dict[int, list[Action]] = {}
    for a in actions:
        by_incident.setdefault(a.incident_id, []).append(a)

    runs = list((await session.execute(select(AgentRun))).scalars().all())
    total_cost = round(sum(r.cost_usd for r in runs), 6)
    cost_by_target: dict[str, float] = {}
    for r in runs:
        t = _target_of(r.area)
        cost_by_target[t] = round(cost_by_target.get(t, 0.0) + r.cost_usd, 6)
    total_runs = int(
        (await session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
    )

    return {
        "incidents": [
            {
                "id": i.id,
                "target": i.target,
                "state": i.state,
                "open": i.open,
                "principal": i.principal,
                "opened_at": i.opened_at.isoformat(),
                "closed_at": i.closed_at.isoformat() if i.closed_at else None,
                "ticks": i.ticks,
                "severity": i.last_severity,
                "confidence": i.confidence,
                "diagnosis": i.diagnosis,
                "plan_kind": i.plan_kind,
                "plan_id": i.plan_id,
                "resolution": i.resolution,
                "actions": [
                    {
                        "principal": a.principal,
                        "decision": a.decision,
                        "tool": a.tool,
                        "plan_id": a.plan_id,
                        "reason": a.reason,
                        "at": a.created_at.isoformat(),
                    }
                    for a in by_incident.get(i.id, [])
                ],
            }
            for i in incidents
        ],
        "summary": {
            "incidents": len(incidents),
            "open": sum(1 for i in incidents if i.open),
            "resolved": sum(1 for i in incidents if i.state == "resolved"),
            "escalated": sum(1 for i in incidents if i.state == "escalated"),
            "actions": len(actions),
            "agent_runs": total_runs,
            "agent_cost_usd": total_cost,
            "agent_cost_by_target": cost_by_target,
        },
    }


def render_text(data: dict[str, Any]) -> str:
    out: list[str] = []
    for i in data["incidents"]:
        head = f"#{i['id']}  {i['target']:<12} {i['state']:<16} {i['principal']}"
        out.append(head)
        span = i["opened_at"][11:19]
        if i["closed_at"]:
            span += f" → {i['closed_at'][11:19]}"
        conf = f", conf {i['confidence']:.2f}" if i["confidence"] is not None else ""
        out.append(f"    {span}  ·  {i['ticks']} ticks  ·  last {i['severity']}{conf}")
        if i["diagnosis"]:
            out.append(f"    diagnosis:  {i['diagnosis']}")
        if i["plan_id"]:
            out.append(f"    plan:       {i['plan_kind']} {i['plan_id']}")
        if i["resolution"]:
            out.append(f"    outcome:    {i['resolution']}")
        for a in i["actions"]:
            pid = f" [{a['plan_id']}]" if a["plan_id"] else ""
            out.append(f"      {a['decision']:<9} {a['tool']}{pid}  {a['reason']}")
        out.append("")

    s = data["summary"]
    out.append(
        f"{s['incidents']} incident(s): {s['open']} open, {s['resolved']} resolved, "
        f"{s['escalated']} escalated  ·  {s['actions']} actions  ·  "
        f"{s['agent_runs']} agent runs, ${s['agent_cost_usd']:.4f}"
    )
    if s["agent_cost_by_target"]:
        by = "  ".join(f"{k} ${v:.4f}" for k, v in sorted(s["agent_cost_by_target"].items()))
        out.append(f"  cost by target: {by}")
    return "\n".join(out)


async def _run(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser()
    if not db.exists():
        print(f"no pilot.db at {db} — nothing to report")
        return 0
    init_engine(f"sqlite+aiosqlite:///{db}")
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)  # tolerate an old schema
    session = await get_session().__anext__()
    try:
        data = await gather(session, target=args.target, only_open=args.open, limit=args.limit)
    finally:
        await session.rollback()  # read-only: nothing to commit
        await dispose_engine()
    print(json.dumps(data, indent=2) if args.json else render_text(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pilot.report", description="read-only pilot history")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--target")
    p.add_argument("--open", action="store_true", help="only unresolved incidents")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
