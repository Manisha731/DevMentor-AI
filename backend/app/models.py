from datetime import datetime

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(Integer, unique=True)
    username: Mapped[str] = mapped_column(String(100))
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("user_id", "full_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    full_name: Mapped[str] = mapped_column(String(200))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Commit(Base):
    __tablename__ = "commits"
    __table_args__ = (UniqueConstraint("repo_id", "sha"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    sha: Mapped[str] = mapped_column(String(40))
    message: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    commit_id: Mapped[int] = mapped_column(ForeignKey("commits.id"))
    status: Mapped[str] = mapped_column(String(30), default="running")
    tier: Mapped[int | None] = mapped_column(Integer)  # 0, 1 or 2 (Decision 20)
    agents_run: Mapped[list | None] = mapped_column(JSON)
    tool_call_trace: Mapped[list | None] = mapped_column(JSON)  # Decision 18
    planner_decisions: Mapped[list | None] = mapped_column(JSON)  # Decision 20
    llm_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    llm_call_budget: Mapped[int | None] = mapped_column(Integer)
    limits_used: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("user_id", "skill_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    skill_name: Mapped[str] = mapped_column(String(100))
    proficiency: Mapped[float] = mapped_column(Float, default=0.0)
    times_flagged: Mapped[int] = mapped_column(Integer, default=0)
    last_flagged_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    commit_id: Mapped[int] = mapped_column(ForeignKey("commits.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    agent: Mapped[str] = mapped_column(String(30))  # code / test / review
    finding_type: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str | None] = mapped_column(String(20))
    file_path: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    function_name: Mapped[str | None] = mapped_column(String(200))  # function-level (Decision 19)
    description: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProposedTest(Base):
    __tablename__ = "proposed_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    commit_id: Mapped[int] = mapped_column(ForeignKey("commits.id"))
    function_name: Mapped[str] = mapped_column(String(200))
    test_code: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)  # required (Decision 19)
    confidence: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/accepted/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
