# geotiff-orchestrator

Python service that takes a GeoTIFF, tiles it (square tiles, no edge padding,
configurable overlap), fans each tile out to a RunPod ComfyUI super-resolution
endpoint, stitches the results with feathered blending, writes a new GeoTIFF
preserving the original CRS, and uploads to Cloudflare R2.

Runs as a single Docker container on Hetzner (CPX32 or larger), deployed via
Coolify. Fronted by your Cloudflare Worker for auth.

```
Client → CF Worker (auth, rate limit) → Hetzner orchestrator → RunPod (per tile)
            │                                  │
            R2 (input GeoTIFF) ────────────────┘
            R2 (output GeoTIFF) ←──────────────┘
```

---

## What's in this folder

```
geotiff-orchestrator/
├── Dockerfile                    GDAL-based image, python deps installed
├── docker-compose.yml            local dev only
├── requirements.txt
├── .env.example                  copy → .env (or paste into Coolify)
├── workflows/
│   └── qwen-image-edit-sr.api.json   embedded in image
└── src/
    ├── app.py                    FastAPI HTTP layer
    ├── config.py                 env-var settings (pydantic-settings)
    ├── db.py                     SQLite job store (aiosqlite)
    ├── orchestrator.py           the pipeline (download → tile → fan-out → stitch → upload)
    ├── runpod_client.py          async submit + poll
    ├── storage.py                R2 / S3 client (boto3)
    ├── stitching.py              feathered blending into output canvas
    └── tiling.py                 tile coordinate math (no padding, edges shift back)
```

---

## Deployment runbook

### A. Cloudflare side (do these first)

#### 1. Create an R2 bucket

Cloudflare dashboard → **R2 → Create bucket** → name `geotiff-jobs`.
Choose a location close to your Hetzner box (EU if Falkenstein/Helsinki, ENAM if Ashburn).

#### 2. Create an R2 API token

R2 → **Manage R2 API Tokens → Create API Token**
- Permission: **Object Read & Write**
- Scope: just the `geotiff-jobs` bucket
- TTL: forever (or rotate annually)

Save the returned **Access Key ID**, **Secret Access Key**, and **Endpoint URL**.
The endpoint looks like `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.

### B. Hetzner side — install Coolify and deploy

#### 3. Get Coolify ready

If you used Hetzner's "Coolify" app preset, it's already installing. Otherwise:
```bash
ssh root@<hetzner-ip>
apt update && apt upgrade -y
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

Open `http://<hetzner-ip>:8000` and create the admin account. Then point
DNS for both `orchestrator.yourdomain.com` and (optionally) `coolify.yourdomain.com`
at the Hetzner IPv4 (Cloudflare DNS, *gray cloud / DNS only* — Coolify needs
to terminate TLS itself for Let's Encrypt).

#### 4. Push this repo to GitHub (private)

```powershell
cd "F:\Coding projects\geotiff-orchestrator"
git init
git add .
git commit -m "initial orchestrator"
gh repo create comfy-geotiff-orchestrator --private --source=. --remote=origin --push
```

#### 5. Deploy in Coolify

- **+ New → Resource → Public Repository** (paste your repo URL, or use Private with the SSH deploy key Coolify gives you)
- Coolify auto-detects the Dockerfile
- **Domain**: `orchestrator.yourdomain.com` — Coolify auto-issues TLS via Let's Encrypt
- **Port** (build pack): `8080`
- **Persistent storage**: add a volume mounted at `/data` — this holds the SQLite job DB across redeploys
- **Environment Variables** — paste the values from your `.env`:

```
ORCHESTRATOR_TOKEN     <long random string — generate one with `openssl rand -hex 32`>
RUNPOD_ENDPOINT_ID     zeydrhqbkf5dlj          (your endpoint)
RUNPOD_API_KEY         rpa_xxx                 (your RunPod key)
RUNPOD_MAX_CONCURRENT  20
RUNPOD_TILE_TIMEOUT_SEC  900
R2_ENDPOINT_URL        https://<account>.r2.cloudflarestorage.com
R2_ACCESS_KEY          <from step 2>
R2_SECRET_KEY          <from step 2>
R2_BUCKET              geotiff-jobs
UPSCALE_FACTOR         2
MAX_CONCURRENT_JOBS    2
MAX_TILE_COUNT         500
```

- Mark `ORCHESTRATOR_TOKEN`, `RUNPOD_API_KEY`, `R2_SECRET_KEY` as **secrets** (Coolify encrypts at rest)
- **Deploy**

Wait ~2 minutes for the build + first deploy. Check **Logs** for `orchestrator ready`.

#### 6. Verify it's up

```powershell
curl https://orchestrator.yourdomain.com/healthz
# → {"ok":true}
```

### C. Cloudflare Worker side (wire it up)

#### 7. Add the orchestrator URL + token as Worker config

```powershell
cd "F:\Coding projects\comfy-api-gateway"

# Set the secret token (must match the one you set in Coolify)
wrangler secret put ORCHESTRATOR_TOKEN
# paste the same value as ORCHESTRATOR_TOKEN above

# Edit wrangler.toml and uncomment / set:
#   ORCHESTRATOR_URL = "https://orchestrator.yourdomain.com"

wrangler deploy
```

#### 8. Test the full chain end-to-end

```powershell
$GW = "https://comfy-api-gateway.<your-subdomain>.workers.dev"   # or your custom domain
$KEY = "sk_..."   # your minted API key

# (a) mint a presigned upload URL
$mint = Invoke-RestMethod -Method Post -Uri "$GW/v1/geotiff/upload-url" `
  -Headers @{ "X-API-Key" = $KEY; "Content-Type" = "application/json" } `
  -Body (@{ key = "inputs/test1.tif"; content_type = "image/tiff" } | ConvertTo-Json)

# (b) upload the file directly to R2 with the presigned PUT
Invoke-WebRequest -Method Put -Uri $mint.upload_url `
  -Headers @{ "Content-Type" = "image/tiff" } `
  -InFile "C:\path\to\your\input.tif"

# (c) create a job
$job = Invoke-RestMethod -Method Post -Uri "$GW/v1/geotiff/jobs" `
  -Headers @{ "X-API-Key" = $KEY; "Content-Type" = "application/json" } `
  -Body (@{ input_key = "inputs/test1.tif"; tile_size = 512; overlap_ratio = 0.125 } | ConvertTo-Json)
$job.id

# (d) poll
while ($true) {
  $s = Invoke-RestMethod -Uri "$GW/v1/geotiff/jobs/$($job.id)" `
        -Headers @{ "X-API-Key" = $KEY }
  "[$($s.status)] $($s.tiles_done)/$($s.tiles_total)  $($s.progress_msg)"
  if ($s.status -in "COMPLETED","FAILED") { $s | ConvertTo-Json -Depth 6; break }
  Start-Sleep 10
}

