"""The vocabulary of change feeds: what every `updated_since` parameter means. See "Change signals" in
docs/ARCHITECTURE.md."""

from typing import Annotated

from pydantic import AwareDatetime, Field

UpdatedSince = Annotated[
    AwareDatetime | None,
    Field(
        description=(
            "Only items changed at or after this instant, the boundary included. Write the offset as `Z` or "
            "percent-encode the `+`. A change carries the start time of its transaction and can appear behind newer "
            "ones: ask from the newest `updated_at` seen minus an overlap (5 minutes), and keep it fixed while "
            "paging. Deleted items are not reported, and pages are not a snapshot: filter by fixed facts only, and "
            'compare in full now and then. See "Change signals" in docs/ARCHITECTURE.md.'
        ),
        examples=["2026-09-25T08:00:00Z"],
    ),
]
"""The `updated_since` filter of every feed; declare it as ``updated_since: UpdatedSince = None``."""
