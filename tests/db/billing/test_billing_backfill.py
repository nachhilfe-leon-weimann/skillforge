"""`just billing-backfill` on Postgres (billing spec, SF3-2): a dry run writes and commits nothing; `--apply` writes the
whole file in one transaction, or nothing; a second run changes no row; one bad row refuses the whole file; the output
holds counts, line numbers and reason codes only.

The command runs in-process on the test's session: `--apply` in a SAVEPOINT standing in for its transaction, a dry
run on the session as it is - so a write of a dry run stays visible to the snapshot, and a commit is recorded.
"""

import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.cli import billing_backfill
from app.cli.billing_backfill import COLUMNS
from app.core.db.models import ManualPayer, PartyRelation, PartyRelationType, PreferredMeetingTool, Rate, SevdeskContact
from app.services.billing import payers, rates
from app.services.billing.errors import SevdeskContactAlreadyLinkedError
from app.services.crm import companies, persons, relations
from app.services.crm.inputs import StudentRoleData
from tests.db.billing.test_seed_demo import _snapshot

pytestmark = pytest.mark.db

PASSWORD = "secret-pw-4711"
URL = f"postgresql+asyncpg://maintainer:{PASSWORD}@db.example.org:5432/skillforge"
CONTACT = "4242424201"
OTHER_CONTACT = "4242424202"
THIRD_CONTACT = "4242424203"
CENTS = 3132
DAY = date(2031, 7, 19)
DRY_RUN = "dry run: nothing written; pass --apply to write"
REFUSED = re.compile(r"^billing-backfill refused: \d+ problems; nothing written$")
# Every line the command may print: counts, line numbers and reason codes.
OUTPUT_LINE = re.compile(
    r"rows: \d+|[a-z_]+: \d+ (to add|added), \d+ unchanged|line \d+: [a-z_]+( \([a-z_]+\))?"
    r"|dry run: nothing written; pass --apply to write|written in one transaction"
)


@dataclass(frozen=True)
class Run:
    code: object
    """``None`` for success, the message the command exits with otherwise."""
    out: str

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()


@pytest.fixture
def commits(session: AsyncSession) -> Iterator[list[str]]:
    """Every COMMIT and released SAVEPOINT on the test's connection: what a transaction of the command would commit."""
    connection = session.bind
    assert isinstance(connection, AsyncConnection)
    recorded: list[str] = []

    def commit(*_: object) -> None:
        recorded.append("commit")

    def release(*_: object) -> None:
        recorded.append("release savepoint")

    event.listen(connection.sync_connection, "commit", commit)
    event.listen(connection.sync_connection, "release_savepoint", release)
    yield recorded
    event.remove(connection.sync_connection, "commit", commit)
    event.remove(connection.sync_connection, "release_savepoint", release)


@pytest.fixture
def backfill(session: AsyncSession, monkeypatch, capsys, tmp_path: Path) -> Callable[..., Awaitable[Run]]:
    """Write the rows to a CSV outside the repository and run the command on it."""

    class TestDatabase:
        @classmethod
        def from_url(cls, given: str) -> TestDatabase:
            assert given == URL
            return cls()

        @asynccontextmanager
        async def session(self, *, write: bool = True) -> AsyncGenerator[AsyncSession]:
            if write:
                async with session.begin_nested():
                    yield session
            else:
                yield session

        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(billing_backfill, "get_settings", lambda: SimpleNamespace(db=SimpleNamespace(url=URL)))
    monkeypatch.setattr(billing_backfill, "Database", TestDatabase)

    async def run(*rows: dict[str, Any], apply: bool) -> Run:
        path = tmp_path / "confirmed mapping.csv"
        lines = [",".join(COLUMNS), *(",".join(str(row.get(column, "")) for column in COLUMNS) for row in rows)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            await billing_backfill.billing_backfill(str(path), apply=apply)
            code = None
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return Run(code, captured.out + captured.err)

    return run


async def _person(session: AsyncSession, lastname: str, *, student: bool = True) -> uuid.UUID:
    role = StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD) if student else None
    return (await persons.create_person(session, firstname="Synthetic", lastname=lastname, student=role)).id


def _row(student: uuid.UUID, payer: uuid.UUID | None = None, **cells: object) -> dict[str, Any]:
    return {"student_party_id": student, "payer_party_id": payer or "", **cells}


