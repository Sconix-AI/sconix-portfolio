"""The loop: probe every target, assess it (with memory), and — with --fix — act.

    uv run python -m pilot.run                      # one pass over every target
    uv run python -m pilot.run relnotes             # one pass, just one
    uv run python -m pilot.run --fix                # arm restart_app (policy-gated)
    uv run python -m pilot.run watch --every 60     # run unattended on a loop
    uv run python -m pilot.run watch --fix --for 900
    uv run python -m pilot.run watch --targets drill.targets.yaml --fix   # drill

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

from pilot import act
from pilot.act import make_guard, restart_app
from pilot.audit import record
from pilot.memory import (
    ACTED,
    APPROVED,
    DIAGNOSED,
    ESCALATED,
    PROPOSED,
    RESOLVED,
    VERIFIED,
    history,
    observe,
    summarize,
    transition,
)
from pilot.principal import PILOT, Principal
from pilot.probe import Probe, probe

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ROOT / "targets.yaml"
DB_URL = f"sqlite+aiosqlite:///{ROOT / 'pilot.db'}"
PRINCIPAL = str(PILOT)  # run_agent still wants a user_id string; see PILOT_REQUIREMENTS.md

_ASSESS_SYSTEM = """You are a release pilot watching a small fleet of deployed web apps.
You are given one app's raw health probe and its recent incident history.
Classify its current state.

