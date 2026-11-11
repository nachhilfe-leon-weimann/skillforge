"""Path and query vocabulary of the billing routes; endpoint signatures use these aliases only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import Path, Query
from pydantic import Field

from app.api.v1.common import PageParams
from app.core.db.models import LessonStatus

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
