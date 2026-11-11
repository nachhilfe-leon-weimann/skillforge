import pytest
from pydantic import SecretStr

from app.integrations.config import IntegrationMode
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from tests.integrations.graph.fake_graph import CALENDAR_NAME, CLIENT_ID, MAILBOX, TENANT_ID, Credential, FakeGraph


@pytest.fixture(scope="session")
def credential() -> Credential:
    return Credential.generate()


@pytest.fixture
def fake_graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def tokens() -> TokenCache:
    """A cache of this test's own: the process's cache is never touched."""
    return TokenCache()


@pytest.fixture
def settings(credential: Credential) -> GraphSettings:
    return GraphSettings(
        _env_file=None,
        mode=IntegrationMode.DRY_RUN,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        certificate_pem=SecretStr(credential.certificate_setting),
        private_key_pem=SecretStr(credential.key_setting),
        mailbox=MAILBOX,
        calendar_name=CALENDAR_NAME,
    )
