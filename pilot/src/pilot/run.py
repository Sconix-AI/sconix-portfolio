"""The loop: probe every target, assess it, and — with --fix — let the agent act.

    uv run python -m pilot.run                 # assess every target
    uv run python -m pilot.run relnotes        # just one
    uv run python -m pilot.run --fix           # arm the restart tool (policy-gated)

Slice 2: the agent now has one mutating tool (`restart_app`). It never fires
unguarded — `sconixapp.agent.guarded_tool` runs the policy gate first, and every
proposal / allow / deny / result lands in `pilot.db`'s `actions` table.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from sconixapp.agent import NAV, WORKER, guarded_tool, run_agent
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel, select

from pilot.act import make_guard, restart_app
from pilot.audit import Action, record
from pilot.probe import Probe, probe

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ROOT / "targets.yaml"
DB_URL = f"sqlite+aiosqlite:///{ROOT / 'pilot.db'}"
PRINCIPAL = "system:pilot"  # no real user — an ops agent has none

_ASSESS_SYSTEM = """You are a release pilot watching a small fleet of deployed web apps.
You are given one app's raw health probe. Classify its state.

Return ONLY a JSON object — no prose, no code fence:
{
  "severity": "ok" | "warn" | "down",
  "headline": "<= 8 words, plain",
  "detail": "one or two sentences: what the probe shows and the likely cause"
}

- "ok": both endpoints 200 and readyz reports every check ok.
- "warn": reachable but slow (latency over 2000 ms), or readyz degraded.
- "down": unreachable, or a non-200 on healthz."""

_FIX_SYSTEM = """You are a release pilot. One app you watch is unhealthy. You have
ONE tool: restart_app(app). An in-place restart clears transient failures (stuck
worker, leaked connection, wedged process) but does nothing for a bad deploy, a
dependency outage, or a config error.

Given the probe + prior assessment: if a restart is a reasonable first move, call
restart_app exactly once for this app, then stop. If a restart clearly won't help,
do not call it — say so in one sentence. A policy gate may still block the call;
if it returns BLOCKED, report that and stop."""


def load_targets() -> list[dict[str, str]]:
    return list(yaml.safe_load(TARGETS.read_text())["targets"])


def _parse(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1].removeprefix("json").strip()
    return json.loads(t)


async def assess(client: Any, session: Any, obs: Probe) -> tuple[dict[str, Any], float]:
    result = await run_agent(
        client=client,
        session=session,
        user_id=PRINCIPAL,
        area=f"pilot.assess:{obs.name}",
        model=NAV,
        system=_ASSESS_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(obs.as_dict(), default=str)}],
        tools=[],
        max_tokens=600,
    )
    return _parse(result.text), result.run.cost_usd


async def fix(client: Any, session: Any, obs: Probe, verdict: dict[str, Any], tool: Any) -> str:
    payload = {"probe": obs.as_dict(), "assessment": verdict}
    result = await run_agent(
        client=client,
        session=session,
        user_id=PRINCIPAL,
        area=f"pilot.fix:{obs.name}",
        model=WORKER,
        system=_FIX_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        tools=[tool],
        max_tokens=1500,
    )
    return result.text.strip()


async def main(only: str | None, do_fix: bool) -> int:
    targets = [t for t in load_targets() if only in (None, t["name"])]
    if not targets:
        print(f"no target named {only!r} in {TARGETS}")
        return 2

    try:
        import anthropic
    except ModuleNotFoundError:
        print("uv add anthropic (sconixapp[agent]) first")
        return 2
    from pilot.secrets import load

    client = anthropic.AsyncAnthropic(api_key=load("ANTHROPIC_API_KEY"))

    init_engine(DB_URL)
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    agen = get_session()
    session = await agen.__anext__()

    assessed: list[tuple[Probe, dict[str, Any], float]] = []
    fixes: list[tuple[str, str]] = []
    try:
        probes = await asyncio.gather(*(probe(t["name"], t["url"]) for t in targets))
        for obs in probes:
            verdict, spent = await assess(client, session, obs)
            assessed.append((obs, verdict, spent))

        fixable = {o.name for o, v, _ in assessed if v["severity"] in ("warn", "down")}
        if do_fix and fixable:
            guard = make_guard(session, allow=fixable, run_result={})
            tool = guarded_tool(restart_app, guard=guard)
            for obs, verdict, _ in assessed:
                if obs.name not in fixable:
                    continue
                report = await fix(client, session, obs, verdict, tool)
                await record(
                    session, target=obs.name, tool="restart_app",
                    decision="done", result=report[:500],
                )
                fixes.append((obs.name, report))
        await session.commit()

        recent = (
            await session.execute(select(Action).order_by(Action.id.desc()).limit(12))
        ).scalars().all()
    finally:
        await session.rollback()
        await dispose_engine()

    total = 0.0
    print(f"\n  {'app':<14}{'sev':<6}{'lat':>7}  headline")
    print("  " + "-" * 62)
    for obs, v, spent in assessed:
        total += spent
        lat = f"{obs.latency_ms}ms" if obs.latency_ms is not None else "-"
        print(f"  {obs.name:<14}{v['severity']:<6}{lat:>7}  {v['headline']}")
        print(f"  {'':<27}{v['detail']}")
    print("  " + "-" * 62)

    if do_fix:
        print("\n  actions")
        print("  " + "-" * 62)
        if not recent:
            print("  (none — fleet healthy, nothing to fix)")
        for a in reversed(recent):
            print(f"  {a.target:<14}{a.decision:<9}{a.reason or a.result[:44]}")
        print("  " + "-" * 62)

    print(f"\n  {len(assessed)} target(s)   run cost ${total:.4f}\n")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    fix_flag = "--fix" in args
    positional = [a for a in args if not a.startswith("-")]
    raise SystemExit(asyncio.run(main(positional[0] if positional else None, fix_flag)))
