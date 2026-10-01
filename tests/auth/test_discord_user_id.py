"""`DiscordUserId`: a snowflake is a decimal string on the wire and an `int` in Python (bot-decoupling, decision I).

`StrictDiscordUserId` is its variant for request bodies: a string only, never a JSON number (P1-1).
"""

import json

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.auth.inputs import MAX_BIGINT, DiscordUserId, StrictDiscordUserId

ADAPTER = TypeAdapter(DiscordUserId)
STRICT = TypeAdapter(StrictDiscordUserId)
SNOWFLAKE = 123456789012345678  # above 2**53: a float or a JavaScript number would round it


class _Body(BaseModel):
    discord_user_id: DiscordUserId


class _StrictBody(BaseModel):
    discord_user_id: StrictDiscordUserId


@pytest.mark.parametrize("value", ["0", "42", str(SNOWFLAKE), str(MAX_BIGINT)])
def test_a_decimal_string_parses_to_its_int(value: str):
    assert ADAPTER.validate_python(value) == int(value)


def test_an_int_from_python_code_is_accepted():
    assert ADAPTER.validate_python(SNOWFLAKE) == SNOWFLAKE


@pytest.mark.parametrize("value", ["-1", "+5", " 7", "1e3", "0x10", "", str(MAX_BIGINT + 1), "0" * 20, True, 1.0, -1])
def test_anything_else_is_refused(value: object):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(value)


def test_it_serializes_as_a_string_so_javascript_keeps_every_digit():
    assert _Body(discord_user_id=SNOWFLAKE).model_dump(mode="json") == {"discord_user_id": str(SNOWFLAKE)}


def test_its_json_schema_is_a_decimal_string():
    schema = _Body.model_json_schema()["properties"]["discord_user_id"]

    assert schema["type"] == "string"
    assert schema["pattern"] == "^[0-9]{1,19}$"


@pytest.mark.parametrize("value", ["0", str(SNOWFLAKE), str(MAX_BIGINT)])
def test_the_strict_type_parses_a_decimal_string_to_its_int(value: str):
    assert STRICT.validate_python(value) == int(value)


@pytest.mark.parametrize("value", [SNOWFLAKE, 0, 1.0, True, None, b"42"])
def test_the_strict_type_refuses_anything_but_a_string_even_from_python(value: object):
    with pytest.raises(ValidationError):
        STRICT.validate_python(value)


@pytest.mark.parametrize("value", ["-1", "+5", " 7", "1e3", "0x10", "", str(MAX_BIGINT + 1), "0" * 20])
def test_the_strict_type_refuses_every_string_the_lenient_type_refuses(value: str):
    with pytest.raises(ValidationError):
        STRICT.validate_python(value)


def test_a_json_number_in_a_body_is_refused_not_rounded():
    """FastAPI validates a JSON body in Python mode, where a number arrives as an ``int``: only the strict type
    refuses it - the lenient one would take whatever JavaScript made of the snowflake."""
    body = json.loads(f'{{"discord_user_id": {SNOWFLAKE}}}')

    assert _Body.model_validate(body).discord_user_id == SNOWFLAKE
    with pytest.raises(ValidationError):
        _StrictBody.model_validate(body)


def test_the_strict_type_serializes_and_documents_itself_like_the_lenient_one():
    strict = _StrictBody(discord_user_id=str(SNOWFLAKE))

    assert strict.model_dump(mode="json") == {"discord_user_id": str(SNOWFLAKE)}
    assert _StrictBody.model_json_schema()["properties"] == _Body.model_json_schema()["properties"]
