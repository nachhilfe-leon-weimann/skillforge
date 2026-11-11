"""Graph settings are optional, nested under ``GRAPH__`` and keep the credential out of every representation."""

import base64
import re
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import Settings
from app.integrations.config import IntegrationMode
from app.integrations.graph.config import GraphSettings
from tests.integrations.graph.fake_graph import CALENDAR_NAME, CLIENT_ID, MAILBOX, TENANT_ID, Credential

FIELDS = ("MODE", "TENANT_ID", "CLIENT_ID", "CERTIFICATE_PEM", "PRIVATE_KEY_PEM", "MAILBOX", "CALENDAR_NAME")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No ``GRAPH__*`` variable, and a working directory without an env file: independent of the checkout."""
    for field in FIELDS:
        monkeypatch.delenv(f"GRAPH__{field}", raising=False)
        monkeypatch.delenv(f"graph__{field.lower()}", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_the_app_settings_load_without_any_graph_variable(clean_env):
    graph = Settings(_env_file=None).graph

    assert graph.mode is IntegrationMode.DRY_RUN
    assert all(getattr(graph, field.lower()) is None for field in FIELDS if field != "MODE")


def test_the_variables_are_read_under_their_prefix(clean_env, credential: Credential):
    values = (
        "live",
        TENANT_ID,
        CLIENT_ID,
        credential.certificate_setting,
        credential.key_setting,
        MAILBOX,
        CALENDAR_NAME,
    )
    for field, value in zip(FIELDS, values, strict=True):
        clean_env.setenv(f"GRAPH__{field}", value)

    graph = Settings(_env_file=None).graph

    assert graph.mode is IntegrationMode.LIVE
    assert (graph.tenant_id, graph.client_id) == (UUID(TENANT_ID), UUID(CLIENT_ID))
    assert (graph.mailbox, graph.calendar_name) == (MAILBOX, CALENDAR_NAME)
    assert graph.private_key_pem is not None and graph.private_key_pem.get_secret_value() == credential.key_setting
    for text in (repr(graph), str(graph)):
        for hidden in (credential.key_setting, credential.certificate_setting, MAILBOX, CALENDAR_NAME):
            assert hidden not in text


def test_an_empty_variable_means_unset(clean_env):
    for field in FIELDS:
        if not field.endswith("_PEM"):
            clean_env.setenv(f"GRAPH__{field}", "")

    for graph in (GraphSettings(_env_file=None), Settings(_env_file=None).graph):
        assert graph.mode is IntegrationMode.DRY_RUN
        assert (graph.tenant_id, graph.client_id, graph.mailbox, graph.calendar_name) == (None, None, None, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MODE", "write"),
        ("TENANT_ID", "contoso.example.org"),
        ("CLIENT_ID", "skillforge"),
        ("MAILBOX", "lessons"),
        ("MAILBOX", "lessons@example.org/../other@example.org"),
        ("MAILBOX", "lessons@example.org?$select=id"),
    ],
)
def test_an_invalid_value_is_refused(clean_env, field: str, value: str):
    clean_env.setenv(f"GRAPH__{field}", value)

    with pytest.raises(ValueError):
        GraphSettings(_env_file=None)


@pytest.mark.parametrize("field", ["CERTIFICATE_PEM", "PRIVATE_KEY_PEM"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64!",
        base64.b64encode(b"\xff\xfe").decode(),
        base64.b64encode(b"synthetic plain text").decode(),
        "-----BEGIN CERTIFICATE-----",
    ],
    ids=["empty", "not base64", "not text", "not PEM", "PEM not encoded"],
)
def test_a_credential_that_is_no_encoded_pem_is_refused_without_repeating_it(clean_env, field: str, value: str):
    clean_env.setenv(f"GRAPH__{field}", value)

    for load in (lambda: GraphSettings(_env_file=None), lambda: Settings(_env_file=None)):
        with pytest.raises(ValueError, match=f"GRAPH__{field}") as raised:
            load()
        assert not value or value not in str(raised.value)


def test_a_certificate_is_no_key_and_a_key_no_certificate(clean_env, credential: Credential):
    clean_env.setenv("GRAPH__PRIVATE_KEY_PEM", credential.certificate_setting)

    with pytest.raises(ValueError, match="GRAPH__PRIVATE_KEY_PEM"):
        GraphSettings(_env_file=None)


def test_the_example_env_file_lists_every_setting_and_nothing_else():
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    listed = {name.lower() for name in re.findall(r"^#?GRAPH__(\w+)=$", example, flags=re.MULTILINE)}

    assert listed == set(GraphSettings.model_fields)
    for secret in ("CERTIFICATE_PEM", "PRIVATE_KEY_PEM"):
        assert re.search(rf"^GRAPH__{secret}=", example, flags=re.MULTILINE) is None, "an empty secret is refused"