@dataclass(frozen=True)
class Family:
    """Parent A pays for student B, whose rate starts on ``DAY``; student C pays for themselves and is billed by
    hand."""

    parent: uuid.UUID
    student: uuid.UUID
    self_payer: uuid.UUID

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [
            _row(self.student, self.parent, sevdesk_contact_id=CONTACT, rate_cents_per_hour=CENTS, rate_valid_from=DAY),
            _row(self.self_payer, self.self_payer, manual_billing="true"),
        ]


@pytest.fixture
async def family(session: AsyncSession) -> Family:
    return Family(await _person(session, "A", student=False), await _person(session, "B"), await _person(session, "C"))


async def _written(session: AsyncSession, family: Family) -> dict[str, list[Any]]:
    return {
        "pays_for": list(
            await session.execute(
                select(PartyRelation.from_party_id, PartyRelation.to_party_id).where(
                    PartyRelation.type == PartyRelationType.PAYS_FOR
                )
            )
        ),
        "contacts": list(await session.execute(select(SevdeskContact.party_id, SevdeskContact.sevdesk_id))),
        "manual": list(await session.scalars(select(ManualPayer.party_id))),
        "rates": list(await session.execute(select(Rate.student_party_id, Rate.valid_from, Rate.cents_per_hour))),
    }


# --- Dry run, apply, second run ---------------------------------------------------------------------------------


async def test_a_dry_run_prints_what_it_would_do_and_writes_and_commits_nothing(
    session: AsyncSession, family: Family, backfill, commits: list[str]
):
    before = await _snapshot(session)

    run = await backfill(*family.rows, apply=False)

    assert run.code is None
    assert run.lines == [
        "rows: 2",
        "pays_for: 1 to add, 1 unchanged",
        "sevdesk_contact: 1 to add, 0 unchanged",
        "manual_billing: 1 to add, 0 unchanged",
        "rate: 1 to add, 0 unchanged",
        DRY_RUN,
    ]
    assert await _snapshot(session) == before
    assert commits == []


async def test_apply_writes_the_file_and_a_second_run_changes_no_row(session: AsyncSession, family: Family, backfill):
    first = await backfill(*family.rows, apply=True)
    after_first = await _snapshot(session)
    second = await backfill(*family.rows, apply=True)

    assert first.code is None
    assert first.lines[1:] == [
        "pays_for: 1 added, 1 unchanged",
        "sevdesk_contact: 1 added, 0 unchanged",
        "manual_billing: 1 added, 0 unchanged",
        "rate: 1 added, 0 unchanged",
        "written in one transaction",
    ]
    # Student C pays for themselves: no relation, the contact and the mark go to the student.
    assert await _written(session, family) == {
        "pays_for": [(family.parent, family.student)],
        "contacts": [(family.parent, CONTACT)],
        "manual": [family.self_payer],
        "rates": [(family.student, DAY, CENTS)],
    }
    assert await _snapshot(session) == after_first
    assert second.code is None
    assert second.lines[1:5] == [
        "pays_for: 0 added, 2 unchanged",
        "sevdesk_contact: 0 added, 1 unchanged",
        "manual_billing: 0 added, 1 unchanged",
        "rate: 0 added, 1 unchanged",
    ]


async def test_rows_already_as_the_database_holds_them_are_unchanged(session: AsyncSession, family: Family, backfill):
    """What the services wrote before - through the API, say - counts as unchanged; a fact repeated in rows once;
    `false` for a payer without a mark is unchanged; a company may pay."""
    company = (await companies.create_company(session, name="Authority D")).id
    paid_by_company = await _person(session, "E")
    await relations.put_relation(session, family.parent, PartyRelationType.PAYS_FOR, family.student)
    await payers.link_sevdesk_contact(session, family.parent, CONTACT)
    await rates.put_rate(session, family.student, DAY, cents_per_hour=CENTS)
    before = await _snapshot(session)

    run = await backfill(
        *family.rows,
        _row(family.student, family.parent, manual_billing="false"),
        _row(family.student, rate_cents_per_hour=CENTS, rate_valid_from=DAY),
        _row(paid_by_company, company, sevdesk_contact_id=OTHER_CONTACT),
        apply=False,
    )

    assert run.code is None
    assert run.lines[1:5] == [
        "pays_for: 1 to add, 2 unchanged",
        "sevdesk_contact: 1 to add, 1 unchanged",
        "manual_billing: 1 to add, 1 unchanged",
        "rate: 0 to add, 1 unchanged",
    ]
    assert await _snapshot(session) == before


