import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.auth import AuthSettings, Scope, validate_access_token
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientScopeGrant,
    ApplicationClientSecret,
    ApplicationClientStatus,
    AuthAuditLog,
    PermissionScope,
)
from app.services.auth import (
    ApplicationClientAlreadyExistsError,
    ApplicationClientScopeGrantNotFoundError,
    ApplicationClientSecretNotFoundError,
    BootstrappedApplicationClient,
    InvalidClientCredentialsError,
    InvalidClientScopeError,
    bootstrap_application_client,
    create_application_client,
    create_application_client_secret,
    create_client_secret,
    grant_application_client_scopes,
    issue_client_token,
    list_application_clients,
    revoke_application_client_scope,
    revoke_application_client_secret,
    seed_default_scopes,
    update_application_client,
)


@pytest.mark.db
async def test_seed_default_scopes_is_idempotent(session):
    first_seed = await seed_default_scopes(session)
    second_seed = await seed_default_scopes(session)
    scopes = (await session.execute(select(PermissionScope))).scalars().all()

    expected = {
        "auth:clients:manage",
        "auth:discord-links:read",
        "auth:users:login",
        "auth:users:manage",
        "account:self",
        "bot:read",
        "bot:write",
        "crm:read",
        "crm:read:own",
        "crm:write",
    }
    assert {scope.key for scope in first_seed} == expected
    assert {scope.key for scope in second_seed} == expected
    assert len(scopes) == len(expected)
    assert all(scope.active for scope in scopes)


@pytest.mark.db
async def test_bootstrap_skillbot_client_creates_client_secret_and_grants(session):
    result = await _bootstrap_skillbot_client(session)
    audit_logs = (await session.execute(select(AuthAuditLog))).scalars().all()
    grants = (await session.execute(select(ApplicationClientScopeGrant))).scalars().all()

    assert result.created_client is True
    assert result.created_secret is not None
    assert result.created_secret.plaintext not in result.created_secret.secret.secret_hash
    assert result.client.client_id == "skillbot"
    assert result.client.status == ApplicationClientStatus.ACTIVE
    assert result.granted_scopes == frozenset({"bot:read", "bot:write"})
    assert sorted(grant.scope_key for grant in grants) == ["bot:read", "bot:write"]
    assert [log.event_type for log in audit_logs] == [
        "application_client.created",
        "scope_grant.added",
        "scope_grant.added",
        "client_secret.created",
    ]


@pytest.mark.db
async def test_bootstrap_skillbot_client_is_idempotent_when_usable_secret_exists(session):
    first_result = await _bootstrap_skillbot_client(session)
    second_result = await _bootstrap_skillbot_client(session)
    clients = (await session.execute(select(ApplicationClient))).scalars().all()
    secrets = (await session.execute(select(ApplicationClientSecret))).scalars().all()
    grants = (await session.execute(select(ApplicationClientScopeGrant))).scalars().all()

    assert first_result.created_secret is not None
    assert second_result.created_client is False
    assert second_result.created_secret is None
    assert len(clients) == 1
    assert len(secrets) == 1
    assert len(grants) == 2


@pytest.mark.db
async def test_bootstrap_skillbot_client_creates_new_secret_when_existing_is_revoked(session):
    first_result = await _bootstrap_skillbot_client(session)
    assert first_result.created_secret is not None
    first_result.created_secret.secret.revoked_at = datetime.now(UTC)
    await session.flush()

    second_result = await _bootstrap_skillbot_client(session)
    secrets = (await session.execute(select(ApplicationClientSecret))).scalars().all()

    assert second_result.created_secret is not None
    assert len(secrets) == 2


@pytest.mark.db
async def test_bootstrap_skillbot_client_can_bootstrap_custom_scopes(session):
    result = await _bootstrap_skillbot_client(session, scopes=[Scope.BOT_READ])
    grants = (await session.execute(select(ApplicationClientScopeGrant))).scalars().all()

    assert result.granted_scopes == frozenset({"bot:read"})
    assert [grant.scope_key for grant in grants] == ["bot:read"]


@pytest.mark.db
async def test_create_application_client_lists_client_and_writes_audit(session):
    client = await create_application_client(
        session,
        client_id="integration",
        name="Integration",
        description="External integration",
    )
    clients, total = await list_application_clients(session)
    audit_logs = (await session.execute(select(AuthAuditLog))).scalars().all()

    assert client.client_id == "integration"
    assert [client.client_id for client in clients] == ["integration"]
    assert total == 1
    assert [(log.event_type, log.success) for log in audit_logs] == [("application_client.created", True)]


