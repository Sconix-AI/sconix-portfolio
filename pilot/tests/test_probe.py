"""No-network tests: probe parsing + the agent assess wiring via a fake client."""

from __future__ import annotations

import httpx
import pytest
from sconixapp.db import dispose_engine, get_engine, get_session, init_engine
from sqlmodel import SQLModel

from pilot.probe import probe
from pilot.run import assess


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = 0


class _Msg:
    stop_reason = "end_turn"

    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage(120, 40)


class _Messages:
    def __init__(self, text: str) -> None:
        self._text = text

    async def create(self, **_: object) -> _Msg:
        return _Msg(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _Messages(text)
        self.beta = type("_B", (), {"messages": self.messages})()


@pytest.fixture()
async def session():
    init_engine("sqlite+aiosqlite:///:memory:")
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    agen = get_session()
    s = await agen.__anext__()
    try:
        yield s
    finally:
        await s.rollback()
        await dispose_engine()


async def test_probe_reads_both_endpoints() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"status": "ok", "checks": {"db": "ok"}})

    transport = httpx.MockTransport(handler)
    # probe() builds its own client; monkeypatch via a thin subclass is overkill,
    # so just exercise the real thing against a local ASGI-less mock server below.
    obs = await _probe_with_transport("relnotes", "https://x.test", transport)
    assert obs.reachable and obs.healthz_status == 200
    assert obs.readyz_body["checks"]["db"] == "ok"
    assert obs.latency_ms is not None


async def test_probe_unreachable_is_data_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    obs = await _probe_with_transport("down", "https://x.test", httpx.MockTransport(handler))
    assert obs.reachable is False
    assert obs.error and "ConnectError" in obs.error


async def test_assess_records_an_agent_run(session) -> None:
    obs = await _probe_with_transport(
        "relnotes",
        "https://x.test",
        httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})),
    )
    client = _FakeClient('{"severity":"ok","headline":"all green","detail":"both 200."}')
    verdict, cost = await assess(client, session, obs, "no prior incidents for this target")
    assert verdict["severity"] == "ok"
    assert cost >= 0.0


# --- helper: run probe() against a mock transport -----------------------------
async def _probe_with_transport(name, url, transport):
    import pilot.probe as mod

    orig = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = transport
        return orig(*a, **kw)

    mod.httpx.AsyncClient = factory
    try:
        return await probe(name, url)
    finally:
        mod.httpx.AsyncClient = orig
