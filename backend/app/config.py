from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # repo root, where .env lives


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str
    chroma_host: str = "localhost"
    chroma_port: int = 8000

    # Auth (Phase 2). Empty defaults so Phase 1 features run without GitHub keys;
    # the auth routes refuse to work until these are set.
    github_client_id: str = ""
    github_client_secret: str = ""
    token_encryption_key: str = ""
    session_secret: str = ""
    github_redirect_uri: str = "http://localhost:8001/auth/github/callback"
    github_oauth_scopes: str = "read:user public_repo"


settings = Settings()
