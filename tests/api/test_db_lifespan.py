from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.dependencies import get_db_session
from app.main import app, lifespan


class _FakeDatabase:
    """Duck-typed stand-in for ``Database`` that records engine + session usage."""

    def __init__(self) -> None:
        self.disposed = False
        self.dispose_calls = 0
        self.session_calls = 0

    async def health(self) -> bool:
        return True

    @asynccontextmanager
    async def session(self, *, write: bool = True) -> AsyncGenerator[object]:
        # Mirror Database.session(): hand out a session, never touch the engine.
        self.session_calls += 1
        yield object()

    async def dispose(self) -> None:
        self.disposed = True
        self.dispose_calls += 1


def _count_from_url(monkeypatch, fake: _FakeDatabase) -> dict[str, int]:
    """Patch ``Database.from_url`` to hand out ``fake`` and count constructions."""
    calls = {"count": 0}

    def _factory(cls, url, **kwargs):
        calls["count"] += 1
        return fake

    monkeypatch.setattr(Database, "from_url", classmethod(_factory))
    return calls


async def test_lifespan_creates_single_engine_and_disposes_on_shutdown(monkeypatch):
    fake = _FakeDatabase()
    calls = _count_from_url(monkeypatch, fake)

    async with lifespan(app):
        # One engine, built once, exposed on app.state, still alive.
        assert calls["count"] == 1
        assert app.state.database is fake
        assert fake.disposed is False

    # Disposed exactly once, only at shutdown.
    assert fake.dispose_calls == 1
    assert fake.disposed is True


async def test_engine_is_reused_across_requests(monkeypatch):
    fake = _FakeDatabase()
    calls = _count_from_url(monkeypatch, fake)

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.get("/health/dependencies/database")
            second = await client.get("/health/dependencies/database")

        assert first.status_code == 200
        assert second.status_code == 200
        # The shared engine is built once at startup and reused for every
        # request -- not created per request...
        assert calls["count"] == 1
        # ...and never disposed mid-flight.
        assert fake.disposed is False

    # Disposed exactly once, at shutdown.
    assert fake.dispose_calls == 1


async def test_db_session_dependency_reuses_engine_without_per_request_dispose(monkeypatch):
    # get_db_session is the per-request session dependency behind every bot/auth
    # endpoint -- the primary path whose per-request dispose was removed for #57.
    # Every real endpoint test stubs it out, so drive its actual body through the
    # lifespan via a throwaway route: a re-added dispose fails here loudly instead
    # of silently defeating the shared engine on the whole API surface.
    fake = _FakeDatabase()
    calls = _count_from_url(monkeypatch, fake)

    probe_app = FastAPI()

    @probe_app.get("/__probe")
    async def _probe(session: Annotated[AsyncSession, Depends(get_db_session)]) -> dict[str, bool]:
        return {"ok": True}

    async with lifespan(probe_app):
        transport = ASGITransport(app=probe_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.get("/__probe")
            second = await client.get("/__probe")

        assert first.status_code == 200
        assert second.status_code == 200
        # Each request opened a session off the one shared engine...
        assert fake.session_calls == 2
        assert calls["count"] == 1
        # ...and get_db_session must NEVER dispose the shared engine per request.
        assert fake.dispose_calls == 0

    # Disposed exactly once, at shutdown.
    assert fake.dispose_calls == 1
