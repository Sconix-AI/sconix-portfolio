"""Read-only observation of one deployed app: hit its health endpoints, time them.

    from pilot.probe import probe
    obs = await probe("relnotes", "https://relnotes.example.sslip.io")
    obs.reachable, obs.healthz, obs.readyz, obs.latency_ms

No side effects — this is the half of the agent that is always safe to run.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass
class Probe:
    name: str
    base_url: str
    reachable: bool
    latency_ms: int | None = None
    healthz_status: int | None = None
    healthz_body: Any = None
    readyz_status: int | None = None
    readyz_body: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int, Any]:
    r = await client.get(url)
    try:
        body: Any = r.json()
    except ValueError:
        body = r.text[:400]
    return r.status_code, body


async def probe(name: str, base_url: str, *, timeout: float = 10.0) -> Probe:
    """Fetch ``/healthz`` and ``/readyz`` for one app. Never raises."""
    base = base_url.rstrip("/")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            h_status, h_body = await _get_json(client, f"{base}/healthz")
            r_status, r_body = await _get_json(client, f"{base}/readyz")
        return Probe(
            name=name,
            base_url=base,
            reachable=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            healthz_status=h_status,
            healthz_body=h_body,
            readyz_status=r_status,
            readyz_body=r_body,
        )
    except Exception as exc:  # noqa: BLE001 - a probe that fails is data, not a crash
        return Probe(
            name=name,
            base_url=base,
            reachable=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{exc.__class__.__name__}: {exc}"[:300],
        )
