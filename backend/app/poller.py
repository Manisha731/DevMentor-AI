import logging
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import InvalidToken
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Commit, Repo, User
from app.security import decrypt_token

log = logging.getLogger("devmentor.poller")

GITHUB_API = "https://api.github.com"
PAGE_SIZE = 30
MAX_PAGES = 3  # at most 90 new commits per poll; older ones are skipped with a warning


class PollError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _fetch_page(full_name: str, token: str, page: int) -> list:
    try:
        r = httpx.get(
            f"{GITHUB_API}/repos/{full_name}/commits",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params={"per_page": PAGE_SIZE, "page": page},
            timeout=15,
        )
    except httpx.HTTPError as e:
        raise PollError("error", f"network error: {type(e).__name__}")
    if r.status_code == 401:
        raise PollError("auth_error", "GitHub rejected the stored token")
    if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
        raise PollError("rate_limited", "GitHub rate limit reached")
    if r.status_code == 404:
        raise PollError("error", "repo not found (deleted, renamed or now private)")
    if r.status_code == 409:  # GitHub's answer for an empty repository
        return []
    if r.status_code != 200:
        raise PollError("error", f"GitHub returned {r.status_code}")
    return r.json()


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _to_row(repo_id: int, c: dict) -> dict:
    info = c["commit"]
    who = info.get("author") or {}
    when = (info.get("committer") or who).get("date")
    return {
        "repo_id": repo_id,
        "sha": c["sha"],
        "message": info.get("message"),
        "author": who.get("name") or (c.get("author") or {}).get("login"),
        "committed_at": _parse_time(when),
    }


def _poll_repo(db: Session, repo: Repo) -> int:
    """Fetch new commits for one repo and insert them. Returns how many were new."""
    user = db.get(User, repo.user_id)
    if user is None or not user.encrypted_token:
        raise PollError("auth_error", "owner has no stored token; log in again")
    try:
        token = decrypt_token(user.encrypted_token)
    except InvalidToken:
        raise PollError("auth_error", "stored token unreadable; log in again")

    known = set(db.scalars(select(Commit.sha).where(Commit.repo_id == repo.id)))
    first_poll = len(known) == 0

    new_items: list = []
    hit_known = False
    for page in range(1, MAX_PAGES + 1):
        items = _fetch_page(repo.full_name, token, page)
        if not items:
            break
        if first_poll:  # baseline: only the newest few, not the whole history
            new_items = items[: settings.poll_initial_commits]
            break
        for c in items:  # newest first
            if c["sha"] in known:
                hit_known = True
                break
            new_items.append(c)
        if hit_known or len(items) < PAGE_SIZE:
            break
    if not first_poll and not hit_known and len(new_items) >= PAGE_SIZE * MAX_PAGES:
        log.warning("%s: more than %d new commits; older ones were skipped", repo.full_name, len(new_items))

    rows = [_to_row(repo.id, c) for c in reversed(new_items)]  # oldest first, so ids follow time
    if not rows:
        return 0
    result = db.execute(insert(Commit).values(rows).on_conflict_do_nothing(index_elements=["repo_id", "sha"]))
    return result.rowcount


def poll_one(db: Session, repo_id: int) -> dict:
    repo = db.get(Repo, repo_id)
    if repo is None or repo.sync_status == "disconnected":
        return {"status": "skipped"}
    name = repo.full_name
    detail = None
    inserted = 0
    try:
        inserted = _poll_repo(db, repo)
        status = "synced"
    except PollError as e:
        db.rollback()
        status, detail = e.status, str(e)
        log.warning("poll %s: %s (%s)", name, status, detail)
    except Exception as e:  # one bad repo must never kill the whole cycle
        db.rollback()
        status, detail = "error", type(e).__name__
        log.exception("poll %s failed", name)

    repo = db.get(Repo, repo_id)
    if repo.sync_status != "disconnected":  # don't overwrite a disconnect made mid-poll
        repo.sync_status = status
    repo.last_polled_at = datetime.now(timezone.utc)
    db.commit()
    if status == "synced":
        log.info("poll %s: %d new commit(s)", name, inserted)
    return {"repo": name, "status": status, "new_commits": inserted, "detail": detail}


def poll_all() -> None:
    with SessionLocal() as db:
        ids = list(db.scalars(select(Repo.id).where(Repo.sync_status != "disconnected").order_by(Repo.id)))
        log.info("poll cycle: %d repo(s)", len(ids))
        for rid in ids:
            if poll_one(db, rid)["status"] == "rate_limited":
                log.warning("rate limited; stopping this cycle early")
                break


scheduler = BackgroundScheduler(timezone="UTC")


def start() -> None:
    if not settings.poll_enabled:
        log.info("polling disabled (POLL_ENABLED=false)")
        return
    scheduler.add_job(
        poll_all,
        "interval",
        minutes=settings.poll_interval_minutes,
        id="poll_all",
        max_instances=1,  # never two cycles at once
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),  # also run once right at startup
    )
    scheduler.start()
    log.info("polling every %d minute(s)", settings.poll_interval_minutes)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
