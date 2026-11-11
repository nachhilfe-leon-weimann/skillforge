"""Shared pieces of the calendar import tests: Graph behind a mocked transport, synthetic events, row snapshots.

Every reader runs the real ``GraphClient`` on ``FakeGraph`` (fixtures G1 to G7, ``tests/fixtures/graph/``): no test
reaches Microsoft. Names and ids are synthetic.
"""

import datetime as dt
import time
import uuid
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import PreferredMeetingTool
from app.integrations.config import IntegrationMode
from app.integrations.graph.client import Clock
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from app.services.billing.calendar import CalendarReader, graph_calendar
from app.services.crm import persons
from app.services.crm.inputs import StudentRoleData
from tests.integrations.graph.fake_graph import (
    CALENDAR_NAME,
    CLIENT_ID,
    MAILBOX,
    TENANT_ID,
    VIEW_PATH,
    Credential,
    FakeGraph,
    fixture,
)

NOW = dt.datetime(2026, 12, 15, 12, tzinfo=dt.UTC)
# The names the fixtures use, all synthetic.
MAX, ERIKA, PAULA = ("Max", "Mustermann"), ("Erika", "Mustermann"), ("Paula", "Beispiel")


def graph_settings(credential: Credential) -> GraphSettings:
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


def reader(fake: FakeGraph, settings: GraphSettings, clock: Clock = time.monotonic) -> CalendarReader:
    """The import's reader over the fake: the real client, a token cache of its own, no real sleep - and, for the
    client's deadline, the clock a test moves."""
    return graph_calendar(settings, transport=fake.transport, tokens=TokenCache(), sleep=fake.sleep, clock=clock)


def answer_view(fake: FakeGraph, events: Iterable[dict[str, Any]]) -> None:
    """Let the calendar view answer with ``events``, in one page."""
    fake.on("GET", VIEW_PATH, httpx.Response(200, json={"value": list(events)}))


def fixture_events(name: str) -> list[dict[str, Any]]:
    return list(fixture(name)["response"]["body"]["value"])


def graph_event(
    event_id: str,
    subject: str,
    start: dt.datetime,
    minutes: int = 60,
    *,
    categories: Iterable[str] = (),
    all_day: bool = False,
    location: str = "",
) -> dict[str, Any]:
    """A ``calendarView`` item in the shape of the fixtures, times in UTC with seven fractional digits."""

    def utc(moment: dt.datetime) -> dict[str, str]:
        return {"dateTime": moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}

    return {
        "id": event_id,
        "type": "singleInstance",
        "subject": subject,
        "start": utc(start),
        "end": utc(start + dt.timedelta(minutes=minutes)),
        "isAllDay": all_day,
        "isCancelled": False,
        "isOnlineMeeting": False,
        "categories": list(categories),
        "location": {"displayName": location},
    }


def moved(event: dict[str, Any], start: dt.datetime | None = None, minutes: int | None = None) -> dict[str, Any]:
    """The same occurrence - same ``id`` - at another start or with another length, as an ``exception``."""
    old_start = dt.datetime.fromisoformat(event["start"]["dateTime"][:19]).replace(tzinfo=dt.UTC)
    old_minutes = int(
        (dt.datetime.fromisoformat(event["end"]["dateTime"][:19]).replace(tzinfo=dt.UTC) - old_start).total_seconds()
        // 60
    )
    built = graph_event(event["id"], event["subject"], start or old_start, old_minutes if minutes is None else minutes)
    return {**event, "type": "exception", "start": built["start"], "end": built["end"]}


async def new_student(session: AsyncSession, name: tuple[str, str]) -> uuid.UUID:
    firstname, lastname = name
    party = await persons.create_person(
        session,
        firstname=firstname,
        lastname=lastname,
        student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
    )
    return party.id


async def snapshot(session: AsyncSession) -> list[tuple[str, str, str]]:
    """Every lesson and occurrence, byte for byte: its whole row as JSON and its ``ctid``, which every write - an
    ``UPDATE`` to the same values included - moves."""
    rows = await session.execute(
        text(
            "SELECT 'lesson', ctid::text, to_jsonb(l)::text FROM billing.lesson l "
            "UNION ALL SELECT 'event', ctid::text, to_jsonb(e)::text FROM billing.calendar_event e "
            "ORDER BY 1, 3"
        )
    )
    return [tuple(row) for row in rows]
