from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from skillcore import get_project_version

from app.api import router
from app.api.v1.common import OPENAPI_TAGS, customize_openapi, operation_id, register_exception_handlers
from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import configure_logging, get_logger, register_request_logging

settings = get_settings()
configure_logging(settings.logging)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own a single database engine for the whole app lifetime.

    The old per-request ``get_database`` built a fresh engine on every call,
    paying cold pool setup each time and leaking engines on the health path.
    We now create one shared ``Database`` at startup, expose it on
    ``app.state``, and dispose it exactly once on shutdown. See #57.
    """
    database = Database.from_url(str(settings.db.url))
    app.state.database = database
    logger.info("db_engine_created")
    try:
        yield
    finally:
        await database.dispose()
        logger.info("db_engine_disposed")


app = FastAPI(
    title="skillforge",
    version=get_project_version(),
    description="The platform's hub for central data, identity, permissions and domain rules",
    lifespan=lifespan,
    generate_unique_id_function=operation_id,
    openapi_tags=OPENAPI_TAGS,
)
register_request_logging(app)
register_exception_handlers(app)
app.include_router(router)


@app.get("/", tags=["system"])
async def root():
    return {"message": "Welcome to the skillforge API!"}


# Last, once every route is registered: it builds the schema right away.
customize_openapi(app)
