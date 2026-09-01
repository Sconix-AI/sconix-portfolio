"""A throwaway app that lies about its health on command — so the down→act path
can be exercised without touching a real deployment.

    python -m pilot.drill serve --port 8765      # run the fake app
    python -m pilot.drill down  --port 8765      # make /healthz 503
    python -m pilot.drill degraded --port 8765   # make /readyz report a bad check
    python -m pilot.drill heal  --port 8765      # back to healthy  (this is "restart")

It speaks the same `/healthz` + `/readyz` shape as `sconixapp.health`, so `probe()`
can't tell it apart from a real target.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_STATE = {"mode": "healthy"}  # healthy | degraded | down


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # keep the demo output clean
        pass

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        mode = _STATE["mode"]
        if self.path == "/healthz":
            # wedged: process stuck but deps fine -> a restart is the right first move.
            # down:   dependency outage -> a restart won't help.
            if mode in ("down", "wedged"):
                self._send(503, {"status": "error", "drill": mode})
            else:
                self._send(200, {"status": "ok", "drill": mode})
        elif self.path == "/readyz":
            if mode == "down":
                self._send(503, {"status": "error", "checks": {"db": "error: drill outage"}})
            elif mode == "degraded":
                self._send(
                    200, {"status": "degraded", "checks": {"db": "ok", "redis": "error: drill"}}
                )
            elif mode == "wedged":
                # deps are reachable; the app itself isn't answering its own probe
                self._send(200, {"status": "ok", "checks": {"db": "ok", "redis": "ok"}})
            else:
                self._send(200, {"status": "ok", "checks": {"db": "ok", "redis": "ok"}})
        elif self.path.startswith("/_drill/"):
            new = self.path.rsplit("/", 1)[-1]
            _STATE["mode"] = "healthy" if new == "heal" else new
            self._send(200, {"mode": _STATE["mode"]})
        else:
            self._send(404, {"error": "not found"})


def _serve(port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"drill app on http://127.0.0.1:{port} (mode={_STATE['mode']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


def _poke(port: int, mode: str) -> None:
    url = f"http://127.0.0.1:{port}/_drill/{mode}"
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - localhost only
        print(json.load(r))


def _fake_plan_id(port: int) -> str:
    import hashlib

    return hashlib.sha256(f"drill-plan:{port}".encode()).hexdigest()[:20]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot.drill")
    p.add_argument(
        "action",
        choices=["serve", "down", "wedged", "degraded", "heal", "healthy", "plan"],
    )
    p.add_argument("--port", type=int, default=8765)
    ns = p.parse_args(argv)
    if ns.action == "serve":
        _serve(ns.port)
    elif ns.action == "plan":
        print(f"created drill plan {_fake_plan_id(ns.port)}")  # stands in for `sx ... --plan`
    else:
        _poke(ns.port, ns.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