# --- One bad row refuses the file -------------------------------------------------------------------------------

Setup = Callable[[AsyncSession, Family], Awaitable[tuple[list[dict[str, Any]], list[str]]]]


async def _unknown_student(session: AsyncSession, family: Family):
    return [_row(uuid.uuid4(), family.parent)], ["line 4: unknown_party (student_party_id)"]


async def _unknown_payer(session: AsyncSession, family: Family):
    return [_row(await _person(session, "D"), uuid.uuid4())], ["line 4: unknown_party (payer_party_id)"]


async def _no_student(session: AsyncSession, family: Family):
    return [_row(family.parent, rate_cents_per_hour=CENTS, rate_valid_from=DAY)], [
        "line 4: not_a_student (student_party_id)"
    ]


async def _rate_against_rule_g(session: AsyncSession, family: Family):
    return [_row(family.self_payer, rate_cents_per_hour=3001, rate_valid_from=DAY)], ["line 4: rate_not_quarter_hour"]


async def _rate_without_day(session: AsyncSession, family: Family):
    return [_row(family.self_payer, rate_cents_per_hour=CENTS)], ["line 4: incomplete_rate"]


async def _another_rate_that_day(session: AsyncSession, family: Family):
    await rates.put_rate(session, family.self_payer, DAY, cents_per_hour=2800)
    return [_row(family.self_payer, rate_cents_per_hour=CENTS, rate_valid_from=DAY)], ["line 4: rate_conflict"]


async def _two_rates_one_day(session: AsyncSession, family: Family):
    return [_row(family.student, rate_cents_per_hour=2800, rate_valid_from=DAY)], [
        "line 2: rate_conflict",
        "line 4: rate_conflict",
    ]


async def _second_payer_in_the_crm(session: AsyncSession, family: Family):
    other = await _person(session, "D", student=False)
    await relations.put_relation(session, other, PartyRelationType.PAYS_FOR, family.self_payer)
    return [], ["line 3: second_payer"]


async def _second_payer_in_the_file(session: AsyncSession, family: Family):
    other = await _person(session, "D", student=False)
    return [_row(family.student, other)], ["line 2: second_payer", "line 4: second_payer"]


async def _contact_linked_elsewhere(session: AsyncSession, family: Family):
    other = await _person(session, "D", student=False)
    await payers.link_sevdesk_contact(session, other, OTHER_CONTACT)
    return [_row(family.self_payer, family.self_payer, sevdesk_contact_id=OTHER_CONTACT)], ["line 4: contact_conflict"]


async def _second_contact_for_a_party(session: AsyncSession, family: Family):
    await payers.link_sevdesk_contact(session, family.self_payer, OTHER_CONTACT)
    return [_row(family.self_payer, family.self_payer, sevdesk_contact_id=THIRD_CONTACT)], ["line 4: contact_conflict"]


async def _two_contacts_in_the_file(session: AsyncSession, family: Family):
    return [_row(family.student, family.parent, sevdesk_contact_id=OTHER_CONTACT)], [
        "line 2: contact_conflict",
        "line 4: contact_conflict",
    ]


async def _one_contact_for_two_payers(session: AsyncSession, family: Family):
    return [_row(family.self_payer, family.self_payer, sevdesk_contact_id=CONTACT)], [
        "line 2: contact_conflict",
        "line 4: contact_conflict",
    ]


async def _unmarked_in_the_file_marked_in_the_database(session: AsyncSession, family: Family):
    await payers.put_manual_payer(session, family.parent)
    return [_row(family.student, family.parent, manual_billing="false")], ["line 4: manual_billing_conflict"]


async def _malformed_cell(session: AsyncSession, family: Family):
    return [_row(family.student, family.parent, rate_cents_per_hour="30.00", rate_valid_from=DAY)], [
        "line 4: malformed_cell (rate_cents_per_hour)"
    ]


