"""Every feed of the pull contract takes `updated_since` from the one `UpdatedSince` alias (bot-decoupling P0-6)."""

from typing import Any, get_args

import pytest
from pydantic.fields import FieldInfo

from app.api.v1.common import UpdatedSince
from app.main import app

FEEDS = {"/api/v1/crm/parties", "/api/v1/auth/discord-links"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


def _updated_since(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every `updated_since` query parameter of the contract, by path."""
    return {
        path: parameter
        for path, item in schema["paths"].items()
        for parameter in item.get("get", {}).get("parameters", [])
        if parameter["name"] == "updated_since"
    }


def test_the_feeds_are_exactly_the_party_list_and_the_discord_link_feed(schema: dict[str, Any]):
    assert set(_updated_since(schema)) == FEEDS


def test_every_feed_declares_updated_since_through_the_one_alias(schema: dict[str, Any]):
    (field,) = [metadata for metadata in get_args(UpdatedSince) if isinstance(metadata, FieldInfo)]
    links, parties = (_updated_since(schema)[path] for path in sorted(FEEDS))

    assert parties == links
    assert parties["description"] == field.description
    assert parties["schema"]["examples"] == field.examples
    assert not parties["required"]