# (e) download result via the signed URL returned in the COMPLETED status
Invoke-WebRequest -Uri $s.output_url -OutFile "out.tif"
```

---

## API reference (orchestrator-direct, behind ORCHESTRATOR_TOKEN)

> The Worker is the only intended caller. If you call directly for debugging,
> add `X-Orchestrator-Token: <token>`.

### `POST /v1/upload-url`
```json
{ "key": "inputs/abc.tif", "content_type": "image/tiff", "expires_sec": 3600 }
```
Returns a presigned R2 PUT URL the client uses to upload directly.

### `POST /v1/jobs`
```json
{
  "input_key": "inputs/abc.tif",
  "tile_size": 512,
  "overlap_ratio": 0.125,
  "upscale_factor": 2
}
```
Returns the new job's status row.

### `GET /v1/jobs/{id}`
Returns:
```json
{
  "id": "...",
  "status": "QUEUED|DOWNLOADING|TILING|PROCESSING|WRITING|UPLOADING|COMPLETED|FAILED",
  "tiles_done": 87,
  "tiles_total": 200,
  "progress_msg": "processed 87/200 tiles",
  "output_url": "https://...r2.../outputs/...tif?...",   // only when COMPLETED
  "output_key": "outputs/...tif",
  "error": null,
  "elapsed_sec": 312.4
}
```

### `GET /healthz`
No auth, returns `{"ok": true}`.

---

## Tuning notes

- **`MAX_TILE_COUNT`** caps jobs that would explode (e.g., 1024×1024 input with tile_size=64 → 1024 tiles).
- **`RUNPOD_MAX_CONCURRENT`** should not exceed your RunPod endpoint's `max_workers × 2–3`. Above that, requests pile up in RunPod's queue and the orchestrator just sits idle.
- **`MAX_CONCURRENT_JOBS`** limits how many *user* jobs run concurrently on the box. On CPX32 (8 GB RAM), 2 is safe; the output canvas dominates memory.
- **Persistent storage** — make sure Coolify's volume is mounted at `/data` so SQLite job history survives redeploys.

## Common failures and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `400 input_key not found in R2` | Wrong bucket or key | Check the bucket name + presigned upload actually succeeded (200 from PUT) |
| Job stuck in `PROCESSING` indefinitely | RunPod endpoint cold or out of GPUs | Check RunPod console; bump `RUNPOD_MAX_CONCURRENT` lower if you're flooding RunPod queue |
| `tile XX timed out after 900s` | RunPod cold start of model load is long | Increase `RUNPOD_TILE_TIMEOUT_SEC`, or set Min Workers ≥ 1 on RunPod |
| Output GeoTIFF has wrong CRS / no georef | Input TIFF had no CRS to begin with | Verify with `gdalinfo input.tif`; orchestrator preserves whatever the input has |
| Seams visible between tiles in output | Overlap too small for the model's "edge artifacts" | Increase `overlap_ratio` (e.g., 0.25); feathering helps but more overlap helps more |
| `413 payload too large` from Worker | TIFF too big for Worker proxy | Use presigned upload URL — TIFF goes Client→R2 directly, never through Worker |