@pytest.mark.db
async def test_list_application_clients_pages_by_client_id_and_counts_all(session):
    for client_id in ("delta", "alpha", "charlie", "bravo"):
        await create_application_client(session, client_id=client_id, name=client_id.title())

    page, total = await list_application_clients(session, limit=2, offset=1)

    assert [client.client_id for client in page] == ["bravo", "charlie"]
    assert total == 4


@pytest.mark.db
async def test_create_application_client_rejects_duplicate_client_id(session):
    await create_application_client(session, client_id="integration", name="Integration")

    with pytest.raises(ApplicationClientAlreadyExistsError):
        await create_application_client(session, client_id="integration", name="Integration 2")


@pytest.mark.db
async def test_update_application_client_can_disable_and_clear_description(session):
    await create_application_client(session, client_id="integration", name="Integration", description="old")

    client = await update_application_client(
        session,
        client_id="integration",
        name="Integration API",
        description=None,
        update_description=True,
        status=ApplicationClientStatus.DISABLED,
    )
    audit_logs = (await session.execute(select(AuthAuditLog))).scalars().all()

    assert client.name == "Integration API"
    assert client.description is None
    assert client.status == ApplicationClientStatus.DISABLED
    assert [log.event_type for log in audit_logs] == [
        "application_client.created",
        "application_client.disabled",
    ]


@pytest.mark.db
async def test_create_application_client_secret_and_revoke_by_client_id(session):
    client = await create_application_client(session, client_id="integration", name="Integration")
    created_secret = await create_application_client_secret(session, client_id="integration", label="primary")

    await revoke_application_client_secret(session, client_id="integration", secret_id=created_secret.secret.id)
    audit_logs = (await session.execute(select(AuthAuditLog))).scalars().all()

    assert created_secret.plaintext not in created_secret.secret.secret_hash
    assert created_secret.secret.application_client_id == client.id
    assert created_secret.secret.revoked_at is not None
    assert [log.event_type for log in audit_logs] == [
        "application_client.created",
        "client_secret.created",
        "client_secret.revoked",
    ]


@pytest.mark.db
async def test_revoke_application_client_secret_rejects_unknown_secret(session):
    await create_application_client(session, client_id="integration", name="Integration")

    with pytest.raises(ApplicationClientSecretNotFoundError):
        await revoke_application_client_secret(
            session,
            client_id="integration",
            secret_id=uuid.uuid4(),
        )


@pytest.mark.db
async def test_grant_and_revoke_application_client_scopes(session):
    await seed_default_scopes(session)
    await create_application_client(session, client_id="integration", name="Integration")

    client = await grant_application_client_scopes(
        session,
        client_id="integration",
        scopes=[Scope.BOT_READ, Scope.BOT_WRITE],
    )
    await revoke_application_client_scope(session, client_id="integration", scope_key=Scope.BOT_READ.value)
    grants = (await session.execute(select(ApplicationClientScopeGrant))).scalars().all()
    audit_logs = (await session.execute(select(AuthAuditLog))).scalars().all()

    assert sorted(grant.scope_key for grant in client.scope_grants) == ["bot:read", "bot:write"]
    assert [grant.scope_key for grant in grants] == ["bot:write"]
    assert [log.event_type for log in audit_logs] == [
        "application_client.created",
        "scope_grant.added",
        "scope_grant.added",
        "scope_grant.removed",
    ]


@pytest.mark.db
async def test_revoke_application_client_scope_rejects_unknown_grant(session):
    await create_application_client(session, client_id="integration", name="Integration")

    with pytest.raises(ApplicationClientScopeGrantNotFoundError):
        await revoke_application_client_scope(session, client_id="integration", scope_key="bot:read")


@pytest.mark.db
async def test_issue_client_token_returns_token_for_valid_credentials(session):
    client, secret, plaintext_secret = await _create_client_with_secret_and_scopes(
        session,
        scopes=["bot:read", "bot:write"],
    )

    token = await issue_client_token(
        session,
        _settings(),
        client_id=client.client_id,
        client_secret=plaintext_secret,
        requested_scopes=["bot:write"],
        now=datetime.now(UTC),
    )
    principal = validate_access_token(token.access_token, _settings())
    audit_logs = await _token_audit_logs(session)

    assert token.scope == "bot:write"
    assert principal.principal_id == client.id
    assert principal.client_id == client.client_id
    assert principal.scopes == frozenset({"bot:write"})
    assert secret.last_used_at is not None
    assert [(log.event_type, log.success) for log in audit_logs] == [("token.issued", True)]


