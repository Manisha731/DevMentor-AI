import re

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_db
from app.models import Repo, User
from app.security import decrypt_token

router = APIRouter(prefix="/repos", tags=["repos"])

GITHUB_API = "https://api.github.com"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def require_user(request: Request, db: Session = Depends(get_db)):
    """Return (user, github_token) for the logged-in user, or raise 401."""
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.encrypted_token:
        raise HTTPException(401, "Not logged in")
    try:
        token = decrypt_token(user.encrypted_token)
    except InvalidToken:
        request.session.clear()
        raise HTTPException(401, "Stored token unreadable; please log in again")
    return user, token


def gh_get(path: str, token: str, params: dict | None = None) -> httpx.Response:
    try:
        r = httpx.get(
            f"{GITHUB_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params=params,
            timeout=10,
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach GitHub")
    if r.status_code == 401:
        raise HTTPException(401, "GitHub rejected the stored token; please log in again")
    if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
        raise HTTPException(429, "GitHub rate limit reached; try again later")
    return r


def repo_out(r: Repo) -> dict:
    return {
        "id": r.id,
        "full_name": r.full_name,
        "sync_status": r.sync_status,
        "last_polled_at": r.last_polled_at,
        "created_at": r.created_at,
    }


class ConnectBody(BaseModel):
    full_name: str  # "owner/name"


@router.get("/available")
def available_repos(auth=Depends(require_user), db: Session = Depends(get_db)):
    user, token = auth
    r = gh_get(
        "/user/repos",
        token,
        {"visibility": "public", "affiliation": "owner,collaborator", "sort": "pushed", "per_page": 50},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"GitHub returned {r.status_code}")
    connected = {
        x.full_name
        for x in db.scalars(select(Repo).where(Repo.user_id == user.id, Repo.sync_status != "disconnected"))
    }
    return [
        {
            "full_name": x["full_name"],
            "description": x.get("description"),
            "language": x.get("language"),
            "pushed_at": x.get("pushed_at"),
            "connected": x["full_name"] in connected,
        }
        for x in r.json()
    ]


@router.post("/connect", status_code=201)
def connect_repo(body: ConnectBody, auth=Depends(require_user), db: Session = Depends(get_db)):
    user, token = auth
    name = body.full_name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(422, "Use the form owner/name")
    r = gh_get(f"/repos/{name}", token)
    if r.status_code == 404:
        raise HTTPException(404, "Repo not found, or it is private (private repos aren't supported yet)")
    if r.status_code != 200:
        raise HTTPException(502, f"GitHub returned {r.status_code}")
    data = r.json()
    if data.get("private"):
        raise HTTPException(400, "Private repos aren't supported yet")

    full_name = data["full_name"]  # GitHub's canonical capitalisation
    repo = db.scalar(select(Repo).where(Repo.user_id == user.id, Repo.full_name == full_name))
    if repo is not None and repo.sync_status != "disconnected":
        raise HTTPException(409, "Repo already connected")
    if repo is None:
        repo = Repo(user_id=user.id, full_name=full_name, sync_status="pending")
        db.add(repo)
    else:
        repo.sync_status = "pending"  # reconnect: history is kept
    db.commit()
    db.refresh(repo)
    return repo_out(repo)


@router.get("")
def list_repos(auth=Depends(require_user), db: Session = Depends(get_db)):
    user, _ = auth
    rows = db.scalars(
        select(Repo)
        .where(Repo.user_id == user.id, Repo.sync_status != "disconnected")
        .order_by(Repo.created_at)
    ).all()
    return [repo_out(r) for r in rows]


@router.delete("/{repo_id}")
def disconnect_repo(repo_id: int, auth=Depends(require_user), db: Session = Depends(get_db)):
    user, _ = auth
    repo = db.get(Repo, repo_id)
    # 404 (not 403) for someone else's repo, so ids can't be probed
    if repo is None or repo.user_id != user.id or repo.sync_status == "disconnected":
        raise HTTPException(404, "Repo not found")
    repo.sync_status = "disconnected"  # stop polling later, keep commits and findings
    db.commit()
    return {"status": "disconnected", "id": repo_id}
