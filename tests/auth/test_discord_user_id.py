"""`DiscordUserId`: a snowflake is a decimal string on the wire and an `int` in Python (bot-decoupling, decision I)."""

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.auth.inputs import MAX_BIGINT, DiscordUserId

ADAPTER = TypeAdapter(DiscordUserId)
SNOWFLAKE = 123456789012345678  # above 2**53: a float or a JavaScript number would round it


class _Body(BaseModel):
    discord_user_id: DiscordUserId


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