@pytest.mark.parametrize(
    "setup",
    [
        _unknown_student,
        _unknown_payer,
        _no_student,
        _rate_against_rule_g,
        _rate_without_day,
        _another_rate_that_day,
        _two_rates_one_day,
        _second_payer_in_the_crm,
        _second_payer_in_the_file,
        _contact_linked_elsewhere,
        _second_contact_for_a_party,
        _two_contacts_in_the_file,
        _one_contact_for_two_payers,
        _unmarked_in_the_file_marked_in_the_database,
        _malformed_cell,
    ],
)
async def test_one_bad_row_after_good_ones_refuses_the_whole_file(
    session: AsyncSession, family: Family, backfill, setup: Setup
):
    bad_rows, problems = await setup(session, family)
    before = await _snapshot(session)

    run = await backfill(*family.rows, *bad_rows, apply=True)

    assert isinstance(run.code, str) and REFUSED.fullmatch(run.code)
    assert run.lines == [f"rows: {2 + len(bad_rows)}", *problems]
    assert await _snapshot(session) == before


async def test_every_problem_is_reported_at_once(session: AsyncSession, family: Family, backfill):
    run = await backfill(
        _row(uuid.uuid4()),
        *family.rows,
        _row(family.student, rate_cents_per_hour=3001, rate_valid_from=date(2031, 8, 1)),
        _row(family.self_payer, sevdesk_contact_id=CONTACT),
        apply=False,
    )

    assert run.code == "billing-backfill refused: 3 problems; nothing written"
    assert run.lines == [
        "rows: 5",
        "line 2: unknown_party (student_party_id)",
        "line 5: rate_not_quarter_hour",
        "line 6: needs_payer",
    ]


async def test_a_failure_midway_writes_nothing(session: AsyncSession, family: Family, backfill, monkeypatch):
    """The relation, the contact and the mark are written when the rate fails - as a race could make a service
    fail - and the transaction takes them back. The service's message, which names a party, is not printed."""
    put_rate = rates.put_rate
    written_before_the_failure: list[dict[str, list[Any]]] = []

    async def failing_put_rate(session: AsyncSession, student_id: uuid.UUID, *args: Any, **kwargs: Any) -> Any:
        if student_id == family.self_payer:
            written_before_the_failure.append(await _written(session, family))
            raise SevdeskContactAlreadyLinkedError(f"Party {student_id} or the contact is linked elsewhere")
        return await put_rate(session, student_id, *args, **kwargs)

    monkeypatch.setattr(rates, "put_rate", failing_put_rate)
    before = await _snapshot(session)

    run = await backfill(
        *family.rows, _row(family.self_payer, rate_cents_per_hour=CENTS, rate_valid_from=DAY), apply=True
    )

    assert run.code == "billing-backfill failed, nothing written: sevdesk_contact_already_linked"
    assert [len(rows) for rows in written_before_the_failure[0].values()] == [1, 1, 1, 1]
    assert await _snapshot(session) == before


# --- The output -------------------------------------------------------------------------------------------------


async def test_the_output_holds_counts_and_line_numbers_only(
    session: AsyncSession, family: Family, backfill, tmp_path: Path, monkeypatch
):
    """Runs whose file holds recognisable values - a dry run, an apply, a refused file, a failure midway - print
    no party id, no contact id, no cell, no path and no part of the database URL."""
    unknown = uuid.uuid4()
    runs = [
        await backfill(*family.rows, apply=False),
        await backfill(*family.rows, _row(family.self_payer, unknown, sevdesk_contact_id=CONTACT), apply=True),
        await backfill(*family.rows, apply=True),
    ]

    async def failing_put_relation(session: AsyncSession, party_id: uuid.UUID, *args: Any) -> Any:
        raise SevdeskContactAlreadyLinkedError(f"Party {party_id} or the contact {OTHER_CONTACT} is linked elsewhere")

    monkeypatch.setattr(relations, "put_relation", failing_put_relation)
    other = await _person(session, "D", student=False)
    runs.append(await backfill(_row(await _person(session, "E"), other, sevdesk_contact_id=OTHER_CONTACT), apply=True))

    parties = [family.parent, family.student, family.self_payer, other, unknown]
    secrets = [str(party) for party in parties] + [party.hex for party in parties]
    secrets += [CONTACT, OTHER_CONTACT, str(CENTS), DAY.isoformat(), PASSWORD, "db.example.org", str(tmp_path)]
    assert [run.code is None for run in runs] == [True, False, True, False]
    for run in runs:
        text = run.out + str(run.code or "")
        assert [secret for secret in secrets if secret.lower() in text.lower()] == []
        assert [line for line in run.lines if not OUTPUT_LINE.fullmatch(line)] == []