Return ONLY a JSON object — no prose, no code fence:
{
  "severity": "ok" | "warn" | "down",
  "headline": "<= 8 words, plain",
  "detail": "one or two sentences: what the probe shows, factoring in the history",
  "confidence": 0.0 - 1.0
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


def load_targets(path: Path) -> list[dict[str, str]]:
    return list(yaml.safe_load(path.read_text())["targets"])


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


async def tick(
    client: Any,
    session: Any,
    targets: list[dict],
    do_fix: bool,
    principal: Principal = PILOT,
) -> list[dict]:
    """One full pass: probe → assess → move each incident through its lifecycle."""
    who = str(principal)
    probes = await asyncio.gather(*(probe(t["name"], t["url"]) for t in targets))
    by_name = {t["name"]: t for t in targets}

    assessed: list[tuple[Probe, dict, float]] = []
    incidents: dict[str, int] = {}  # target -> open incident id
    for obs in probes:
        ctx = summarize(await history(session, obs.name))
        verdict, cost = await assess(client, session, obs, ctx)
        inc = await observe(session, obs.name, verdict["severity"], verdict["headline"], who)
        if inc is not None:
            await transition(
                session,
                inc,
                DIAGNOSED,
                diagnosis=verdict.get("detail", ""),
                confidence=verdict.get("confidence"),
            )
            incidents[obs.name] = inc.id
        assessed.append((obs, verdict, cost))

    fixable = {o.name for o, v, _ in assessed if v["severity"] in ("warn", "down")}
    said: dict[str, str] = {}
    restarted: set[str] = set()
    if do_fix and fixable:
        run_result: dict[str, str] = {}

        async def _restart_and_record(app: str) -> str:
            out = await restart_app(app)
            await record(
                session,
                target=app,
                incident_id=incidents.get(app),
                principal=who,
                tool="restart_app",
                decision="failed" if out.startswith("restart failed") else "done",
                result=out[:500],
            )
            return out

        _restart_and_record.__name__ = "restart_app"
        _restart_and_record.__doc__ = restart_app.__doc__

        guard = make_guard(
            session,
            allow=fixable,
            run_result=run_result,
            principal=who,
            incidents=incidents,
        )
        tool = guarded_tool(_restart_and_record, guard=guard)
        for obs, verdict, _ in assessed:
            if obs.name not in fixable or not principal.may_touch(obs.name):
                continue
            inc = await _reload(session, incidents[obs.name])
            await transition(session, inc, PROPOSED)
            fctx = summarize(await history(session, obs.name))
            said[obs.name] = await fix(client, session, obs, verdict, fctx, tool)
            await _settle(session, incidents[obs.name], run_result.get(obs.name), by_name[obs.name])
        restarted = {n for n, r in run_result.items() if r == "allowed"}

    await session.commit()
    out_rows = []
    for o, v, c in assessed:
        inc = await _open_by_target(session, o.name)
        out_rows.append(
            {
                "name": o.name,
                "severity": v["severity"],
                "latency_ms": o.latency_ms,
                "headline": v["headline"],
                "detail": v["detail"],
                "confidence": v.get("confidence"),
                "cost": c,
                "said": said.get(o.name, ""),
                "restarted": o.name in restarted,
                "state": inc.state if inc else RESOLVED,
            }
        )
    return out_rows


async def _reload(session: Any, incident_id: int) -> Any:
    from pilot.memory import Incident

    return await session.get(Incident, incident_id)


async def _open_by_target(session: Any, target: str) -> Any:
    from sqlmodel import select

    from pilot.memory import Incident

    return (
        (
            await session.execute(
                select(Incident)
                .where(Incident.target == target)
                .order_by(Incident.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _settle(session: Any, incident_id: int, outcome: str | None, target: dict) -> None:
    """After the fix agent ran: move the incident to acted / escalated, then
    verify recovery with one confirming probe."""
    inc = await _reload(session, incident_id)
    if inc is None or inc.state in (RESOLVED, VERIFIED):
        return

    if outcome == "allowed":
        await transition(session, inc, APPROVED)
        await transition(session, inc, ACTED)
        confirm = await probe(target["name"], target["url"])
        healthy = confirm.healthz_status == 200 and (
            not confirm.readyz_body or confirm.readyz_body.get("status") == "ok"
        )
        if healthy:
            await transition(session, inc, VERIFIED, last_severity="ok")
            await transition(session, inc, RESOLVED, resolution="recovered after action")
        # else: stays ACTED; next tick's observe() rolls it back to DIAGNOSED
    elif outcome and outcome.startswith("denied"):
        await transition(session, inc, ESCALATED, resolution=outcome)
    else:
        await transition(
            session,
            inc,
            ESCALATED,
            resolution="agent proposed no safe action",
        )


async def _client() -> Any:
    import anthropic

    from pilot.secrets import load

    return anthropic.AsyncAnthropic(api_key=load("ANTHROPIC_API_KEY"))


async def _open_session() -> Any:
    init_engine(DB_URL)
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return await get_session().__anext__()


def _print_pass(rows: list[dict]) -> float:
    total = 0.0
    print(f"\n  {'app':<14}{'sev':<6}{'lat':>7}  {'state':<10}headline")
    print("  " + "-" * 66)
    for r in rows:
        total += r["cost"]
        lat = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "-"
        print(f"  {r['name']:<14}{r['severity']:<6}{lat:>7}  {r['state']:<10}{r['headline']}")
        print(f"  {'':<29}{r['detail']}")
        if r["said"]:
            verb = "restarted" if r["restarted"] else "pilot"
            print(f"  {'':<29}↳ {verb}: {r['said'][:120]}")
    print("  " + "-" * 66)
    print(f"  {len(rows)} target(s)   run cost ${total:.4f}\n")
    return total


async def once(only: str | None, do_fix: bool, targets_path: Path) -> int:
    act.TARGETS_PATH = targets_path
    targets = [t for t in load_targets(targets_path) if only in (None, t["name"])]
    if not targets:
        print(f"no target named {only!r} in {targets_path}")
        return 2
    client = await _client()
    session = await _open_session()
    try:
        rows = await tick(client, session, targets, do_fix)
    finally:
        await session.rollback()
        await dispose_engine()
    _print_pass(rows)
    return 0


async def watch(do_fix: bool, every: int, for_s: int | None, targets_path: Path) -> int:
    act.TARGETS_PATH = targets_path
    targets = load_targets(targets_path)
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
                f"{r['name']}={r['severity']}" + ("*restarted" if r["restarted"] else "")
                for r in rows
            )
            print(f"  {stamp}  tick {n:>3}  worst={worst['severity']:<4}  {flags}")
            for r in rows:
                if r["state"] not in ("resolved",) and (r["said"] or r["severity"] != "ok"):
                    tag = "restarted" if r["restarted"] else r["state"]
                    print(f"           └ {r['name']} [{tag}]: {(r['said'] or r['detail'])[:100]}")
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
    rest = [a for a in argv if a != "--fix"]

    def opt(name: str, default: str) -> str:
        return rest[rest.index(name) + 1] if name in rest else default

    targets_path = Path(opt("--targets", str(TARGETS)))
    if not targets_path.is_absolute():
        targets_path = ROOT / targets_path

    flag_values = {opt("--every", ""), opt("--for", ""), opt("--targets", "")}
    positional = [a for a in rest if not a.startswith("-") and a not in flag_values]

    if positional and positional[0] == "watch":
        every = int(opt("--every", "60"))
        for_s = int(opt("--for", "0")) or None
        return asyncio.run(watch(do_fix, every, for_s, targets_path))
    only = positional[0] if positional else None
    return asyncio.run(once(only, do_fix, targets_path))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
