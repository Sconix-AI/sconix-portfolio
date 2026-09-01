"""Full-chain integration — no live server, no LLM.

Real `sconixcore.ManifestExecutor` → a real drill `sconix.yaml` → a real
subprocess (`python -m pilot.drill`) → verify loop → incident lifecycle →
plan store → `pilot report`. This is the wiring `task demo` exercises, minus
the paid Anthropic calls.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from sconixapp.agent import guarded_tool

from pilot import act, deploy, drill, run
from pilot.audit import record
from pilot.memory import (
    ACTED,
    AWAITING_APPROVAL,
    DIAGNOSED,
    PROPOSED,
    Incident,
    observe,
    transition,
)
from pilot.principal import PILOT, label
from pilot.report import gather, render_text
from pilot.run import _settle, make_guard

_MANIFEST = """\
schema: sconix.dev/project/v1
kind: application
name: Drill
slug: drill
lifecycle: {{status: live}}
commands:
  restart:
    run: ["{py}", -m, pilot.drill, heal, --port, "{port}"]
    risk: external-write
    approval: policy
    verify: {{checks: [healthz, readyz], withinSeconds: 8, attempts: 4, intervalSeconds: 0.3}}
  rollback_plan:
    run: ["{py}", -m, pilot.drill, plan, --port, "{port}"]
    arguments: [release]
    risk: local-write
    approval: never
    verify: {{checks: [exit-zero]}}
  rollback:
    run: ["{py}", -m, pilot.drill, heal, --port, "{port}"]
    arguments: [release, plan_id]
    risk: external-write
    approval: always
    verify: {{checks: [healthz, readyz], withinSeconds: 8, attempts: 4, intervalSeconds: 0.3}}
"""


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    drill._STATE["mode"] = "healthy"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), drill._Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    (tmp_path / "drill.sconix.yaml").write_text(_MANIFEST.format(py=sys.executable, port=port))
    tf = tmp_path / "targets.yaml"
    tf.write_text(
        "targets:\n"
        "  - name: drill\n"
        f"    url: http://127.0.0.1:{port}\n"
        "    manifest: drill.sconix.yaml\n"
        f"    root: {Path(__file__).resolve().parents[1]}\n"
        "    rollback_to: drill-prev\n"
    )
    act.TARGETS_PATH = tf
    act.COOLDOWN_S = 600
    monkeypatch.setenv("SCONIX_STATE_DIR", str(tmp_path / "state"))
    try:
        yield {"port": port, "url": f"http://127.0.0.1:{port}", "state": tmp_path / "state"}
    finally:
        srv.shutdown()
        act.TARGETS_PATH = None
        drill._STATE["mode"] = "healthy"


async def _attempt_restart(session, inc, target: dict) -> str | None:
    """A faithful slice of run.tick's restart step."""
    rr: dict[str, str] = {}
    dec: dict = {}
    guard = make_guard(
        session,
        allow={"drill"},
        run_result=rr,
        principal=PILOT,
        incidents={"drill": inc.id},
        executor=run.EXECUTOR,
        decisions=dec,
    )

    async def _fn(app: str) -> str:
        """restart."""
        res = await run.EXECUTOR.execute("drill", "restart", principal=PILOT, decision=dec.get(app))
        await record(
            session,
            target=app,
            incident_id=inc.id,
            principal=label(PILOT),
            tool="restart",
            decision="done" if res.ok else "failed",
            result=res.output[:500],
        )
        return res.output

    _fn.__name__ = "restart"
    await transition(session, inc, PROPOSED)
    await guarded_tool(_fn, guard=guard).call({"app": "drill"})
    await _settle(session, inc.id, rr.get("drill"), target)
    return rr.get("drill")


async def _open(session) -> Incident:
    inc = await observe(session, "drill", "down", "healthz 503", label(PILOT))
    return await transition(session, inc, DIAGNOSED, diagnosis="wedged", confidence=0.7)


async def test_restart_recovers_and_resolves(session, fleet) -> None:
    drill._poke(fleet["port"], "wedged")
    inc = await _open(session)

    outcome = await _attempt_restart(session, inc, {"name": "drill", "url": fleet["url"]})
    assert outcome == "allow"

    inc = await session.get(Incident, inc.id)
    assert inc.state == "resolved" and inc.resolution == "recovered after action"

    data = await gather(session)
    (row,) = data["incidents"]
    assert [a["decision"] for a in row["actions"]] == ["allow", "done"]
    assert "recovered after action" in render_text(data)


async def test_cooldown_then_escalates(session, fleet) -> None:
    drill._poke(fleet["port"], "wedged")
    inc = await _open(session)
    await _attempt_restart(session, inc, {"name": "drill", "url": fleet["url"]})  # heals + resolves

    drill._poke(fleet["port"], "wedged")  # breaks again, same incident window
    inc2 = await _open(session)
    outcome = await _attempt_restart(session, inc2, {"name": "drill", "url": fleet["url"]})
    assert outcome == "deny"
    inc2 = await session.get(Incident, inc2.id)
    assert inc2.state == "escalated" and "already restarted" in inc2.resolution


async def test_rollback_proposal_through_human_approval(session, fleet) -> None:
    drill._poke(fleet["port"], "down")  # a dependency outage restart won't fix
    inc = await _open(session)
    # simulate: a restart ran this incident and didn't help
    for st in (PROPOSED, "approved", ACTED, "escalated"):
        await transition(session, inc, st)
    assert deploy.should_rollback(inc)

    plan_id = await deploy.propose(
        session,
        run.EXECUTOR,
        target="drill",
        kind="rollback",
        principal=PILOT,
        incident=inc,
        arguments={"release": "drill-prev"},
    )
    assert len(plan_id) == 20
    inc = await session.get(Incident, inc.id)
    assert inc.state == AWAITING_APPROVAL and inc.plan_id == plan_id

    # a human approves (writes the plan-store record `sx approve` would)
    ap = fleet["state"] / "deploy" / "approvals" / f"{plan_id}.json"
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(
        json.dumps(
            {
                "planId": plan_id,
                "outcome": "allow-once",
                "principal": {"kind": "human", "id": "yusuf", "role": "approver"},
                "reason": "confirmed bad release",
                "approvedAt": "2026-09-01T00:00:00+00:00",
            }
        )
    )
    drill._poke(fleet["port"], "healthy")  # the rollback will heal it

    ok = await deploy.resume_if_approved(
        session,
        run.EXECUTOR,
        target="drill",
        incident=inc,
        principal=PILOT,
    )
    assert ok is True
    inc = await session.get(Incident, inc.id)
    assert inc.state == ACTED

    data = await gather(session, target="drill")
    tools = [a["tool"] for a in data["incidents"][0]["actions"]]
    assert "rollback_plan" in tools and "rollback" in tools
    assert all(a["plan_id"] == plan_id for a in data["incidents"][0]["actions"] if a["plan_id"])
