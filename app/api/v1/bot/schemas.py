from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.db.models import (
    CommandEnvKind,
    ContactInfoType,
    JobStatus,
    MemberRole,
    OperationKind,
    OperationStatus,
    PartyRelationType,
    StudentChannelState,
)

if TYPE_CHECKING:
    from app.core.db.models import (
        DiscordUser,
        DiscordUserPermissionGroup,
        Job,
        Operation,
        Party,
        StudentWorkspace,
        TutorWorkspace,
    )
    from app.services.bot.views import JobQueueSummaryView


# --- Operational profile ----------------------------------------------------


class PersonProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    firstname: str
    lastname: str


class ContactInfoProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: ContactInfoType
    value: str
    label: str | None = None


class RelationProfile(BaseModel):
    type: PartyRelationType
    direction: Literal["outgoing", "incoming"]
    counterparty_party_id: UUID


class DiscordAccountProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    discord_id: int
    is_primary: bool
    active: bool


class MicrosoftAccountProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str


class ExternalAccountsProfile(BaseModel):
    discord: list[DiscordAccountProfile] = Field(default_factory=list)
    microsoft: MicrosoftAccountProfile | None = None


class OperationalProfile(BaseModel):
    """Operational view of a party. Intentionally excludes billing/finance data."""

    party_id: UUID
    person: PersonProfile | None = None
    contact_infos: list[ContactInfoProfile] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    relations: list[RelationProfile] = Field(default_factory=list)
    external_accounts: ExternalAccountsProfile = Field(default_factory=ExternalAccountsProfile)

    @classmethod
    def from_party(cls, party: Party) -> OperationalProfile:
        person = party.person

        subjects: set[str] = set()
        if person is not None:
            if person.tutor is not None:
                subjects.update(ts.subject.title for ts in person.tutor.tutor_subjects)
            if person.student is not None:
                subjects.update(ss.subject.title for ss in person.student.student_subjects)

        relations: list[RelationProfile] = []
        for relation in party.outgoing_relations:
            if relation.type is PartyRelationType.PAYS_FOR:
                continue
            relations.append(
                RelationProfile(
                    type=relation.type,
                    direction="outgoing",
                    counterparty_party_id=relation.to_party_id,
                )
            )
        for relation in party.incoming_relations:
            if relation.type is PartyRelationType.PAYS_FOR:
                continue
            relations.append(
                RelationProfile(
                    type=relation.type,
                    direction="incoming",
                    counterparty_party_id=relation.from_party_id,
                )
            )

        return cls(
            party_id=party.id,
            person=PersonProfile.model_validate(person) if person is not None else None,
            contact_infos=[ContactInfoProfile.model_validate(info) for info in party.contact_infos],
            subjects=sorted(subjects),
            relations=relations,
            external_accounts=ExternalAccountsProfile(
                discord=[DiscordAccountProfile.model_validate(account) for account in party.discord_accounts],
                microsoft=(
                    MicrosoftAccountProfile.model_validate(party.microsoft_account)
                    if party.microsoft_account is not None
                    else None
                ),
            ),
        )


# --- Principal & runtime contexts -------------------------------------------


class BotPrincipal(BaseModel):
    discord_id: int
    role: MemberRole
    display_name: str
    active: bool
    groups: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    profile: OperationalProfile | None = None

    @classmethod
    def from_parts(
        cls,
        *,
        user: DiscordUser,
        group_keys: list[str],
        permission_keys: list[str],
        party: Party | None,
    ) -> BotPrincipal:
        return cls(
            discord_id=user.discord_id,
            role=user.role,
            display_name=user.nick_name,
            active=user.active,
            groups=group_keys,
            permissions=permission_keys,
            profile=OperationalProfile.from_party(party) if party is not None else None,
        )


class TutorContext(BaseModel):
    principal: BotPrincipal
    guild_id: int
    category_channel_id: int
    command_channel_id: int
    student_channel_capacity: int

    @classmethod
    def from_parts(cls, *, principal: BotPrincipal, workspace: TutorWorkspace) -> TutorContext:
        return cls(
            principal=principal,
            guild_id=workspace.guild_id,
            category_channel_id=workspace.category_channel_id,
            command_channel_id=workspace.command_channel_id,
            student_channel_capacity=workspace.student_channel_capacity,
        )


