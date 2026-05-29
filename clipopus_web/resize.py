"""
Чистая ffmpeg-логика ресайза, перенесённая из VideoResizer/app.py.

Без зависимостей от pywebview/desktop. Подход: вписываем видео в целевой формат
поверх размытого фона того же кадра (blur-fit), как в app.py:357 fc_resize_chunk.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

# --- параметры энкодера (app.py:157-184) ---------------------------------
RESOLUTIONS = {
    "1080x1080": (1080, 1080),   # 1:1
    "1080x1350": (1080, 1350),   # 4:5
    "1080x1920": (1080, 1920),   # 9:16
    "1920x1080": (1920, 1080),   # 16:9
}
DEFAULT_RES = "1080x1920"

BLUR_SIGMA = 30
FPS = 30
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "medium"
VIDEO_CRF = "18"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
PIX_FMT = "yuv420p"


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def ffprobe_info(path) -> dict:
    """Длительность + наличие аудио (app.py:209)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", str(path)]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    data = json.loads(out)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if video is None:
        raise ValueError(f"Нет видеопотока: {Path(path).name}")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": audio is not None,
        "duration": duration,
    }


def _fc_resize_chunk(in_label, out_label, target_w, target_h, suffix="s") -> str:
    """blur-фон + вписанный по центру кадр (app.py:357)."""
    return (
        f"{in_label}split=2[base_{suffix}][blur_{suffix}];"
        f"[blur_{suffix}]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},gblur=sigma={BLUR_SIGMA}[bg_{suffix}];"
        f"[base_{suffix}]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg_{suffix}];"
        f"[bg_{suffix}][fg_{suffix}]overlay=(W-w)/2:(H-h)/2,"
        f"setsar=1,fps={FPS},format={PIX_FMT}{out_label}"
    )


def _build_filter_resize_only(target_w, target_h, src_has_audio):
    fc = _fc_resize_chunk("[0:v]", "[v]", target_w, target_h, "s")
    maps = ["-map", "[v]"]
    if src_has_audio:
        maps += ["-map", "0:a"]
    return fc, maps


def resize_file(
    src: Path,
    res_key: str,
    out_dir: Path,
    *,
    out_name: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """
    Ресайзит один файл в одно разрешение. Возвращает путь к результату.
    progress_cb(pct) вызывается по ходу (0..100). stop_check() → True прерывает.
    """
    if res_key not in RESOLUTIONS:
        raise ValueError(f"Неизвестное разрешение: {res_key}")
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    target_w, target_h = RESOLUTIONS[res_key]
    info = ffprobe_info(src)
    fc, maps = _build_filter_resize_only(target_w, target_h, info["has_audio"])

    dur = max(0.05, info["duration"])
    total_us = max(1, int(dur * 1_000_000))

    stem = out_name or f"{src.stem}_{res_key}"
    dst = _unique_path(out_dir / f"{stem}.mp4")

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex", fc, *maps,
        "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
        "-pix_fmt", PIX_FMT, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(dst),
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    rx_time = re.compile(r"^out_time_us=(-?\d+)$")
    last_pct = -1
    try:
        for line in proc.stdout:
            if stop_check and stop_check():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise InterruptedError("Остановлено")
            line = line.strip()
            m = rx_time.match(line)
            if m and progress_cb:
                us = max(0, int(m.group(1)))
                pct = int(min(100, us * 100 / total_us))
                if pct != last_pct:
                    last_pct = pct
                    progress_cb(pct)
            elif line == "progress=end" and progress_cb:
                progress_cb(100)
    finally:
        err = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()

    if rc != 0:
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg rc={rc}: {err.strip()[:300]}")

    return dst


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1
