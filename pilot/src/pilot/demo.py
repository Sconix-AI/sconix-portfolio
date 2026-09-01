"""End-to-end drill: watch a fake app, break it, watch the agent react — and
watch the gate stop a restart loop. Runs the real `tick()` against a local
`pilot.drill` server; the only side effect is healing that server.

    uv run python -m pilot.demo

Costs a few cents (haiku assess + sonnet fix, 4 ticks)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel, select

from pilot import act, drill
from pilot.audit import Action
from pilot.memory import Incident
from pilot.run import _client, load_targets, tick

_DRILL_MANIFEST = """\
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
    idempotent: true
    verify: {{checks: [healthz, readyz], withinSeconds: 8, attempts: 4, intervalSeconds: 0.5}}
"""


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    drill._STATE["mode"] = "healthy"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), drill._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


SCRIPT = [
    ("healthy", "baseline — drill is fine"),
    ("wedged", "wedge it: /healthz 503s but deps are ok → a restart should clear it"),
    (None, "recovered — the restart worked, incident closes"),
    ("wedged", "wedge it again, inside the 10-min cooldown → gate must refuse a 2nd restart"),
]


async def main() -> int:
    srv, port = _start_server()
    tmp = Path(tempfile.mkdtemp(prefix="pilot-demo-"))
    (tmp / "drill.sconix.yaml").write_text(_DRILL_MANIFEST.format(py=sys.executable, port=port))
    tf = tmp / "targets.yaml"
    tf.write_text(
        "targets:\n"
        "  - name: drill\n"
        f"    url: http://127.0.0.1:{port}\n"
        "    manifest: drill.sconix.yaml\n"
        f"    root: {Path(__file__).resolve().parents[2]}\n"
    )
    act.TARGETS_PATH = tf
    act.COOLDOWN_S = 600

    db = Path(tempfile.mkstemp(suffix=".db")[1])
    init_engine(f"sqlite+aiosqlite:///{db}")
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    session = await get_session().__anext__()
    client = await _client()
    targets = load_targets(tf)

    print(f"\n  drill app on :{port}\n  {'-' * 70}")
    try:
        for i, (toggle, note) in enumerate(SCRIPT, 1):
            if toggle:
                drill._poke(port, toggle)
            rows = await tick(client, session, targets, do_fix=True)
            r = rows[0]
            mark = "  ← RESTARTED" if r["restarted"] else ""
            print(f"  tick {i}  drill={r['severity']:<4} [{r['state']}]{mark}  {note}")
            print(f"          headline: {r['headline']}")
            if r["said"]:
                print(f"          pilot:    {r['said'].splitlines()[0][:90]}")
        print(f"  {'-' * 70}")

        incs = (await session.execute(select(Incident).order_by(Incident.id))).scalars().all()
        acts = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
        print("\n  incidents  (target · state · principal · resolution)")
        for x in incs:
            print(
                f"    #{x.id} {x.target} · {x.state} · {x.principal} · "
                f"{x.resolution or x.last_headline}  ({x.ticks} ticks)"
            )
        print("\n  actions  (the audit trail — principal · decision · why)")
        for a in acts:
            why = a.reason or a.result.splitlines()[0][:64]
            print(f"    {a.principal} · {a.decision:<8} · inc#{a.incident_id} · {why}")
        print()
    finally:
        await session.rollback()
        await dispose_engine()
        srv.shutdown()
        db.unlink(missing_ok=True)
        for p in (tf, tmp / "drill.sconix.yaml"):
            p.unlink(missing_ok=True)
        tmp.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