@pytest.mark.db
async def test_issue_client_token_uses_all_granted_active_scopes_when_none_requested(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(
        session,
        scopes=["bot:write", "bot:read"],
    )

    token = await issue_client_token(
        session,
        _settings(),
        client_id=client.client_id,
        client_secret=plaintext_secret,
    )

    assert token.scope == "bot:read bot:write"


@pytest.mark.db
async def test_issue_client_token_accepts_space_separated_requested_scopes(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(
        session,
        scopes=["bot:write", "bot:read"],
    )

    token = await issue_client_token(
        session,
        _settings(),
        client_id=client.client_id,
        client_secret=plaintext_secret,
        requested_scopes="bot:write bot:read",
    )

    assert token.scope == "bot:read bot:write"


@pytest.mark.db
async def test_issue_client_token_grants_the_own_variant_of_a_granted_scope(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(session, scopes=["crm:read"])

    token = await issue_client_token(
        session,
        _settings(),
        client_id=client.client_id,
        client_secret=plaintext_secret,
        requested_scopes="crm:read:own",
    )

    assert token.scope == "crm:read:own"
    assert validate_access_token(token.access_token, _settings()).scopes == frozenset({"crm:read:own"})


@pytest.mark.db
async def test_issue_client_token_rejects_invalid_secret(session):
    client, _secret, _plaintext_secret = await _create_client_with_secret_and_scopes(session, scopes=["bot:read"])

    with pytest.raises(InvalidClientCredentialsError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret="wrong-secret",
        )

    audit_logs = await _token_audit_logs(session)
    assert [(log.event_type, log.success) for log in audit_logs] == [("token.denied", False)]


@pytest.mark.db
async def test_issue_client_token_rejects_disabled_client(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(session, scopes=["bot:read"])
    client.status = ApplicationClientStatus.DISABLED
    await session.flush()

    with pytest.raises(InvalidClientCredentialsError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret=plaintext_secret,
        )


@pytest.mark.db
async def test_issue_client_token_rejects_revoked_secret(session):
    client, secret, plaintext_secret = await _create_client_with_secret_and_scopes(session, scopes=["bot:read"])
    secret.revoked_at = datetime.now(UTC)
    await session.flush()

    with pytest.raises(InvalidClientCredentialsError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret=plaintext_secret,
        )


@pytest.mark.db
async def test_issue_client_token_rejects_expired_secret(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(
        session,
        scopes=["bot:read"],
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    with pytest.raises(InvalidClientCredentialsError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret=plaintext_secret,
        )


@pytest.mark.db
async def test_issue_client_token_rejects_ungranted_scope(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(session, scopes=["bot:read"])

    with pytest.raises(InvalidClientScopeError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret=plaintext_secret,
            requested_scopes=["bot:write"],
        )


@pytest.mark.db
async def test_issue_client_token_ignores_inactive_grants(session):
    client, _secret, plaintext_secret = await _create_client_with_secret_and_scopes(
        session,
        scopes=["bot:read"],
        active=False,
    )

    with pytest.raises(InvalidClientScopeError):
        await issue_client_token(
            session,
            _settings(),
            client_id=client.client_id,
            client_secret=plaintext_secret,
        )


async def _create_client_with_secret_and_scopes(
    session,
    *,
    scopes: list[str],
    active: bool = True,
    expires_at: datetime | None = None,
) -> tuple[ApplicationClient, ApplicationClientSecret, str]:
    client = ApplicationClient(client_id="skillbot", name="SkillBot")
    session.add(client)
    await session.flush()

    created_secret = await create_client_secret(
        session,
        application_client_id=client.id,
        expires_at=expires_at,
    )

    for scope_key in scopes:
        permission_scope = PermissionScope(
            key=scope_key,
            description=f"{scope_key} scope",
            active=active,
        )
        session.add(permission_scope)
        session.add(ApplicationClientScopeGrant(application_client=client, permission_scope=permission_scope))

    await session.flush()
    return client, created_secret.secret, created_secret.plaintext


async def _bootstrap_skillbot_client(session, *, scopes: list[Scope] | None = None) -> BootstrappedApplicationClient:
    return await bootstrap_application_client(
        session,
        client_id="skillbot",
        name="SkillBot",
        description="Discord Bot",
        scopes=scopes or (Scope.BOT_READ, Scope.BOT_WRITE),
    )


def _settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))


async def _token_audit_logs(session) -> list[AuthAuditLog]:
    return (
        (
            await session.execute(
                select(AuthAuditLog).where(AuthAuditLog.event_type.in_(["token.issued", "token.denied"]))
            )
        )
        .scalars()
        .all()
    )
