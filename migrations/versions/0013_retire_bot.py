"""retire the bot schema and the bot scopes

Revision ID: 0013_retire_bot
Revises: 0012_auth_expiry_indexes
Create Date: 2026-09-27 00:00:00.000000

SkillBot keeps its Discord state in its own database now (ADR 0009; bot-decoupling spec, decision M). A frozen
snapshot: this revision imports no app code, so it keeps working without the bot models.

Upgrade: every ``bot:*`` grant goes, with one ``scope_grant.removed`` audit row each (the detail
``revoke_application_client_scope`` writes); then the two scope rows, the 14 tables, the 9 enum types and the
schema itself. ``DROP SCHEMA`` runs without ``CASCADE``: an object this revision does not know fails the upgrade,
and the whole migration rolls back.

Downgrade: the empty structure of 0011 - every column, default, check, foreign key and index under its original
name, taken from ``pg_dump --schema-only --schema=bot`` at 0011 - and the two scope rows. No data and no grants:
what the upgrade deleted comes back only from a backup (the Neon branch of the pre-flight).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_retire_bot"
down_revision: str | None = "0012_auth_expiry_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Children first: every table goes before the tables it references.
TABLES = (
    "student_workspace",
    "tutor_workspace",
    "command_env_channel",
    "archive_category",
    "discord_user_permission_group",
    "discord_role_binding",
    "discord_channel",
    "discord_user",
    "discord_guild",
    "permission_group",
    "permission_grant",
    "app_command_audit_log",
    "job",
    "operation",
)

# The labels of 0011, in their sort order.
ENUM_TYPES = {
    "member_role": ("admin", "tutor", "student"),
    "discord_channel_type": ("category", "text", "voice", "thread", "forum"),
    "student_channel_state": ("tutor_category", "archive_category"),
    "command_env_kind": ("admin_cmd", "tutor_cmd"),
    "permission_subject_type": ("role", "group", "user"),
    "permission_grant_effect": ("allow", "deny"),
    "job_status": ("pending", "claimed", "completed", "failed"),
    "operation_kind": (
        "tutor_activate",
        "student_activate",
        "student_stash",
        "student_pop",
        "student_deactivate",
        "tutor_deactivate",
    ),
    "operation_status": ("prepared", "committed", "expired", "failed", "cancelled"),
}

# Every grant of a bot scope, deleted with the audit row revoke_application_client_scope writes for a removal.
DELETE_BOT_GRANTS = """
WITH removed AS (
    DELETE FROM auth.application_client_scope_grant AS g
    USING auth.application_client AS c
    WHERE c.id = g.application_client_id AND g.scope_key LIKE 'bot:%'
    RETURNING c.id, c.client_id, g.scope_key, g.mode
)
INSERT INTO auth.auth_audit_log (id, principal_type, principal_id, event_type, success, detail)
SELECT gen_random_uuid(), 'application', removed.id::text, 'scope_grant.removed', true,
       'Removed scope ' || removed.scope_key || ' in ' || removed.mode::text || ' mode from application client '
       || removed.client_id || '.'
