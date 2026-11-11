from pydantic import PostgresDsn
from pydantic_settings import SettingsConfigDict
from skillcore.config import CoreSettings


class DatabaseSettings(CoreSettings):
    """
    Database settings loaded from environment/.env.

    Expected keys:
        - DB__URL=postgresql+asyncpg://...
        - DB__MIGRATION_URL=postgresql+asyncpg://...  (optional)

    DB__MIGRATION_URL lets migrations target a direct, unpooled endpoint. A pooled
    endpoint (e.g. Neon's "-pooler" host running PgBouncer in transaction mode) breaks
    advisory locks and CONCURRENTLY operations, so it is unsafe for migrations; the app
    itself keeps using the pooled DB__URL. Falls back to DB__URL when unset.
    """

    url: PostgresDsn
    migration_url: PostgresDsn | None = None

    # Migrations build these settings on their own, and a URL holds a password: no validation error repeats an input.
    model_config = SettingsConfigDict(
        env_prefix="DB__",
        hide_input_in_errors=True,
    )
