"""
Оркестрация полного цикла: создать проект OpusClip -> дождаться клипов ->
скачать -> ресайзнуть в выбранные разрешения -> упаковать zip.

Один процесс, состояние в памяти + файлы на диске (MVP под один инстанс Railway).
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import httpx

import resize as rsz
from opus_client import (
    OpusClient,
    OpusError,
    clip_download_url,
    clip_score,
    clip_title,
    project_id_from_response,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()

# Поллинг готовности клипов
POLL_INTERVAL_SEC = float(os.environ.get("OPUS_POLL_INTERVAL", "15"))
POLL_TIMEOUT_SEC = float(os.environ.get("OPUS_POLL_TIMEOUT", "3600"))  # 1 час

STATUS_FLOW = ("queued", "creating", "clipping", "downloading", "resizing", "done")


def _safe_name(name: str, maxlen: int = 50) -> str:
    return re.sub(r"[^\w\-. ]", "_", name).strip()[:maxlen] or "clip"


class Job:
    def __init__(self, job_id: str, video_url: str, resolutions: list[str],
                 curation_pref: Optional[dict] = None, min_score: int = 0):
        self.id = job_id
        self.video_url = video_url
        self.resolutions = resolutions
        self.curation_pref = curation_pref
        self.min_score = min_score
        self.status = "queued"
        self.project_id: Optional[str] = None
        self.error: Optional[str] = None
        self.log: list[str] = []
        self.clips: list[dict] = []        # [{title, url}]
        self.outputs: list[dict] = []      # [{name, file}]  file = относит. путь для /files
        self.progress = 0                  # 0..100 на стадии ресайза
        self.bundle: Optional[str] = None  # относит. путь к zip
        self._clips_ready = asyncio.Event()  # webhook/поллинг -> «можно проверять»
        self._stop = False

    @property
    def dir(self) -> Path:
        return DATA_DIR / self.id

    def add_log(self, msg: str):
        self.log.append(msg)
        # не даём логу разрастаться бесконечно
        if len(self.log) > 500:
            self.log = self.log[-500:]

    def stop(self):
        self._stop = True
        self._clips_ready.set()

    def notify_clips_ready(self):
        self._clips_ready.set()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "video_url": self.video_url,
            "resolutions": self.resolutions,
            "status": self.status,
            "project_id": self.project_id,
            "error": self.error,
            "log": self.log[-60:],
            "clips": self.clips,
            "outputs": self.outputs,
            "progress": self.progress,
            "bundle": self.bundle,
        }


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def create(self, video_url: str, resolutions: list[str],
               curation_pref: Optional[dict] = None, min_score: int = 0) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, video_url, resolutions, curation_pref, min_score)
        job.dir.mkdir(parents=True, exist_ok=True)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)


store = JobStore()


# =============================================================================
# Пайплайн
# =============================================================================
async def run_job(job: Job, opus: OpusClient, webhook_url: Optional[str]):
    try:
        await _create_project(job, opus, webhook_url)
        clips = await _wait_for_clips(job, opus)
        if job._stop:
            job.status = "stopped"
            return
        await _download_and_resize(job, clips)
        if job._stop:
            job.status = "stopped"
            return
        _bundle_zip(job)
        job.status = "done"
        job.add_log("✓ Готово")
    except OpusError as e:
        job.status = "error"
        job.error = f"OpusClip: {e} {('— ' + e.body) if e.body else ''}".strip()
        job.add_log(f"✗ {job.error}")
    except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку
        job.status = "error"
        job.error = str(e)
        job.add_log(f"✗ Ошибка: {e}")


async def _create_project(job: Job, opus: OpusClient, webhook_url: Optional[str]):
    job.status = "creating"
    job.add_log(f"Создаём проект OpusClip для {job.video_url}")
    hook = None
    if webhook_url:
        hook = f"{webhook_url}?job={job.id}"
    data = await opus.create_project(
        job.video_url, curation_pref=job.curation_pref, webhook_url=hook)
    pid = project_id_from_response(data)
    if not pid:
        raise OpusError(f"Не удалось получить projectId из ответа: {data}")
    job.project_id = pid
    job.status = "clipping"
    job.add_log(f"Проект создан: {pid}. Ждём нарезку…")


async def _wait_for_clips(job: Job, opus: OpusClient) -> list[dict]:
    """Поллим exportable-clips, пока не появятся клипы (webhook ускоряет проверку)."""
    waited = 0.0
    while waited < POLL_TIMEOUT_SEC and not job._stop:
        try:
            clips = await opus.list_exportable_clips(job.project_id)
        except OpusError as e:
            # 404/425 «ещё не готово» — не падаем, ждём дальше
            if e.status in (404, 425, 409):
                clips = []
            else:
                raise
        ready = [c for c in clips if clip_download_url(c)]
        if ready:
            job.add_log(f"Найдено клипов: {len(ready)}")
            if job.min_score:
                before = len(ready)
                ready = [c for c in ready if (clip_score(c) or 0) >= job.min_score]
                job.add_log(
                    f"Фильтр рейтинга ≥ {job.min_score}: оставлено {len(ready)} из {before}")
                if not ready:
                    job.add_log("Под порог рейтинга не прошёл ни один клип", "warn")
            job.clips = [
                {"title": clip_title(c, f"clip_{i+1}"),
                 "url": clip_download_url(c),
                 "score": clip_score(c)}
                for i, c in enumerate(ready)
            ]
            return ready
        # ждём интервал ИЛИ сигнал от вебхука
        try:
            await asyncio.wait_for(job._clips_ready.wait(), timeout=POLL_INTERVAL_SEC)
            job._clips_ready.clear()
        except asyncio.TimeoutError:
            pass
        waited += POLL_INTERVAL_SEC
    if job._stop:
        return []
    raise TimeoutError("OpusClip не вернул клипы за отведённое время")


async def _download_and_resize(job: Job, clips: list[dict]):
    src_dir = job.dir / "src"
    out_dir = job.dir / "out"
    src_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_units = len(clips) * max(1, len(job.resolutions))
    done_units = 0

    job.status = "downloading"
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        for i, clip in enumerate(clips):
            if job._stop:
                return
            url = clip_download_url(clip)
            title = clip_title(clip, f"clip_{i+1}")
            safe = f"{i+1:02d}_{_safe_name(title)}"
            src_path = src_dir / f"{safe}.mp4"

            job.add_log(f"[{i+1}/{len(clips)}] Скачиваем: {title}")
            await _download(client, url, src_path)

            job.status = "resizing"
            for res_key in job.resolutions:
                if job._stop:
                    return
                job.add_log(f"   ресайз → {res_key}")
                out_name = f"{safe}_{res_key}"
                dst = await asyncio.to_thread(
                    rsz.resize_file, src_path, res_key, out_dir,
                    out_name=out_name,
                    stop_check=lambda: job._stop,
                )
                rel = str(dst.relative_to(DATA_DIR))
                job.outputs.append({"name": dst.name, "file": rel})
                done_units += 1
                job.progress = int(done_units * 100 / total_units)


async def _download(client: httpx.AsyncClient, url: str, dst: Path):
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with open(dst, "wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 17):
                fh.write(chunk)


def _bundle_zip(job: Job):
    if not job.outputs:
        return
    zip_path = job.dir / "clips_resized.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for out in job.outputs:
            fpath = DATA_DIR / out["file"]
            if fpath.exists():
                zf.write(fpath, arcname=out["name"])
    job.bundle = str(zip_path.relative_to(DATA_DIR))
