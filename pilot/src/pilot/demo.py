"""End-to-end drill: watch a fake app, break it, watch the agent react — and
watch the gate stop a restart loop. Runs the real `tick()` against a local
`pilot.drill` server; the only side effect is healing that server.

    uv run python -m pilot.demo

Costs a few cents (haiku assess + sonnet fix, 4 ticks)."""

from __future__ import annotations

import asyncio
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
    tf = Path(tempfile.mkstemp(suffix=".yaml")[1])
    tf.write_text(
        "targets:\n"
        "  - name: drill\n"
        f"    url: http://127.0.0.1:{port}\n"
        f"    restart: python -m pilot.drill heal --port {port}\n"
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

    print(f"\n  drill app on :{port}\n  {'-' * 66}")
    try:
        for i, (toggle, note) in enumerate(SCRIPT, 1):
            if toggle:
                drill._poke(port, toggle)
            rows = await tick(client, session, targets, do_fix=True)
            r = rows[0]
            mark = "  ← RESTARTED" if r["restarted"] else ""
            print(f"  tick {i}  drill={r['severity']:<4}{mark}  {note}")
            print(f"          headline: {r['headline']}")
            if r["said"]:
                print(f"          pilot:    {r['said'].splitlines()[0][:90]}")
        print(f"  {'-' * 66}")

        incs = (await session.execute(select(Incident).order_by(Incident.id))).scalars().all()
        acts = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
        print("\n  incidents")
        for x in incs:
            state = f"open ({x.ticks} ticks)" if x.open else "resolved"
            print(f"    #{x.id} drill  {state:<16} last: {x.last_severity} — {x.last_headline}")
        print("\n  actions (the audit trail)")
        for a in acts:
            print(f"    {a.decision:<8} {a.reason or a.result.splitlines()[0][:70]}")
        print()
    finally:
        await session.rollback()
        await dispose_engine()
        srv.shutdown()
        tf.unlink(missing_ok=True)
        db.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
