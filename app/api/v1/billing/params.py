"""Path and query vocabulary of the billing routes; endpoint signatures use these aliases only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import Depends, Path, Query
from pydantic import Field

from app.api.v1.common import PageParams
from app.core.config import get_settings
from app.core.db.models import LessonStatus
from app.services.billing.calendar import CalendarReader, graph_calendar

LessonId = Annotated[
    uuid.UUID,
    Path(description="ID of the lesson.", examples=["5c1e7a2b-3d4f-4a6b-8c9d-0e1f2a3b4c5d"]),
]

StudentId = Annotated[
    uuid.UUID,
    Path(description="ID of the student's party.", examples=["7d9f4f3e-1c2b-4a5d-9e8f-0a1b2c3d4e5f"]),
]

PartyId = Annotated[
    uuid.UUID,
    Path(description="ID of the party: a person or a company.", examples=["3b8e2c1d-4f5a-4b6c-9d7e-8f9a0b1c2d3e"]),
]

AliasId = Annotated[
    uuid.UUID,
    Path(description="ID of the calendar alias.", examples=["2a6c8e0f-5b7d-4c9e-8f1a-3b5d7f9e1c2a"]),
]

EventId = Annotated[
    uuid.UUID,
    Path(description="ID of the calendar occurrence.", examples=["4e8a0c2b-6d1f-4b3a-9c5e-7f9a1b3d5e7c"]),
]

ValidFrom = Annotated[
    date,
    Path(description="The day the rate takes effect; it holds until the next rate's day.", examples=["2026-09-01"]),
]

# Where a created lesson can be read: the target of the `Location` header of `POST /lessons`.
LESSON_LOCATION = "/api/v1/billing/lessons/{lesson_id}"


class LessonListParams(PageParams):
    """Filters of `GET /lessons`. They never fail: a combination nothing matches is an empty page."""

    # The first and the last day of the calendar are refused: their Berlin midnight has no UTC datetime.
    from_: date | None = Field(
        None,
        alias="from",
        gt=date.min,
        lt=date.max,
        description="Only lessons starting on this day or later, as a day in `Europe/Berlin` (included).",
    )
    to: date | None = Field(
        None,
        gt=date.min,
        lt=date.max,
        description="Only lessons starting on this day or earlier, as a day in `Europe/Berlin` (included).",
    )
    student_id: uuid.UUID | None = Field(None, description="Only lessons of this student party.")
    status: LessonStatus | None = Field(None, description="Only lessons in this status.")


type LessonListQuery = Annotated[LessonListParams, Query()]


def get_calendar_reader() -> CalendarReader:
    """The one place the calendar import gets its reader: the Graph client built from the settings, per request.

    Building it reads nothing; settings that are missing or incomplete fail the read as `calendar_unavailable`. Tests
    override this dependency with a reader over a mocked transport.
    """
    return graph_calendar(get_settings().graph)


type CalendarSource = Annotated[CalendarReader, Depends(get_calendar_reader)]
