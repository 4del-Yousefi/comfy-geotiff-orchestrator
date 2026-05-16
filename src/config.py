from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Auth ---
    # Shared secret the Cloudflare Worker presents in `X-Orchestrator-Token`.
    # Set to a long random string in production.
    orchestrator_token: str = "change-me"

    # --- RunPod ---
    runpod_endpoint_id: str
    runpod_api_key: str
    runpod_base_url: str = "https://api.runpod.ai/v2"
    # Max concurrent RunPod requests in flight per job.
    # Don't exceed your endpoint's max workers × ~3.
    runpod_max_concurrent: int = 20
    # Per-tile poll interval and timeout
    runpod_poll_interval_sec: float = 3.0
    runpod_tile_timeout_sec: int = 900

    # --- R2 / S3 ---
    r2_endpoint_url: str               # https://<account>.r2.cloudflarestorage.com
    r2_access_key: str
    r2_secret_key: str
    r2_bucket: str
    r2_region: str = "auto"

    # --- Workflow ---
    workflow_path: Path = Path("/app/workflows/qwen-image-edit-sr.api.json")
    # The workflow's LoadImage node references this filename.
    input_image_name: str = "test1.png"
    # 512×512 input tile -> 2048×2048 output tile via the Wan2.1 2x VAE
    # applied twice in this workflow = 4x linear upscale.
    upscale_factor: int = 4

    # --- Storage paths ---
    jobs_db_path: Path = Path("/data/jobs.db")
    tmp_dir: Path = Path("/data/tmp")

    # --- Limits ---
    max_concurrent_jobs: int = 2
    max_tile_count: int = 500   # safety bound; reject jobs that would exceed


settings = Settings()