class StudentContext(BaseModel):
    principal: BotPrincipal
    party_id: UUID | None
    tutor_discord_id: int
    channel_id: int | None
    channel_state: StudentChannelState
    current_parent_channel_id: int | None

    @classmethod
    def from_parts(
        cls,
        *,
        principal: BotPrincipal,
        workspace: StudentWorkspace,
        party_id: UUID | None,
    ) -> StudentContext:
        return cls(
            principal=principal,
            party_id=party_id,
            tutor_discord_id=workspace.tutor_discord_id,
            channel_id=workspace.channel_id,
            channel_state=workspace.channel_state,
            current_parent_channel_id=workspace.current_parent_channel_id,
        )


BATCH_LOOKUP_LIMIT = 100


class DiscordIdBatchRequest(BaseModel):
    """A bounded batch of discord ids to resolve. De-duplicated at the service boundary."""

    discord_ids: list[Annotated[int, Field(ge=0)]] = Field(min_length=1, max_length=BATCH_LOOKUP_LIMIT)


class PrincipalBatch(BaseModel):
    """Partial-result envelope: resolved principals plus the requested ids that did not resolve."""

    found: list[BotPrincipal]
    missing: list[int]


class TutorContextBatch(BaseModel):
    found: list[TutorContext]
    missing: list[int]


class StudentContextBatch(BaseModel):
    found: list[StudentContext]
    missing: list[int]


class CommandEnvChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    guild_id: int
    channel_id: int
    kind: CommandEnvKind
    owner_discord_id: int | None = None

    @classmethod
    def from_model(cls, command_env: object) -> CommandEnvChannelResponse:
        return cls.model_validate(command_env)


class CommandEnvUpsertRequest(BaseModel):
    guild_id: int
    channel_id: int
    kind: CommandEnvKind
    owner_discord_id: int | None = None


# --- Provisioning (users & account links) -----------------------------------


class DiscordUserUpsertRequest(BaseModel):
    role: MemberRole
    nick_name: str = Field(min_length=1)
    active: bool = True


class DiscordUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    discord_id: int
    role: MemberRole
    nick_name: str
    active: bool

    @classmethod
    def from_model(cls, user: DiscordUser) -> DiscordUserResponse:
        return cls.model_validate(user)


class GroupMembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    discord_id: int
    group_key: str

    @classmethod
    def from_model(cls, membership: DiscordUserPermissionGroup) -> GroupMembershipResponse:
        return cls.model_validate(membership)


# --- Authorization ----------------------------------------------------------


class AuthorizationCheckRequest(BaseModel):
    actor_discord_id: int
    action_key: str = Field(min_length=1)
    target_party_id: UUID


class AuthorizationCheckResponse(BaseModel):
    allowed: bool


# --- Jobs -------------------------------------------------------------------


class JobClaimRequest(BaseModel):
    kinds: list[str] | None = None
    limit: int = Field(default=1, ge=1, le=50)
    worker: str | None = None


class JobFailRequest(BaseModel):
    error: str | None = None
    retry: bool = False


class BotJob(BaseModel):
    """Processing view returned when a job is claimed."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    kind: str
    payload: dict[str, Any]
    claimed_at: datetime | None
    attempt: int

    @classmethod
    def from_model(cls, job: Job) -> BotJob:
        return cls.model_validate(job)


class JobResponse(BaseModel):
    """State view returned after completing or failing a job."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    kind: str
    status: JobStatus
    attempt: int
    available_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    last_error: str | None

    @classmethod
    def from_model(cls, job: Job) -> JobResponse:
        return cls.model_validate(job)


# --- Jobs (read plane) ------------------------------------------------------


class JobListItem(BaseModel):
    """List view of a job. Omits the ``payload`` -- fetch a single job by id for that."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    kind: str
    status: JobStatus
    attempt: int
    max_attempts: int
    available_at: datetime
    claimed_at: datetime | None
    claimed_by: str | None
    completed_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, job: Job) -> JobListItem:
        return cls.model_validate(job)


class JobDetail(BaseModel):
    """Full read view of a single job, including its ``payload``."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    attempt: int
    max_attempts: int
    available_at: datetime
    claimed_at: datetime | None
    claimed_by: str | None
    completed_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, job: Job) -> JobDetail:
        return cls.model_validate(job)


