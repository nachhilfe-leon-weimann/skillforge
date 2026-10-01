"""Neither a one-time token, a password nor an e-mail address leaves the process (spec: security rules).

The create -> invite -> redeem flow runs with logging turned up and its JSON output captured, next to the
refusals that name an address; none of the three values may appear in that output or in an audit row. The
Discord link code flow keeps its code out of both, and its Discord user ID out of the request log
(bot-decoupling spec, "Security rules").
"""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import AuthAuditLog, UserAccount, UserActionToken
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging

pytestmark = pytest.mark.db

EMAIL = "anna.schmidt@example.org"
SNOWFLAKE = "123456789012345678"


@pytest.fixture
def restore_logging():
    """Put the logging configuration back, so the turned-up level ends with the test."""
    yield
    configure_logging(LoggingSettings())


async def test_the_create_invite_redeem_flow_logs_neither_the_token_the_password_nor_the_email(
    operator: AsyncClient, make_person, session: AsyncSession, password: str, restore_logging, capsys
):
    party, other = await make_person(), await make_person("Ben")
    # Configured inside the test: the handler writes to the stream that is current when it is set up.
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    created = await operator.post("/users", json={"party_id": str(party.id), "email": EMAIL})
    user_id = created.json()["id"]
    refused = await operator.post("/users", json={"party_id": str(other.id), "email": EMAIL.upper()})
    listed = await operator.get("/users", params={"email": EMAIL})
    token = (await operator.post(f"/users/{user_id}/invitation")).json()["token"]
    weak = await operator.post("/password/redeem", json={"token": token, "new_password": "short"})
    redeemed = await operator.post("/password/redeem", json={"token": token, "new_password": password})
    again = await operator.post("/password/redeem", json={"token": token, "new_password": password})

    output = capsys.readouterr().out
    assert [r.status_code for r in (created, refused, listed, weak, redeemed, again)] == [201, 409, 200, 422, 204, 422]
    assert output.count("http_request_") >= 6, "the flow logged its requests"
    for secret in (token, password, EMAIL, EMAIL.upper()):
        assert secret not in output

    details = [detail or "" for detail in await session.scalars(select(AuthAuditLog.detail))]
    assert details
    for secret in (token, password, EMAIL, "example.org"):
        assert not any(secret in detail for detail in details)
    assert token not in set(await session.scalars(select(UserActionToken.token_hash)))
    password_hash = await session.scalar(select(UserAccount.password_hash).where(UserAccount.party_id == party.id))
    assert password_hash is not None
    assert password not in password_hash


async def test_the_link_code_flow_logs_neither_the_code_nor_the_discord_user_id(
    link_admin: AsyncClient, link_bot: AsyncClient, make_person, session: AsyncSession, restore_logging, capsys
):
    party = await make_person()
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    user_id = (await link_admin.post("/users", json={"party_id": str(party.id)})).json()["id"]
    code = (await link_admin.post(f"/users/{user_id}/discord-link-code")).json()["token"]
    body = {"token": code, "discord_user_id": SNOWFLAKE}
    redeemed = await link_bot.post("/discord-links/redeem", json=body)
    again = await link_bot.post("/discord-links/redeem", json=body)
    read = await link_admin.get(f"/discord-links/{SNOWFLAKE}")

    output = capsys.readouterr().out
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    # The test's own HTTP client logs every URL it calls (logger `httpx`); SkillForge's lines must hold no Discord ID.
    served = json.dumps([line for line in lines if line.get("logger") != "httpx"])
    paths = [line["path"] for line in lines if line["event"].startswith("http_request_")]
    assert [r.status_code for r in (redeemed, again, read)] == [200, 422, 200]
    assert paths.count("/api/v1/auth/discord-links/redeem") == 2, "the redeem path holds no digit and stays readable"
    assert "/api/v1/auth/discord-links/{discord_user_id}" in paths
    assert code not in output
    assert SNOWFLAKE not in served

    details = [detail or "" for detail in await session.scalars(select(AuthAuditLog.detail))]
    assert any(SNOWFLAKE in detail for detail in details), "the audit rows name the Discord user"
    assert not any(code in detail for detail in details)
    assert code not in set(await session.scalars(select(UserActionToken.token_hash)))
