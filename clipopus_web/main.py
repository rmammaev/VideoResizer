"""
FastAPI-сервис: веб-страница ClipOpus → Resizer.

Роуты:
  GET  /                     — UI (static/index.html)
  GET  /api/resolutions      — список доступных разрешений
  GET  /api/config           — есть ли ключ/orgid, включён ли вебхук
  POST /api/jobs             — старт полного цикла {video_url, resolutions, curation?}
  GET  /api/jobs/{id}        — статус + прогресс + ссылки
  POST /api/jobs/{id}/stop   — остановить
  POST /api/opus-webhook     — приёмник conclusionActions от OpusClip (?job=<id>)
  GET  /files/{path}         — отдача готовых файлов / zip
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import jobs as jobs_mod
import resize as rsz
from opus_client import OpusClient

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"

PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL")
    or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
        if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else None)
)
WEBHOOK_URL = f"{PUBLIC_BASE_URL.rstrip('/')}/api/opus-webhook" if PUBLIC_BASE_URL else None

app = FastAPI(title="ClipOpus → Resizer")


# --- модели запросов ---------------------------------------------------------
class JobRequest(BaseModel):
    video_url: str = Field(..., min_length=4)
    resolutions: list[str] = Field(default_factory=lambda: [rsz.DEFAULT_RES])
    min_duration: Optional[int] = None   # сек, для curationPref
    max_duration: Optional[int] = None
    language: Optional[str] = None       # importPref.language


# --- роуты -------------------------------------------------------------------
@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "index.html не найден"}, status_code=500)
    return FileResponse(idx)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/resolutions")
async def api_resolutions():
    return {"resolutions": list(rsz.RESOLUTIONS.keys()), "default": rsz.DEFAULT_RES}


@app.get("/api/config")
async def api_config():
    opus = OpusClient()
    return {
        "has_key": bool(opus.api_key),
        "has_org": bool(opus.org_id),
        "webhook_enabled": bool(WEBHOOK_URL),
        "ffmpeg": rsz.have_ffmpeg(),
    }


@app.post("/api/jobs")
async def api_create_job(req: JobRequest):
    opus = OpusClient()
    if not opus.api_key:
        raise HTTPException(400, "OPUS_API_KEY не задан на сервере")
    if not rsz.have_ffmpeg():
        raise HTTPException(500, "ffmpeg не найден в окружении сервера")

    bad = [r for r in req.resolutions if r not in rsz.RESOLUTIONS]
    if bad:
        raise HTTPException(400, f"Неизвестные разрешения: {bad}")
    if not req.resolutions:
        raise HTTPException(400, "Не выбрано ни одного разрешения")

    curation = _build_curation(req)
    job = jobs_mod.store.create(req.video_url.strip(), req.resolutions, curation)
    asyncio.create_task(jobs_mod.run_job(job, opus, WEBHOOK_URL))
    return {"id": job.id, "status": job.status}


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = jobs_mod.store.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/stop")
async def api_job_stop(job_id: str):
    job = jobs_mod.store.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    job.stop()
    return {"ok": True}


@app.post("/api/opus-webhook")
async def api_opus_webhook(request: Request, job: Optional[str] = None):
    """OpusClip дёргает этот URL по завершении нарезки — будим поллер job'а."""
    if job:
        j = jobs_mod.store.get(job)
        if j:
            j.add_log("← Webhook от OpusClip: нарезка завершена")
            j.notify_clips_ready()
    return {"ok": True}


@app.get("/files/{file_path:path}")
async def api_files(file_path: str):
    base = jobs_mod.DATA_DIR
    target = (base / file_path).resolve()
    # защита от path traversal
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(target, filename=target.name)


# статика (css/js, если появятся) — монтируем последней, чтобы не перехватывать /api
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _build_curation(req: JobRequest) -> Optional[dict]:
    pref: dict = {}
    if req.min_duration or req.max_duration:
        rng: dict = {}
        if req.min_duration:
            rng["min"] = req.min_duration
        if req.max_duration:
            rng["max"] = req.max_duration
        pref["clipDurationRange"] = rng
    return pref or None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")), reload=False)