class JobStatusCounts(BaseModel):
    """Job counts for every status in the queue funnel, zero-filled (no silent gaps)."""

    pending: int
    claimed: int
    completed: int
    failed: int

    @classmethod
    def from_counts(cls, counts: dict[JobStatus, int]) -> JobStatusCounts:
        return cls(
            pending=counts[JobStatus.PENDING],
            claimed=counts[JobStatus.CLAIMED],
            completed=counts[JobStatus.COMPLETED],
            failed=counts[JobStatus.FAILED],
        )


class JobKindCounts(BaseModel):
    """Queue funnel for a single job kind: depth (``total``/``open``) and the status breakdown."""

    kind: str
    total: int
    open: int
    statuses: JobStatusCounts

    @classmethod
    def from_counts(cls, kind: str, counts: dict[JobStatus, int]) -> JobKindCounts:
        return cls(
            kind=kind,
            total=sum(counts.values()),
            open=counts[JobStatus.PENDING] + counts[JobStatus.CLAIMED],
            statuses=JobStatusCounts.from_counts(counts),
        )


class JobQueueSummary(BaseModel):
    """Queue funnel: global depth (``total``/``open``) and status counts, plus a per-kind breakdown.

    ``open`` is outstanding work -- ``pending`` (including jobs delayed by retry backoff) plus
    ``claimed``. Terminal counts are historical totals (rows are retained). ``by_kind`` is sorted by
    kind and scoped to whatever ``kind`` filter the summary was fetched with.
    """

    total: int
    open: int
    statuses: JobStatusCounts
    by_kind: list[JobKindCounts]

    @classmethod
    def from_view(cls, view: JobQueueSummaryView) -> JobQueueSummary:
        return cls(
            total=sum(view.by_status.values()),
            open=view.by_status[JobStatus.PENDING] + view.by_status[JobStatus.CLAIMED],
            statuses=JobStatusCounts.from_counts(view.by_status),
            by_kind=[JobKindCounts.from_counts(item.kind, item.counts) for item in view.by_kind],
        )


# --- Transitions (prepare/commit) -------------------------------------------


class TransitionPrepareResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    kind: OperationKind
    expires_at: datetime
    plan: dict[str, Any]

    @classmethod
    def from_model(cls, operation: Operation) -> TransitionPrepareResponse:
        return cls.model_validate(operation)


class TransitionCommitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    kind: OperationKind
    status: OperationStatus
    committed_at: datetime | None

    @classmethod
    def from_model(cls, operation: Operation) -> TransitionCommitResponse:
        return cls.model_validate(operation)


class OperationCancelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    kind: OperationKind
    status: OperationStatus
    cancelled_at: datetime | None

    @classmethod
    def from_model(cls, operation: Operation) -> OperationCancelResponse:
        return cls.model_validate(operation)


class TutorActivationPrepareRequest(BaseModel):
    guild_id: int
    tutor_discord_id: int


class TutorActivationCommitRequest(BaseModel):
    category_channel_id: int
    command_channel_id: int


class StudentActivationPrepareRequest(BaseModel):
    guild_id: int
    student_discord_id: int
    tutor_discord_id: int


class StudentActivationCommitRequest(BaseModel):
    channel_id: int


# --- Operations (read plane) ------------------------------------------------


class OperationSummary(BaseModel):
    """List view of an operation. Omits the heavy ``plan`` -- fetch a single operation by id for that."""

    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    kind: OperationKind
    status: OperationStatus
    guild_id: int
    subject_discord_id: int
    tutor_discord_id: int | None
    reserved_archive_category_channel_id: int | None
    expires_at: datetime
    committed_at: datetime | None
    cancelled_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, operation: Operation) -> OperationSummary:
        return cls.model_validate(operation)


class OperationResponse(BaseModel):
    """Full read view of a single operation, including the two-phase ``plan``."""

    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    kind: OperationKind
    status: OperationStatus
    guild_id: int
    subject_discord_id: int
    tutor_discord_id: int | None
    reserved_archive_category_channel_id: int | None
    plan: dict[str, Any]
    expires_at: datetime
    committed_at: datetime | None
    cancelled_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, operation: Operation) -> OperationResponse:
        return cls.model_validate(operation)
