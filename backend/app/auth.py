import secrets

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.security import decrypt_token, encrypt_token

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API_USER = "https://api.github.com/user"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_config():
    needed = {
        "GITHUB_CLIENT_ID": settings.github_client_id,
        "GITHUB_CLIENT_SECRET": settings.github_client_secret,
        "TOKEN_ENCRYPTION_KEY": settings.token_encryption_key,
    }
    missing = [name for name, value in needed.items() if not value]
    if missing:
        raise HTTPException(500, f"Missing in .env: {', '.join(missing)}")


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


@router.get("/github/login")
def github_login(request: Request):
    _require_config()
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state  # checked in the callback (CSRF protection)
    params = httpx.QueryParams(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_redirect_uri,
            "scope": settings.github_oauth_scopes,
            "state": state,
        }
    )
    return RedirectResponse(f"{GITHUB_AUTHORIZE}?{params}")


@router.get("/github/callback")
def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    _require_config()
    if error:
        raise HTTPException(400, f"GitHub returned an error: {error}")
    expected = request.session.pop("oauth_state", None)
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "Invalid OAuth state")

    with httpx.Client(timeout=10) as client:
        tok = client.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_redirect_uri,
            },
        ).json()
        access_token = tok.get("access_token")
        if not access_token:
            raise HTTPException(400, f"Token exchange failed: {tok.get('error', 'unknown')}")
        gh = client.get(GITHUB_API_USER, headers=_gh_headers(access_token))
    if gh.status_code != 200:
        raise HTTPException(502, "Could not read the GitHub profile")
    profile = gh.json()

    user = db.scalar(select(User).where(User.github_id == profile["id"]))
    if user is None:
        user = User(github_id=profile["id"], username=profile["login"])
        db.add(user)
    user.username = profile["login"]
    user.encrypted_token = encrypt_token(access_token)  # never stored in plain text
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/auth/me")


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.encrypted_token:
        raise HTTPException(401, "Not logged in")

    # Revocation check: if GitHub rejects the stored token, clear it and force a re-login.
    try:
        token = decrypt_token(user.encrypted_token)
    except InvalidToken:
        request.session.clear()
        raise HTTPException(401, "Stored token unreadable; please log in again")
    with httpx.Client(timeout=10) as client:
        r = client.get(GITHUB_API_USER, headers=_gh_headers(token))
    if r.status_code == 401:
        user.encrypted_token = None
        db.commit()
        request.session.clear()
        raise HTTPException(401, "GitHub access was revoked; please log in again")

    return {"id": user.id, "github_id": user.github_id, "username": user.username, "has_token": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"status": "logged out"}
