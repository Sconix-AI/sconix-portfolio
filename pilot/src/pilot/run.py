"""The loop: probe every target, assess it (with memory), and — with --fix — act.

    uv run python -m pilot.run                      # one pass over every target
    uv run python -m pilot.run relnotes             # one pass, just one
    uv run python -m pilot.run --fix                # arm restart_app (policy-gated)
    uv run python -m pilot.run watch --every 60     # run unattended on a loop
    uv run python -m pilot.run watch --fix --for 900

Slice 3: `watch` runs the loop unattended, and `pilot.memory` gives it an
incident record — an app that's been down for six ticks reads differently from
one that just blipped, and the restart gate now has a cooldown so a wedged app
isn't restart-looped.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from sconixapp.agent import NAV, WORKER, guarded_tool, run_agent
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel

from pilot.act import make_guard, restart_app
from pilot.audit import record
from pilot.memory import history, observe, summarize
from pilot.probe import Probe, probe

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ROOT / "targets.yaml"
DB_URL = f"sqlite+aiosqlite:///{ROOT / 'pilot.db'}"
PRINCIPAL = "system:pilot"  # no real user — an ops agent has none (backlog: NOTES #3)

_ASSESS_SYSTEM = """You are a release pilot watching a small fleet of deployed web apps.
You are given one app's raw health probe and its recent incident history.
Classify its current state.

Return ONLY a JSON object — no prose, no code fence:
{
  "severity": "ok" | "warn" | "down",
  "headline": "<= 8 words, plain",
  "detail": "one or two sentences: what the probe shows, factoring in the history"
}

- "ok": both endpoints 200 and readyz reports every check ok.
- "warn": reachable but slow (latency over 2000 ms), or readyz degraded, or a
  flapping target that is up this tick but has repeated recent incidents.
- "down": unreachable, or a non-200 on healthz."""

_FIX_SYSTEM = """You are a release pilot. One app you watch is unhealthy. You have
ONE tool: restart_app(app). An in-place restart clears transient failures (stuck
worker, leaked connection, wedged process) but does nothing for a bad deploy, a
dependency outage, or a config error.

Given the probe, the assessment, and the incident history: if a restart is a
reasonable first move AND it hasn't already been tried this incident, call
restart_app exactly once, then stop. Otherwise do not call it — say why in one
sentence. The policy gate may return BLOCKED (e.g. cooldown); if so, report it
and stop."""


def load_targets() -> list[dict[str, str]]:
    return list(yaml.safe_load(TARGETS.read_text())["targets"])


def _parse(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1].removeprefix("json").strip()
    return json.loads(t)


async def assess(
    client: Any, session: Any, obs: Probe, context: str
) -> tuple[dict[str, Any], float]:
    payload = {"probe": obs.as_dict(), "recent_incidents": context}
    result = await run_agent(
        client=client,
        session=session,
        user_id=PRINCIPAL,
        area=f"pilot.assess:{obs.name}",
        model=NAV,
        system=_ASSESS_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        tools=[],
        max_tokens=600,
    )
    return _parse(result.text), result.run.cost_usd


async def fix(client: Any, session: Any, obs: Probe, verdict: dict, context: str, tool: Any) -> str:
    payload = {"probe": obs.as_dict(), "assessment": verdict, "recent_incidents": context}
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


async def tick(client: Any, session: Any, targets: list[dict], do_fix: bool) -> list[dict]:
    """One full pass. Returns a row per target: name, severity, latency, headline, acted."""
    probes = await asyncio.gather(*(probe(t["name"], t["url"]) for t in targets))

    assessed: list[tuple[Probe, dict, float]] = []
    for obs in probes:
        ctx = summarize(await history(session, obs.name))
        verdict, cost = await assess(client, session, obs, ctx)
        await observe(session, obs.name, verdict["severity"], verdict["headline"])
        assessed.append((obs, verdict, cost))

    fixable = {o.name for o, v, _ in assessed if v["severity"] in ("warn", "down")}
    acted: dict[str, str] = {}
    if do_fix and fixable:
        guard = make_guard(session, allow=fixable, run_result={})
        tool = guarded_tool(restart_app, guard=guard)
        for obs, verdict, _ in assessed:
            if obs.name not in fixable:
                continue
            ctx = summarize(await history(session, obs.name))
            report = await fix(client, session, obs, verdict, ctx, tool)
            await record(
                session, target=obs.name, tool="restart_app", decision="done", result=report[:500]
            )
            acted[obs.name] = report

    await session.commit()
    return [
        {
            "name": o.name,
            "severity": v["severity"],
            "latency_ms": o.latency_ms,
            "headline": v["headline"],
            "detail": v["detail"],
            "cost": c,
            "acted": acted.get(o.name, ""),
        }
        for o, v, c in assessed
    ]


async def _client() -> Any:
    import anthropic

    from pilot.secrets import load

    return anthropic.AsyncAnthropic(api_key=load("ANTHROPIC_API_KEY"))


async def _open_session() -> Any:
    init_engine(DB_URL)
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return await get_session().__anext__()


def _print_pass(rows: list[dict], do_fix: bool) -> float:
    total = 0.0
    print(f"\n  {'app':<14}{'sev':<6}{'lat':>7}  headline")
    print("  " + "-" * 62)
    for r in rows:
        total += r["cost"]
        lat = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "-"
        print(f"  {r['name']:<14}{r['severity']:<6}{lat:>7}  {r['headline']}")
        print(f"  {'':<27}{r['detail']}")
        if r["acted"]:
            print(f"  {'':<27}↳ acted: {r['acted'][:120]}")
    print("  " + "-" * 62)
    print(f"  {len(rows)} target(s)   run cost ${total:.4f}\n")
    return total


async def once(only: str | None, do_fix: bool) -> int:
    targets = [t for t in load_targets() if only in (None, t["name"])]
    if not targets:
        print(f"no target named {only!r}")
        return 2
    client = await _client()
    session = await _open_session()
    try:
        rows = await tick(client, session, targets, do_fix)
    finally:
        await session.rollback()
        await dispose_engine()
    _print_pass(rows, do_fix)
    return 0


async def watch(do_fix: bool, every: int, for_s: int | None) -> int:
    targets = load_targets()
    client = await _client()
    session = await _open_session()
    started = time.monotonic()
    n = 0
    try:
        while True:
            n += 1
            rows = await tick(client, session, targets, do_fix)
            stamp = time.strftime("%H:%M:%S")
            worst = max(rows, key=lambda r: ("ok", "warn", "down").index(r["severity"]))
            flags = " ".join(
                f"{r['name']}={r['severity']}" + ("*" if r["acted"] else "") for r in rows
            )
            print(f"  {stamp}  tick {n:>3}  worst={worst['severity']:<4}  {flags}")
            if for_s is not None and time.monotonic() - started >= for_s:
                break
            await asyncio.sleep(every)
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        await session.rollback()
        await dispose_engine()
    print(f"  {n} tick(s) over {int(time.monotonic() - started)}s")
    return 0


def main(argv: list[str]) -> int:
    do_fix = "--fix" in argv
    rest = [a for a in argv if a not in ("--fix",)]

    def opt(name: str, default: int) -> int:
        if name in rest:
            return int(rest[rest.index(name) + 1])
        return default

    positional = [a for a in rest if not a.startswith("-") and not a.isdigit()]
    if positional and positional[0] == "watch":
        every = opt("--every", 60)
        for_s = opt("--for", 0) or None
        return asyncio.run(watch(do_fix, every, for_s))
    return asyncio.run(once(positional[0] if positional else None, do_fix))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
