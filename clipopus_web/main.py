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
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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

# Порог virality-рейтинга по умолчанию: качаем только клипы с score >= этого значения.
DEFAULT_MIN_SCORE = 85


# --- модели запросов ---------------------------------------------------------
class JobRequest(BaseModel):
    video_url: str = Field(..., min_length=4)
    resolutions: list[str] = Field(default_factory=lambda: [rsz.DEFAULT_RES])
    start_sec: Optional[int] = None      # окно исходника: с какой секунды
    end_sec: Optional[int] = None        # ... по какую
    clip_min: Optional[int] = None       # длина клипа, сек (нижняя граница)
    clip_max: Optional[int] = None       # длина клипа, сек (верхняя граница)
    min_score: Optional[int] = None      # порог virality-рейтинга 0..100
    top_only: bool = False               # взять только лучший клип (топ-1)
    language: Optional[str] = None       # importPref.sourceLang


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
    # Дефолтный порог рейтинга: берём только клипы с score >= 85.
    # Но при «только лучший клип» фильтр не нужен — берём лучший из окна как есть.
    if req.top_only:
        min_score = 0
    else:
        raw_score = req.min_score if req.min_score is not None else DEFAULT_MIN_SCORE
        min_score = max(0, min(100, raw_score))
    job = jobs_mod.store.create(
        req.video_url.strip(), req.resolutions, curation, min_score, req.top_only)
    asyncio.create_task(jobs_mod.run_job(job, opus, WEBHOOK_URL))
    return {"id": job.id, "status": job.status}


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = jobs_mod.store.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    return job.to_dict()


class AddFormatsRequest(BaseModel):
    resolutions: list[str]


@app.post("/api/jobs/{job_id}/add_formats")
async def api_add_formats(job_id: str, req: AddFormatsRequest):
    job = jobs_mod.store.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    if job.status not in ("done", "stopped", "error"):
        raise HTTPException(409, "Дождись завершения текущей обработки")
    bad = [r for r in req.resolutions if r not in rsz.RESOLUTIONS]
    if bad:
        raise HTTPException(400, f"Неизвестные разрешения: {bad}")
    if not req.resolutions:
        raise HTTPException(400, "Не выбрано ни одного формата")
    job._stop = False
    asyncio.create_task(jobs_mod.add_formats(job, req.resolutions))
    return {"ok": True}


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


async def _save_uploads(job, files: list[UploadFile]) -> list[Path]:
    up = job.dir / "upload"
    up.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for idx, uf in enumerate(files):
        safe = re.sub(r"[^\w.\- ]", "_", (uf.filename or f"file{idx}"))[:80]
        dst = up / f"{idx:02d}_{safe}"
        with open(dst, "wb") as fh:
            while True:
                chunk = await uf.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        saved.append(dst)
    return saved


def _parse_resolutions(resolutions: str) -> list[str]:
    res = [r.strip() for r in (resolutions or "").split(",") if r.strip()]
    bad = [r for r in res if r not in rsz.RESOLUTIONS]
    if bad:
        raise HTTPException(400, f"Неизвестные разрешения: {bad}")
    if not res:
        raise HTTPException(400, "Не выбрано ни одного формата")
    return res


@app.post("/api/resize")
async def api_resize(
    files: list[UploadFile] = File(...),
    resolutions: str = Form(...),
):
    if not rsz.have_ffmpeg():
        raise HTTPException(500, "ffmpeg не найден")
    res = _parse_resolutions(resolutions)
    if not files:
        raise HTTPException(400, "Не загружено ни одного файла")
    job = jobs_mod.store.create("upload", res)
    saved = await _save_uploads(job, files)
    asyncio.create_task(jobs_mod.run_resize_job(job, saved))
    return {"id": job.id, "status": job.status}


@app.post("/api/concat")
async def api_concat(
    files: list[UploadFile] = File(...),
    resolutions: str = Form(...),
    fade: bool = Form(False),
):
    if not rsz.have_ffmpeg():
        raise HTTPException(500, "ffmpeg не найден")
    res = _parse_resolutions(resolutions)
    if not files or len(files) < 2:
        raise HTTPException(400, "Для склейки нужно минимум 2 файла")
    job = jobs_mod.store.create("upload", res)
    saved = await _save_uploads(job, files)
    asyncio.create_task(jobs_mod.run_concat_job(job, saved, fade))
    return {"id": job.id, "status": job.status}


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
    """Собираем curationPref под реальный API OpusClip (range + clipDurations + lang)."""
    pref: dict = {}
    # окно исходника: { startSec, endSec }
    if req.start_sec is not None or req.end_sec is not None:
        rng: dict = {}
        if req.start_sec is not None:
            rng["startSec"] = max(0, req.start_sec)
        if req.end_sec is not None:
            rng["endSec"] = max(0, req.end_sec)
        if rng:
            pref["range"] = rng
    # желаемая длина клипов: [[min, max]]
    if req.clip_min is not None or req.clip_max is not None:
        lo = max(0, req.clip_min) if req.clip_min is not None else 0
        hi = req.clip_max if req.clip_max is not None else 90
        pref["clipDurations"] = [[lo, hi]]
    return pref or None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")), reload=False)
