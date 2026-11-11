"""Two overlapping links of one sevDesk contact answer `sevdesk_contact_already_linked` or the link, never a 500.

This needs two real transactions, so it cannot run on the rolled-back ``session`` fixture: it commits, and removes
what it created.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import delete

from app.core.db import Database
from app.core.db.models import SevdeskContact
from app.services.billing import payers
from app.services.billing.errors import SevdeskContactAlreadyLinkedError
from app.services.crm import parties, persons

pytestmark = pytest.mark.db

CONTACT = "4711003"


async def _race(db: Database, first_party: uuid.UUID, second_party: uuid.UUID) -> SevdeskContact:
    """Link the contact to ``first_party`` without committing, start linking it to ``second_party``, see that wait,
    commit the first and return what the second answers."""
    first, second = db.session_factory(), db.session_factory()
    try:
        await payers.link_sevdesk_contact(first, first_party, CONTACT)
        racing = asyncio.create_task(payers.link_sevdesk_contact(second, second_party, CONTACT))
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second link waits for the first"

        await first.commit()
        return await asyncio.wait_for(racing, timeout=10)
    finally:
        await first.close()
        await second.close()


@pytest.fixture
async def two_parties(db: Database):
    async with db.session() as setup:
        ids = [(await persons.create_person(setup, firstname=name, lastname="A")).id for name in ("Race", "Other")]
    yield ids
    async with db.session() as cleanup:
        await cleanup.execute(delete(SevdeskContact).where(SevdeskContact.sevdesk_id == CONTACT))
        for party_id in ids:
            await parties.delete_party(cleanup, party_id)


async def test_linking_one_contact_to_two_parties_at_once_is_already_linked(db: Database, two_parties):
    party, other = two_parties

    with pytest.raises(SevdeskContactAlreadyLinkedError):
        await _race(db, party, other)


async def test_the_same_link_twice_at_once_answers_the_link_both_times(db: Database, two_parties):
    party, _ = two_parties

    link = await _race(db, party, party)

    assert (link.sevdesk_id, link.party_id) == (CONTACT, party)
