"""The drill server toggles health; probe() must see it, and a per-target
`restart:` command must override the `sx restart` default."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from pilot import act, drill
from pilot.probe import probe


@pytest.fixture()
def server():
    drill._STATE["mode"] = "healthy"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), drill._Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        drill._STATE["mode"] = "healthy"


async def test_probe_follows_drill_state(server) -> None:
    url = f"http://127.0.0.1:{server}"

    ok = await probe("drill", url)
    assert ok.reachable and ok.healthz_status == 200 and ok.readyz_body["status"] == "ok"

    drill._poke(server, "down")
    bad = await probe("drill", url)
    assert bad.reachable and bad.healthz_status == 503

    drill._poke(server, "degraded")
    deg = await probe("drill", url)
    assert deg.healthz_status == 200 and deg.readyz_body["status"] == "degraded"

    drill._poke(server, "heal")
    healed = await probe("drill", url)
    assert healed.healthz_status == 200 and healed.readyz_body["status"] == "ok"


def test_restart_cmd_uses_per_target_override(tmp_path) -> None:
    tf = tmp_path / "t.yaml"
    tf.write_text(
        "targets:\n"
        "  - name: drill\n"
        "    url: http://127.0.0.1:8765\n"
        "    restart: python -m pilot.drill heal --port 8765\n"
        "  - name: relnotes\n"
        "    url: https://x\n"
    )
    act.TARGETS_PATH = tf
    try:
        assert act._restart_cmd("drill") == [
            "python",
            "-m",
            "pilot.drill",
            "heal",
            "--port",
            "8765",
        ]
        assert act._restart_cmd("relnotes") == ["sx", "restart", "relnotes"]  # no override
    finally:
        act.TARGETS_PATH = None
