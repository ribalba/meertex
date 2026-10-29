"""Application configuration.

meertex is a hosted, multi-user service like meerpad, so it is configured the
way meerpad is: environment variables, optionally from a ``.env`` file, with dev
defaults that work out of the box. Coolify sets the environment in its UI.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application configuration, overridable via environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Where the app is reachable from a browser. Sign-in links, invite mails and
    # the project URLs the API hands out are built from it.
    base_url: str = "http://localhost:8060"

    # Signs login codes. CHANGE THIS in production.
    secret_key: str = "dev-insecure-secret-change-me"

    # Database (PostgreSQL). docker-compose points this at the bundled "db" service.
    database_url: str = "postgresql+psycopg://meertex:meertex@localhost:5436/meertex"

    # Every project's git history and binary files: DATA_DIR/projects/<id>/{repo,blobs}.
    data_dir: Path = BASE_DIR / "data"

    # Auth token lifetimes (minutes). The emailed sign-in code shares the login TTL.
    login_token_ttl_minutes: int = 30
    session_ttl_minutes: int = 60 * 24 * 30  # 30 days

    # Wrong guesses before an emailed sign-in code is destroyed.
    login_code_max_attempts: int = 5

    # Sign-in rate limits, per email, over a rolling window (meerpad's numbers).
    login_rate_max: int = 5
    verify_rate_max: int = 15
    login_rate_window_minutes: int = 15

    # A profile picture (§2, Accounts), in DATA_DIR/avatars. The web app
    # uploads a small square, far below this.
    max_avatar_bytes: int = 1024 * 1024

    # SMTP. If smtp_host is empty, emails are printed to the console instead of sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "hello@meertex.com"
    # Who hears about every new account: one mail at its first sign-in, saying
    # who invited it or that it signed up on its own (docs/DESIGN.md §2,
    # Accounts). Empty mails nobody.
    new_user_mail_to: str = ""

    # --- The live document (docs/DESIGN.md §8-§10) ------------------------------
    # A file is text up to this size, a blob above it. Blobs are capped too.
    max_text_bytes: int = 2 * 1024 * 1024
    max_blob_bytes: int = 50 * 1024 * 1024
    # The first sync of a checkout is the whole document in one message.
    # uvicorn caps WebSocket messages separately (--ws-max-size, Dockerfile).
    max_sync_message_bytes: int = 64 * 1024 * 1024
    # A snapshot (git commit) runs this long after the last edit, or this long
    # after the first edit not yet committed, whichever comes first.
    snapshot_idle_seconds: int = 60
    snapshot_max_seconds: int = 600
    # A room without connections is written out and dropped after this long.
    room_idle_seconds: int = 300

    # --- Compiling (docs/DESIGN.md §14) ----------------------------------------
    # The compile service. Empty turns compiling off. docker-compose sets it
    # to the bundled `compile` service, which checks the token.
    compile_url: str = ""
    compile_token: str = ""
    compile_timeout_seconds: int = 90

    # --- References (docs/DESIGN.md §16) -----------------------------------------
    # The lookups at doi.org, Crossref, Semantic Scholar, DBLP, arXiv, Open
    # Library and the web. False leaves only the checks that need no network.
    references_network: bool = True
    # The address the lookups give the services (Crossref's polite pool).
    # EMAIL_FROM when empty.
    references_mailto: str = ""
    semantic_scholar_api_key: str = ""
    # The claim check asks Claude. Without a key it is off.
    anthropic_api_key: str = ""
    references_claude_model: str = "claude-opus-5-5"

    # --- Spelling and grammar (docs/DESIGN.md §17) -----------------------------------
    # A LanguageTool server (its base URL, a trailing /v2 is fine). Empty turns
    # the check off, and the editor keeps the browser's own spellchecker.
    # docker-compose sets it to the bundled `languagetool` service.
    grammar_url: str = ""
    # Reading the answer. The first check in a language loads that language.
    grammar_timeout_seconds: int = 20
    # The privacy guard: a GRAMMAR_URL host with a public address is refused,
    # so unpublished papers do not leave the server by accident.
    grammar_allow_public_hosts: bool = False
    # Requests with LanguageTool at once, for the whole server.
    grammar_max_concurrent: int = 3

    # --- Reminders (docs/DESIGN.md §5, Reminders) ---------------------------------
    # The daily reminder mails before deadlines. False turns the background
    # loop off, for a laptop or a second copy of the app on the same database.
    reminders_enabled: bool = True

    # The pattern a new account names its projects with (§4).
    default_naming_pattern: str = "{year}-{conference|lower}-{paper|lower}"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.projects_dir.mkdir(parents=True, exist_ok=True)
    return settings
