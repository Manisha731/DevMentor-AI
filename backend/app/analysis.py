"""Manual triggers + read access for the Code Agent. The scheduler/orchestrator takes over in Phase 7."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import code_agent
from app.auth import get_db
from app.models import Commit, Finding, Repo
from app.repos import require_user

router = APIRouter(prefix="/repos", tags=["analysis"])


def _own_repo(db: Session, user, repo_id: int) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None or repo.user_id != user.id:
        raise HTTPException(404, "Repo not found")
    return repo


def _finding_out(f: Finding) -> dict:
    return {"id": f.id, "agent": f.agent, "type": f.finding_type, "severity": f.severity,
            "file": f.file_path, "line": f.line, "function": f.function_name,
            "description": f.description, "details": f.details}


@router.post("/{repo_id}/analyze")
def analyze(repo_id: int, sha: str | None = Query(None, description="Commit SHA (or prefix). Omit to analyze the newest unanalyzed commits."),
            limit: int = Query(1, ge=1, le=5), db: Session = Depends(get_db), auth=Depends(require_user)):
    user, token = auth
    repo = _own_repo(db, user, repo_id)
    if sha:
        ids = list(db.scalars(select(Commit.id).where(Commit.repo_id == repo.id, Commit.sha.startswith(sha)).limit(2)))
        if not ids:
            raise HTTPException(404, "No saved commit matches that SHA (poll first?)")
        if len(ids) > 1:
            raise HTTPException(400, "SHA prefix is ambiguous; use more characters")
    else:
        ids = code_agent.latest_unanalyzed(db, repo.id, limit)
        if not ids:
            return {"analyzed": [], "note": "no unanalyzed commits"}
    out = []
    for cid in ids:
        try:
            out.append(code_agent.analyze_commit(db, cid, token))
        except code_agent.CodeAgentError as e:
            code = {"auth_error": 401, "rate_limited": 429, "not_found": 404}.get(e.status, 502)
            raise HTTPException(code, str(e))
    return {"analyzed": out}


@router.get("/{repo_id}/findings")
def findings(repo_id: int, sha: str | None = None, severity: str | None = None,
             limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), auth=Depends(require_user)):
    user, _ = auth
    repo = _own_repo(db, user, repo_id)
    q = (select(Finding, Commit.sha).join(Commit, Commit.id == Finding.commit_id)
         .where(Commit.repo_id == repo.id).order_by(Finding.id.desc()).limit(limit))
    if sha:
        q = q.where(Commit.sha.startswith(sha))
    if severity:
        q = q.where(Finding.severity == severity)
    return [{**_finding_out(f), "sha": s[:7]} for f, s in db.execute(q)]
