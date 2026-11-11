from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.interfaces import IsolationLevel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


@dataclass(frozen=True)
class Database:
    """Async database abstraction providing engine, session handling, and health checks."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> Database:
        normalized_url, engine_options = _normalize_asyncpg_engine_options(url)
        engine = create_async_engine(
            normalized_url,
            echo=echo,
            pool_pre_ping=True,
            # A failing statement is logged with its traceback. Without this the message repeats the
            # bound parameters - names, e-mail addresses, phone numbers of the CRM.
            hide_parameters=True,
            **engine_options,
        )

        session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        )

        return cls(engine=engine, session_factory=session_factory)

    @asynccontextmanager
    async def session(
        self, *, write: bool = True, isolation_level: IsolationLevel | None = None
    ) -> AsyncGenerator[AsyncSession]:
        """A session; with ``write``, one transaction that commits when the block succeeds, without it one that rolls
        back when the session closes. ``isolation_level`` applies to that transaction in both cases - for reads, one
        snapshot for the whole block - and the server's default holds without it."""
        async with self.session_factory() as session:
            try:
                if write:
                    async with session.begin():
                        await _isolate(session, isolation_level)
                        yield session
                else:
                    await _isolate(session, isolation_level)
                    yield session
            except Exception:
                await session.rollback()
                raise

    async def health(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError, OSError:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


async def _isolate(session: AsyncSession, isolation_level: IsolationLevel | None) -> None:
    """Begin the session's transaction at ``isolation_level``; the pool resets the connection's level afterwards."""
    if isolation_level is not None:
        await session.connection(execution_options={"isolation_level": isolation_level})


def _normalize_asyncpg_engine_options(url: str) -> tuple[str, dict[str, object]]:
    parsed_url = make_url(url)
    if parsed_url.drivername != "postgresql+asyncpg":
        return url, {}

    query = dict(parsed_url.query)
    query.setdefault("prepared_statement_cache_size", "0")
    normalized_url = parsed_url.set(query=query).render_as_string(hide_password=False)

    engine_options: dict[str, object] = {}
    if _uses_connection_pooler(parsed_url):
        engine_options["poolclass"] = NullPool
        engine_options["connect_args"] = {
            "prepared_statement_name_func": lambda: "",
        }

    return normalized_url, engine_options


def _uses_connection_pooler(url: URL) -> bool:
    host = url.host or ""
    return "pooler" in host or "pgbouncer" in host
