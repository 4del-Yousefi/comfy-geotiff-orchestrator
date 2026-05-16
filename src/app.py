"""FastAPI HTTP layer. The Cloudflare Worker is the only intended caller.

Endpoints:
  POST /v1/jobs            create a job (worker has already uploaded the TIFF to R2)
  GET  /v1/jobs/{id}       get job status (incl. signed output URL when COMPLETED)
  GET  /v1/jobs            list recent
  POST /v1/upload-url      mint a presigned R2 PUT URL for the client to upload directly
  GET  /healthz            liveness, no auth
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .config import settings
from .db import JobStore
from .orchestrator import run_job
from .runpod_client import RunPodClient
from .storage import StorageClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
log = logging.getLogger("orchestrator")


# ---- Globals (initialised in lifespan) ------------------------------------
_db: JobStore | None = None
_storage: StorageClient | None = None
_runpod: RunPodClient | None = None
_job_semaphore: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _storage, _runpod, _job_semaphore
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)

    _db = JobStore(settings.jobs_db_path)
    await _db.init()

    _storage = StorageClient(
        endpoint_url=settings.r2_endpoint_url,
        access_key=settings.r2_access_key,
        secret_key=settings.r2_secret_key,
        bucket=settings.r2_bucket,
        region=settings.r2_region,
    )
    _runpod = RunPodClient(
        endpoint_id=settings.runpod_endpoint_id,
        api_key=settings.runpod_api_key,
        base_url=settings.runpod_base_url,
        poll_interval_sec=settings.runpod_poll_interval_sec,
        tile_timeout_sec=settings.runpod_tile_timeout_sec,
    )
    _job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

    log.info("orchestrator ready")
    try:
        yield
    finally:
        await _runpod.aclose()


app = FastAPI(title="comfy-geotiff-orchestrator", lifespan=lifespan)


# ---- Auth ------------------------------------------------------------------
def require_token(x_orchestrator_token: str | None = Header(default=None)):
    if not x_orchestrator_token or x_orchestrator_token != settings.orchestrator_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid orchestrator token")


# ---- Schemas ---------------------------------------------------------------
class CreateJobReq(BaseModel):
    input_key: str = Field(..., description="R2 object key holding the input GeoTIFF")
    tile_size: int = Field(512, ge=64, le=2048)
    overlap_ratio: float = Field(0.125, ge=0.0, lt=0.9)
    upscale_factor: int = Field(default=settings.upscale_factor, ge=1, le=8)


class JobResp(BaseModel):
    id: str
    status: str
    tiles_done: int
    tiles_total: int
    progress_msg: str | None = None
    output_url: str | None = None
    output_key: str | None = None
    error: str | None = None
    elapsed_sec: float | None = None


class UploadUrlReq(BaseModel):
    key: str = Field(..., description="Desired R2 object key (e.g. 'inputs/abc.tif')")
    content_type: str = "image/tiff"
    expires_sec: int = Field(3600, ge=60, le=7 * 24 * 3600)


class UploadUrlResp(BaseModel):
    upload_url: str
    key: str
    expires_sec: int


# ---- Helpers ---------------------------------------------------------------
async def _job_runner(job_id: str) -> None:
    assert _job_semaphore is not None
    async with _job_semaphore:
        assert _db and _storage and _runpod
        await run_job(job_id, settings, _db, _storage, _runpod)


def _job_to_resp(job: dict) -> JobResp:
    output_url: str | None = None
    if job.get("status") == "COMPLETED" and job.get("output_key") and _storage is not None:
        output_url = _storage.presigned_get_url(job["output_key"], expires_sec=3600)
    return JobResp(
        id=job["id"],
        status=job["status"],
        tiles_done=job.get("tiles_done") or 0,
        tiles_total=job.get("tiles_total") or 0,
        progress_msg=job.get("progress_msg"),
        output_url=output_url,
        output_key=job.get("output_key"),
        error=job.get("error"),
        elapsed_sec=job.get("elapsed_sec"),
    )


# ---- Routes ----------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/v1/upload-url", response_model=UploadUrlResp, dependencies=[Depends(require_token)])
async def upload_url(req: UploadUrlReq):
    assert _storage
    url = _storage.presigned_put_url(req.key, expires_sec=req.expires_sec, content_type=req.content_type)
    return UploadUrlResp(upload_url=url, key=req.key, expires_sec=req.expires_sec)


@app.post("/v1/jobs", response_model=JobResp, dependencies=[Depends(require_token)])
async def create_job(req: CreateJobReq):
    assert _db and _storage
    # Verify the input exists in R2 before queueing
    try:
        await _storage.head(req.input_key)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"input_key not found in R2: {e}")

    job_id = await _db.create(
        input_key=req.input_key,
        tile_size=req.tile_size,
        overlap_ratio=req.overlap_ratio,
        upscale_factor=req.upscale_factor,
    )
    asyncio.create_task(_job_runner(job_id))
    job = await _db.get(job_id)
    assert job is not None
    return _job_to_resp(job)


@app.get("/v1/jobs/{job_id}", response_model=JobResp, dependencies=[Depends(require_token)])
async def get_job(job_id: str):
    assert _db
    job = await _db.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    return _job_to_resp(job)


@app.get("/v1/jobs", dependencies=[Depends(require_token)])
async def list_jobs(limit: int = 50):
    assert _db
    return {"jobs": await _db.list_recent(limit=limit)}
