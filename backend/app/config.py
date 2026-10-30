from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # repo root, where .env lives


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str
    chroma_host: str = "localhost"
    chroma_port: int = 8000

    # Auth (Phase 2). Empty defaults so features run without GitHub keys;
    # the auth routes refuse to work until these are set.
    github_client_id: str = ""
    github_client_secret: str = ""
    token_encryption_key: str = ""
    session_secret: str = ""
    github_redirect_uri: str = "http://localhost:8001/auth/github/callback"
    github_oauth_scopes: str = "read:user public_repo"

    # Polling (Phase 4). Out-of-range values make the app refuse to start.
    poll_enabled: bool = True
    poll_interval_minutes: int = Field(5, ge=1, le=1440)
    poll_initial_commits: int = Field(5, ge=1, le=30)


settings = Settings()
