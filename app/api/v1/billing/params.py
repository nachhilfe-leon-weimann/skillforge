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
