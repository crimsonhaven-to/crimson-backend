"""Every environment-driven setting, loaded once.

Route handlers take ``Settings`` through ``Depends(get_settings)``; background
jobs and engines call ``get_settings()`` at the point of use. Nothing else reads
the environment, with one exception: ``resolvers._proxy_secret`` looks up the
per-source secret names an overlay module passes it.
"""

from functools import lru_cache
from typing import Annotated, Optional

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Into os.environ rather than pydantic's env_file, because overlay modules read
# their own variables straight from the environment.
load_dotenv()

DEFAULT_ORIGINS = ["https://crimsonhaven.to", "https://www.crimsonhaven.to"]

CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    tmdb_api_key: str = ""

    # --- database ---------------------------------------------------------
    database_url: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "crimson"
    postgres_user: str = "crimson"
    postgres_password: str = "crimson"
    db_pool_min: int = Field(1, ge=0)
    db_pool_max: int = Field(10, ge=1)
    db_connect_timeout: float = 30.0
    # Off by default so transaction-mode PgBouncer is safe; see core/db_pool.py.
    db_prepare_threshold: Optional[int] = None

    # --- process roles ----------------------------------------------------
    # The mapping sync rebuilds tables wholesale, so exactly one replica runs it.
    run_db_sync: bool = True
    # The cache and download workers each run on one dedicated service. Both
    # default on so a single-container deploy works without extra config.
    run_cache_worker: bool = True
    run_download_worker: bool = True

    # --- access -----------------------------------------------------------
    require_login: bool = True
    allowed_origins: CommaList = DEFAULT_ORIGINS
    # Seeds the first admin; only promotes accounts that already exist.
    admin_emails: CommaList = []
    signup_invite_code: CommaList = []
    rate_limit_storage_uri: str = "memory://"
    debug: bool = False

    # Open signup, with every non-admin account wiped nightly at this hour (UTC).
    demo_mode: bool = False
    demo_reset_hour: int = Field(4, ge=0, le=23)

    # --- metadata maintenance ----------------------------------------------
    # Each night the oldest 1/buckets of the TMDB tables is re-pulled, since TMDB
    # never says when a row changed.
    metadata_refresh_buckets: int = Field(14, ge=1)
    metadata_refresh_hour: int = Field(4, ge=0, le=23)
    run_metadata_backfill: bool = False
    metadata_backfill_pages: int = Field(100, ge=1)

    # --- email ------------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = "CrimsonHaven"
    frontend_base_url: str = "https://crimsonhaven.to"
    # Off by default: a wrong send is the one thing a redeploy cannot take back.
    airing_notify_enabled: bool = False
    airing_notify_dry_run: bool = False

    security_events_retention_days: int = Field(90, ge=1)

    # --- Lumi -------------------------------------------------------------
    # Only the keys live here, so a database dump never carries billable
    # credentials. Everything else is operator state in chat_settings.
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = Field(
        None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )

    # --- signed proxies -----------------------------------------------------
    # Must be identical on every replica, or a link minted by one fails on the next.
    proxy_secret: str = ""
    crimson_proxy_base: CommaList = []
    private_sources_enabled: bool = True

    # --- sources ----------------------------------------------------------
    jellyfin_url: str = ""
    jellyfin_username: str = ""
    jellyfin_password: Optional[str] = None
    jellyfin_edge_inject: bool = False

    opensubtitles_api_key: str = ""
    opensubtitles_app_name: str = "CrimsonHaven v1.0"
    subtitles_search_ttl: float = 3600.0

    manga_enabled: bool = True
    manga_languages: CommaList = ["en"]
    manga_content_rating: CommaList = ["safe", "suggestive", "erotica"]

    iptv_enabled: bool = True
    iptv_include_nsfw: bool = False
    iptv_refresh_hours: float = Field(12.0, ge=1.0)

    # --- server-side cache and downloads ----------------------------------
    cache_internal_base: str = "http://127.0.0.1:8000"
    cache_max_concurrent: int = Field(1, ge=1)
    cache_download_timeout: int = 3600
    cache_poll_interval: int = Field(10, ge=2)
    cache_min_free_bytes: int = 2 * 1024**3

    download_max_active: int = Field(3, ge=1)
    download_poll_interval: int = Field(5, ge=2)
    download_min_free_bytes: int = 2 * 1024**3
    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = ""

    # --- observability ----------------------------------------------------
    log_format: str = "plain"
    metrics_token: str = ""
    prometheus_url: str = ""

    # The source-health probe asks every source for this title.
    health_canary_title: str = "Attack on Titan"
    health_canary_tmdb: int = 1429
    health_canary_season: int = 1
    health_canary_episode: int = 1
    health_canary_anilist: int = 16498

    # --- integrations -----------------------------------------------------
    kofi_verification_token: str = ""
    kofi_active_window_days: int = 35
    kofi_list_cache_ttl: int = 60

    github_repo: str = "crimsonhaven-to/crimson-backend"
    github_token: str = ""
    changelog_max_entries: int = Field(30, ge=1, le=100)
    changelog_cache_ttl: int = Field(1800, ge=0)
    changelog_include_prereleases: bool = True

    discord_bot_token: str = ""
    discord_owner_id: str = ""
    discord_command_prefix: str = "!"

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "allowed_origins",
        "admin_emails",
        "signup_invite_code",
        "crimson_proxy_base",
        "manga_languages",
        "manga_content_rating",
        mode="before",
    )
    @classmethod
    def _split_commas(cls, value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _default_origins(cls, value: list[str]) -> list[str]:
        return value or DEFAULT_ORIGINS

    @field_validator("crimson_proxy_base")
    @classmethod
    def _strip_slashes(cls, value: list[str]) -> list[str]:
        return [base.rstrip("/") for base in value]

    @field_validator("db_prepare_threshold", mode="before")
    @classmethod
    def _disabled_is_none(cls, value):
        if isinstance(value, str) and value.strip().lower() in ("none", "disabled", "off"):
            return None
        return value

    @field_validator("log_format", "smtp_security", mode="before")
    @classmethod
    def _lowercase(cls, value):
        return value.lower() if isinstance(value, str) else value

    @field_validator("frontend_base_url", "cache_internal_base", "aria2_rpc_url", "jellyfin_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
