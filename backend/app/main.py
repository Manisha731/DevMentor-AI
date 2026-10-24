from fastapi import FastAPI
from sqlalchemy import text

from app.chroma import get_client
from app.db import engine

app = FastAPI(title="DevMentor AI")


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
