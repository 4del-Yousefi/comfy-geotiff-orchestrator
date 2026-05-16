"""SQLite-backed job store. One process owns the DB; aiosqlite is async."""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    progress_msg    TEXT DEFAULT '',
    tiles_total     INTEGER DEFAULT 0,
    tiles_done      INTEGER DEFAULT 0,
    input_key       TEXT NOT NULL,
    output_key      TEXT,
    tile_size       INTEGER NOT NULL,
    overlap_ratio   REAL NOT NULL,
    upscale_factor  INTEGER NOT NULL,
    error           TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    extra           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


class JobStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def create(
        self,
        input_key: str,
        tile_size: int,
        overlap_ratio: float,
        upscale_factor: int,
        extra: Optional[dict] = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO jobs
                   (id, status, input_key, tile_size, overlap_ratio,
                    upscale_factor, created_at, extra)
                   VALUES (?, 'QUEUED', ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, input_key, tile_size, overlap_ratio,
                    upscale_factor, time.time(),
                    json.dumps(extra or {}),
                ),
            )
            await db.commit()
        return job_id

    async def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Auto-stamp transitions
        if "status" in fields:
            s = fields["status"]
            if s in ("DOWNLOADING", "TILING", "PROCESSING") and "started_at" not in fields:
                fields.setdefault("started_at", time.time())
            if s in ("COMPLETED", "FAILED", "CANCELLED"):
                fields.setdefault("finished_at", time.time())

        cols = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [job_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", vals)
            await db.commit()

    async def get(self, job_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        if out.get("extra"):
            try:
                out["extra"] = json.loads(out["extra"])
            except json.JSONDecodeError:
                pass
        # If we have started_at but not finished_at, compute elapsed
        if out.get("started_at"):
            end = out.get("finished_at") or time.time()
            out["elapsed_sec"] = round(end - out["started_at"], 1)
        return out

    async def list_recent(self, limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, status, tiles_done, tiles_total, created_at, finished_at "
                "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
