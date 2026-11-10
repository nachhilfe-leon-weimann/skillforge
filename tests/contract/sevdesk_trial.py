"""The contract run's way into the sevDesk trial: an expired trial is a skip, any other failed check a failure."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from http import HTTPStatus

import pytest

from app.integrations.errors import IdentityCheckError
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.transport import InnerTransport


@asynccontextmanager
async def connect_trial(
    settings: SevdeskSettings, *, transport: InnerTransport | None = None
) -> AsyncGenerator[SevdeskClient]:
    async with AsyncExitStack() as stack:
        try:
            client = await stack.enter_async_context(SevdeskClient.connect(settings, transport=transport))
        except IdentityCheckError as exc:
            if exc.status == HTTPStatus.UNAUTHORIZED:
                pytest.skip("trial expired: sevDesk refuses the trial's token (HTTP 401)")
            raise
        yield client
