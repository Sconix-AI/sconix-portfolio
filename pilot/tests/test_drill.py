"""The drill server toggles health; probe() must see it, and the retry-aware
verify loop must give up on a target that stays broken."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from pilot import drill
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


async def test_verify_recovered_retries_then_gives_up(server) -> None:
    from sconixcore import Verification

    from pilot.run import _verify_recovered

    target = {"name": "drill", "url": f"http://127.0.0.1:{server}"}
    fast = Verification(checks=("healthz",), within_seconds=5, attempts=3, interval_seconds=0.01)

    drill._poke(server, "healthy")
    assert await _verify_recovered(target, fast) is True

    drill._poke(server, "wedged")  # healthz 503 forever
    assert await _verify_recovered(target, fast) is False