FROM removed
"""

# The 0011 tables, parents first.
CREATE_TABLES = (
    """
    CREATE TABLE bot.operation (
        operation_id uuid NOT NULL,
        kind bot.operation_kind NOT NULL,
        status bot.operation_status DEFAULT 'prepared'::bot.operation_status NOT NULL,
        guild_id bigint NOT NULL,
        subject_discord_id bigint NOT NULL,
        tutor_discord_id bigint,
        reserved_archive_category_channel_id bigint,
        plan jsonb DEFAULT '{}'::jsonb NOT NULL,
        expires_at timestamp with time zone NOT NULL,
        committed_at timestamp with time zone,
        failed_at timestamp with time zone,
        last_error text,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        cancelled_at timestamp with time zone,
        CONSTRAINT operation_pkey PRIMARY KEY (operation_id)
    )
    """,
    """
    CREATE TABLE bot.job (
        job_id uuid NOT NULL,
        kind text NOT NULL,
        payload jsonb DEFAULT '{}'::jsonb NOT NULL,
        status bot.job_status DEFAULT 'pending'::bot.job_status NOT NULL,
        attempt smallint DEFAULT 0 NOT NULL,
        max_attempts smallint DEFAULT 5 NOT NULL,
        available_at timestamp with time zone DEFAULT now() NOT NULL,
        claimed_at timestamp with time zone,
        claimed_by text,
        completed_at timestamp with time zone,
        failed_at timestamp with time zone,
        last_error text,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT job_pkey PRIMARY KEY (job_id),
        CONSTRAINT ck_job_attempt_non_negative CHECK (attempt >= 0),
        CONSTRAINT ck_job_max_attempts_positive CHECK (max_attempts >= 1)
    )
    """,
    """
    CREATE TABLE bot.app_command_audit_log (
        id uuid NOT NULL,
        guild_id bigint,
        channel_id bigint,
        discord_id bigint,
        command_name text NOT NULL,
        permission_action text,
        permission_allowed boolean,
        permission_source text,
        permission_reason text,
        error_type text,
        error text,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT app_command_audit_log_pkey PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE bot.permission_grant (
        id uuid NOT NULL,
        subject_type bot.permission_subject_type NOT NULL,
        subject_key text NOT NULL,
        action_key text NOT NULL,
        effect bot.permission_grant_effect NOT NULL,
        priority integer DEFAULT 0 NOT NULL,
        active boolean DEFAULT true NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT permission_grant_pkey PRIMARY KEY (id),
        CONSTRAINT uq_permission_grant_subject_action UNIQUE (subject_type, subject_key, action_key)
    )
    """,
    """
    CREATE TABLE bot.permission_group (
        key text NOT NULL,
        name text NOT NULL,
        active boolean DEFAULT true NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT permission_group_pkey PRIMARY KEY (key)
    )
    """,
    """
    CREATE TABLE bot.discord_guild (
        guild_id bigint NOT NULL,
        name text,
        is_primary boolean DEFAULT false NOT NULL,
        active boolean DEFAULT true NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT discord_guild_pkey PRIMARY KEY (guild_id)
    )
    """,
    """
    CREATE TABLE bot.discord_user (
        discord_id bigint NOT NULL,
        role bot.member_role NOT NULL,
        nick_name text NOT NULL,
        active boolean DEFAULT true NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT discord_user_pkey PRIMARY KEY (discord_id),
        CONSTRAINT ck_discord_user_nick_name_not_empty CHECK (nick_name <> ''::text)
    )
    """,
    """
    CREATE TABLE bot.discord_channel (
        channel_id bigint NOT NULL,
        guild_id bigint NOT NULL,
        parent_channel_id bigint,
        type bot.discord_channel_type NOT NULL,
        name text,
        managed_by_bot boolean DEFAULT true NOT NULL,
        deleted_at timestamp with time zone,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT discord_channel_pkey PRIMARY KEY (channel_id),
        CONSTRAINT uq_discord_channel_guild_id_channel_id UNIQUE (guild_id, channel_id),
        CONSTRAINT discord_channel_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE,
        CONSTRAINT fk_discord_channel_parent FOREIGN KEY (guild_id, parent_channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE SET NULL (parent_channel_id)
    )
    """,
    """
    CREATE TABLE bot.discord_role_binding (
        guild_id bigint NOT NULL,
        member_role bot.member_role NOT NULL,
        role_id bigint,
        role_name text NOT NULL,
        active boolean DEFAULT true NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT discord_role_binding_pkey PRIMARY KEY (guild_id, member_role),
        CONSTRAINT discord_role_binding_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE bot.discord_user_permission_group (
        discord_id bigint NOT NULL,
        group_key text NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT discord_user_permission_group_pkey PRIMARY KEY (discord_id, group_key),
        CONSTRAINT discord_user_permission_group_discord_id_fkey FOREIGN KEY (discord_id)
            REFERENCES bot.discord_user (discord_id) ON DELETE CASCADE,
        CONSTRAINT discord_user_permission_group_group_key_fkey FOREIGN KEY (group_key)
            REFERENCES bot.permission_group (key) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE bot.archive_category (
        guild_id bigint NOT NULL,
        archive_no integer NOT NULL,
        category_channel_id bigint NOT NULL,
        capacity smallint DEFAULT 50 NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT archive_category_pkey PRIMARY KEY (guild_id, archive_no),
        CONSTRAINT uq_archive_category_category_channel_id UNIQUE (category_channel_id),
        CONSTRAINT uq_archive_category_guild_id_category_channel_id UNIQUE (guild_id, category_channel_id),
        CONSTRAINT ck_archive_category_archive_no_positive CHECK (archive_no > 0),
        CONSTRAINT ck_archive_category_capacity CHECK (capacity >= 1 AND capacity <= 50),
        CONSTRAINT archive_category_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE,
        CONSTRAINT fk_archive_category_channel FOREIGN KEY (guild_id, category_channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE bot.command_env_channel (
        guild_id bigint NOT NULL,
        channel_id bigint NOT NULL,
        kind bot.command_env_kind NOT NULL,
        owner_discord_id bigint,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT command_env_channel_pkey PRIMARY KEY (guild_id, channel_id, kind),
        CONSTRAINT command_env_channel_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE,
        CONSTRAINT command_env_channel_owner_discord_id_fkey FOREIGN KEY (owner_discord_id)
            REFERENCES bot.discord_user (discord_id) ON DELETE CASCADE,
        CONSTRAINT fk_command_env_channel_channel FOREIGN KEY (guild_id, channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE bot.tutor_workspace (
        guild_id bigint NOT NULL,
        tutor_discord_id bigint NOT NULL,
        category_channel_id bigint NOT NULL,
        command_channel_id bigint NOT NULL,
        student_channel_capacity smallint DEFAULT 49 NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT tutor_workspace_pkey PRIMARY KEY (guild_id, tutor_discord_id),
        CONSTRAINT uq_tutor_workspace_category_channel_id UNIQUE (category_channel_id),
        CONSTRAINT uq_tutor_workspace_command_channel_id UNIQUE (command_channel_id),
        CONSTRAINT ck_tutor_workspace_student_channel_capacity
            CHECK (student_channel_capacity >= 0 AND student_channel_capacity <= 49),
        CONSTRAINT tutor_workspace_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE,
        CONSTRAINT tutor_workspace_tutor_discord_id_fkey FOREIGN KEY (tutor_discord_id)
            REFERENCES bot.discord_user (discord_id) ON DELETE CASCADE,
        CONSTRAINT fk_tutor_workspace_category_channel FOREIGN KEY (guild_id, category_channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE RESTRICT,
        CONSTRAINT fk_tutor_workspace_command_channel FOREIGN KEY (guild_id, command_channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE bot.student_workspace (
        guild_id bigint NOT NULL,
        student_discord_id bigint NOT NULL,
        tutor_discord_id bigint NOT NULL,
        channel_id bigint,
        channel_state bot.student_channel_state DEFAULT 'tutor_category'::bot.student_channel_state NOT NULL,
        current_parent_channel_id bigint,
        archive_category_channel_id bigint,
        stashed_at timestamp with time zone,
        popped_at timestamp with time zone,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        CONSTRAINT student_workspace_pkey PRIMARY KEY (guild_id, student_discord_id),
        CONSTRAINT uq_student_workspace_channel_id UNIQUE (channel_id),
        CONSTRAINT ck_student_workspace_archive_category_matches_state CHECK (
            (channel_state = 'tutor_category'::bot.student_channel_state AND archive_category_channel_id IS NULL)
            OR (channel_state = 'archive_category'::bot.student_channel_state AND archive_category_channel_id IS NOT NULL)
        ),
        CONSTRAINT ck_student_workspace_archive_parent CHECK (
            channel_state <> 'archive_category'::bot.student_channel_state
            OR current_parent_channel_id = archive_category_channel_id
        ),
        CONSTRAINT ck_student_workspace_archive_requires_channel CHECK (
            channel_state <> 'archive_category'::bot.student_channel_state OR channel_id IS NOT NULL
        ),
        CONSTRAINT ck_student_workspace_tutor_category_parent CHECK (
            channel_state <> 'tutor_category'::bot.student_channel_state
            OR channel_id IS NULL
            OR current_parent_channel_id IS NOT NULL
        ),
        CONSTRAINT student_workspace_guild_id_fkey FOREIGN KEY (guild_id)
            REFERENCES bot.discord_guild (guild_id) ON DELETE CASCADE,
        CONSTRAINT student_workspace_student_discord_id_fkey FOREIGN KEY (student_discord_id)
            REFERENCES bot.discord_user (discord_id) ON DELETE CASCADE,
        CONSTRAINT student_workspace_tutor_discord_id_fkey FOREIGN KEY (tutor_discord_id)
            REFERENCES bot.discord_user (discord_id) ON DELETE RESTRICT,
        CONSTRAINT fk_student_workspace_archive_category FOREIGN KEY (guild_id, archive_category_channel_id)
            REFERENCES bot.archive_category (guild_id, category_channel_id) ON DELETE RESTRICT,
        CONSTRAINT fk_student_workspace_channel FOREIGN KEY (guild_id, channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE SET NULL (channel_id),
        CONSTRAINT fk_student_workspace_current_parent_channel FOREIGN KEY (guild_id, current_parent_channel_id)
            REFERENCES bot.discord_channel (guild_id, channel_id) ON DELETE RESTRICT
    )
    """,
)

# The 0011 indexes that no constraint creates.
CREATE_INDEXES = (
    "CREATE INDEX ix_operation_reservation ON bot.operation (guild_id, reserved_archive_category_channel_id)"
    " WHERE status = 'prepared'::bot.operation_status AND reserved_archive_category_channel_id IS NOT NULL",
    "CREATE INDEX ix_operation_status_expires_at ON bot.operation (status, expires_at)",
    "CREATE INDEX ix_operation_subject ON bot.operation (guild_id, subject_discord_id, status)",
    "CREATE UNIQUE INDEX uq_operation_prepared_subject_kind ON bot.operation (guild_id, subject_discord_id, kind)"
    " WHERE status = 'prepared'::bot.operation_status",
    "CREATE INDEX ix_job_claimable ON bot.job (available_at) WHERE status = 'pending'::bot.job_status",
    "CREATE INDEX ix_job_kind_status ON bot.job (kind, status)",
    "CREATE INDEX ix_app_command_audit_log_created_at ON bot.app_command_audit_log (created_at)",
    "CREATE INDEX ix_app_command_audit_log_discord_id_created_at ON bot.app_command_audit_log (discord_id, created_at)",
    "CREATE INDEX ix_app_command_audit_log_guild_id_command_name_created_at"
    " ON bot.app_command_audit_log (guild_id, command_name, created_at)",
    "CREATE INDEX ix_permission_grant_action_key ON bot.permission_grant (action_key)",
    "CREATE INDEX ix_permission_grant_subject_type_subject_key_active"
    " ON bot.permission_grant (subject_type, subject_key, active)",
    "CREATE UNIQUE INDEX uq_discord_guild_primary_active ON bot.discord_guild (is_primary)"
    " WHERE is_primary IS TRUE AND active IS TRUE",
    "CREATE INDEX ix_discord_user_role_active ON bot.discord_user (role, active)",
    "CREATE INDEX ix_discord_channel_guild_id_deleted_at ON bot.discord_channel (guild_id, deleted_at)",
    "CREATE INDEX ix_discord_channel_parent_channel_id ON bot.discord_channel (parent_channel_id)",
    "CREATE INDEX ix_archive_category_guild_id ON bot.archive_category (guild_id)",
    "CREATE INDEX ix_command_env_channel_guild_id_kind ON bot.command_env_channel (guild_id, kind)",
    "CREATE INDEX ix_command_env_channel_guild_id_owner_discord_id_kind"
    " ON bot.command_env_channel (guild_id, owner_discord_id, kind)",
    "CREATE UNIQUE INDEX uq_command_env_channel_guild_id_owner_discord_id_kind"
    " ON bot.command_env_channel (guild_id, owner_discord_id, kind) WHERE owner_discord_id IS NOT NULL",
    "CREATE INDEX ix_student_workspace_archive_category_channel_id_channel_state"
    " ON bot.student_workspace (archive_category_channel_id, channel_state)",
    "CREATE INDEX ix_student_workspace_guild_id_channel_state ON bot.student_workspace (guild_id, channel_state)",
    "CREATE INDEX ix_student_workspace_guild_id_tutor_discord_id ON bot.student_workspace (guild_id, tutor_discord_id)",
    "CREATE INDEX ix_student_workspace_guild_id_tutor_discord_id_channel_state"
    " ON bot.student_workspace (guild_id, tutor_discord_id, channel_state)",
)


def upgrade() -> None:
    # Queue at most 15 s behind a running transaction - and never make every later query queue behind this one.
    op.execute(sa.text("SET LOCAL lock_timeout = '15s'"))
    op.execute(sa.text(f"LOCK TABLE {', '.join(f'bot.{table}' for table in TABLES)} IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text(DELETE_BOT_GRANTS))
    op.execute(sa.text("DELETE FROM auth.permission_scope WHERE key IN ('bot:read', 'bot:write')"))
    for table in TABLES:
        op.execute(sa.text(f"DROP TABLE bot.{table}"))
    for name in ENUM_TYPES:
        op.execute(sa.text(f"DROP TYPE bot.{name}"))
    # No CASCADE: an object this revision does not know fails here, and the whole migration rolls back.
    op.execute(sa.text("DROP SCHEMA bot"))


def downgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA bot"))
    for name, labels in ENUM_TYPES.items():
        values = ", ".join(f"'{label}'" for label in labels)
        op.execute(sa.text(f"CREATE TYPE bot.{name} AS ENUM ({values})"))
    for statement in (*CREATE_TABLES, *CREATE_INDEXES):
        op.execute(sa.text(statement))
    op.execute(
        sa.text(
            "INSERT INTO auth.permission_scope (key, description) VALUES "
            "('bot:read', 'Read bot API surface.'), ('bot:write', 'Write bot API surface.') "
            "ON CONFLICT (key) DO NOTHING"
        )
    )
