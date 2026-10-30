"""Code Agent (Phase 5): structural analysis of the files a commit changed.

Flow for one commit:
  1. GET the commit from GitHub -> list of changed files + their patches
  2. for each supported source file: fetch its content AT that commit, run the analyzer
  3. keep only issues that touch lines this commit added/changed
  4. replace this commit's previous 'code' findings, store the new ones, mark analyzed

Facts only (Decision 14): no LLM, no advice. Re-running is safe (idempotent).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.code_analyzer import Issue, analyze_source, language_for
from app.models import AgentRun, Commit, Finding, Repo

log = logging.getLogger("devmentor.code_agent")

GITHUB_API = "https://api.github.com"
MAX_FILES_PER_COMMIT = 25
MAX_FILE_BYTES = 200_000
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class CodeAgentError(Exception):
    """status is one of: auth_error, rate_limited, not_found, error"""

    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status


@dataclass
class ChangedFile:
    path: str
    status: str                 # added | modified | renamed | removed ...
    patch: str | None           # None for binary / very large diffs


def added_lines(patch: str | None) -> set[int] | None:
    """Line numbers (new-file side) that this patch added. None = unknown (analyze all)."""
    if patch is None:
        return None
    out: set[int] = set()
    cur = 0
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            cur = int(m.group(1))
            continue
        if cur == 0:
            continue
        if raw.startswith("+"):
            out.add(cur)
            cur += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith("\\"):   # "\ No newline at end of file"
            continue
        else:
            cur += 1
    return out


def touches(issue: Issue, changed: set[int] | None) -> bool:
    if changed is None:
        return True
    end = issue.end_line or issue.line
    return any(issue.line <= n <= end for n in changed)


def analyze_files(files: list[ChangedFile], fetch: Callable[[str], str | None]):
    """Pure orchestration: returns (list of (path, Issue), skipped list of (path, reason))."""
    results: list[tuple[str, Issue]] = []
    skipped: list[tuple[str, str]] = []
    todo = [f for f in files if f.status != "removed" and language_for(f.path)]
    for f in files:
        if f.status == "removed":
            skipped.append((f.path, "removed"))
        elif not language_for(f.path):
            skipped.append((f.path, "unsupported file type"))
    if len(todo) > MAX_FILES_PER_COMMIT:
        for f in todo[MAX_FILES_PER_COMMIT:]:
            skipped.append((f.path, f"over the {MAX_FILES_PER_COMMIT}-file limit"))
        todo = todo[:MAX_FILES_PER_COMMIT]
    for f in todo:
        text = fetch(f.path)
        if text is None:
            skipped.append((f.path, "could not fetch / too large"))
            continue
        changed = None if f.status == "added" else added_lines(f.patch)
        for issue in analyze_source(f.path, text):
            if touches(issue, changed):
                results.append((f.path, issue))
    return results, skipped


# ---------------- GitHub + DB layer ----------------

def _gh(path: str, token: str, accept: str = "application/vnd.github+json", params=None) -> httpx.Response:
    try:
        r = httpx.get(f"{GITHUB_API}{path}", headers={"Authorization": f"Bearer {token}", "Accept": accept},
                      params=params, timeout=15)
    except httpx.HTTPError:
        raise CodeAgentError("error", "could not reach GitHub")
    if r.status_code == 401:
        raise CodeAgentError("auth_error", "GitHub rejected the stored token")
    if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
        raise CodeAgentError("rate_limited", "GitHub rate limit reached")
    return r


def fetch_commit_files(full_name: str, sha: str, token: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for page in range(1, 4):  # GitHub returns up to 300 files across pages
        r = _gh(f"/repos/{full_name}/commits/{sha}", token, params={"per_page": 100, "page": page})
        if r.status_code == 404:
            raise CodeAgentError("not_found", "commit or repo not found on GitHub")
        if r.status_code != 200:
            raise CodeAgentError("error", f"GitHub returned {r.status_code}")
        batch = r.json().get("files", [])
        files += [ChangedFile(f["filename"], f["status"], f.get("patch")) for f in batch]
        if len(batch) < 100:
            break
    return files


def make_fetcher(full_name: str, sha: str, token: str) -> Callable[[str], str | None]:
    def fetch(path: str) -> str | None:
        r = _gh(f"/repos/{full_name}/contents/{path}", token,
                accept="application/vnd.github.raw+json", params={"ref": sha})
        if r.status_code != 200 or len(r.content) > MAX_FILE_BYTES:
            return None
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return fetch


def analyze_commit(db: Session, commit_id: int, token: str) -> dict:
    commit = db.get(Commit, commit_id)
    repo = db.get(Repo, commit.repo_id)

    run = AgentRun(commit_id=commit.id, status="running", agents_run=["code"], llm_calls_used=0)
    db.add(run)
    db.flush()
    try:
        files = fetch_commit_files(repo.full_name, commit.sha, token)
        results, skipped = analyze_files(files, make_fetcher(repo.full_name, commit.sha, token))
    except CodeAgentError as e:
        run.status, run.finished_at = e.status, datetime.now(timezone.utc)
        db.commit()
        raise

    # idempotent: drop this commit's earlier code findings, then store fresh ones
    db.execute(delete(Finding).where(Finding.commit_id == commit.id, Finding.agent == "code"))
    for path, i in results:
        db.add(Finding(commit_id=commit.id, run_id=run.id, agent="code", finding_type=i.finding_type,
                       severity=i.severity, file_path=path, line=i.line, function_name=i.function_name,
                       description=i.description, details=i.details))
    now = datetime.now(timezone.utc)
    run.status, run.finished_at = "completed", now
    commit.analyzed_at = now
    db.commit()
    log.info("code agent %s@%s: %d finding(s), %d file(s) skipped",
             repo.full_name, commit.sha[:7], len(results), len(skipped))
    return {"sha": commit.sha, "run_id": run.id, "findings": len(results),
            "files_analyzed": len(files) - len(skipped), "skipped": [{"path": p, "reason": r} for p, r in skipped]}


def latest_unanalyzed(db: Session, repo_id: int, limit: int) -> list[int]:
    return list(db.scalars(select(Commit.id).where(Commit.repo_id == repo_id, Commit.analyzed_at.is_(None))
                           .order_by(Commit.committed_at.desc().nullslast(), Commit.id.desc()).limit(limit)))
