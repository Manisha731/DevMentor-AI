import secrets

from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import auth, repos
from app.chroma import get_client
from app.config import settings
from app.db import engine

app = FastAPI(title="DevMentor AI")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or secrets.token_urlsafe(32),
    same_site="lax",
    https_only=False,  # local dev only; becomes True once served over HTTPS (Phase 14)
    max_age=14 * 24 * 3600,
)
app.include_router(auth.router)
app.include_router(repos.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/deps")
def health_deps():
    result = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        result["postgres"] = "ok"
    except Exception as e:
        result["postgres"] = f"error: {type(e).__name__}"
    try:
        get_client().heartbeat()
        result["chroma"] = "ok"
    except Exception as e:
        result["chroma"] = f"error: {type(e).__name__}"
    return result
