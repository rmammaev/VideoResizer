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
FADE_DURATION = 0.5


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


def _build_filter_resize_packshot(target_w, target_h, src_has_audio, pack_has_audio):
    """Ресайз исходника + пекшота и склейка (пекшот дописывается в конец). app.py:383."""
    parts = [
        _fc_resize_chunk("[0:v]", "[v0]", target_w, target_h, "s"),
        _fc_resize_chunk("[1:v]", "[v1]", target_w, target_h, "p"),
        _fc_audio_chunk("[0:a]", "[a0]", src_has_audio),
        _fc_audio_chunk("[1:a]", "[a1]", pack_has_audio),
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
    ]
    return ";".join(parts), ["-map", "[v]", "-map", "[a]"]


def _fc_audio_chunk(in_label, out_label, has_audio):
    if has_audio:
        return (f"{in_label}aresample=async=1,"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{out_label}")
    return f"anullsrc=channel_layout=stereo:sample_rate=44100{out_label}"


def _build_filter_concat_simple(n_inputs, target_w, target_h, has_audios):
    parts = []
    for i in range(n_inputs):
        parts.append(_fc_resize_chunk(f"[{i}:v]", f"[v{i}]", target_w, target_h, f"c{i}"))
        parts.append(_fc_audio_chunk(f"[{i}:a]", f"[a{i}]", has_audios[i]))
    chain = "".join(f"[v{i}][a{i}]" for i in range(n_inputs))
    parts.append(f"{chain}concat=n={n_inputs}:v=1:a=1[v][a]")
    return ";".join(parts), ["-map", "[v]", "-map", "[a]"]


def _build_filter_concat_xfade(n_inputs, target_w, target_h, has_audios, durations,
                               fade_dur=FADE_DURATION):
    parts = []
    for i in range(n_inputs):
        parts.append(_fc_resize_chunk(f"[{i}:v]", f"[v{i}]", target_w, target_h, f"c{i}"))
        parts.append(_fc_audio_chunk(f"[{i}:a]", f"[a{i}]", has_audios[i]))
    cur_v, cur_a = "v0", "a0"
    cur_dur = durations[0]
    for i in range(1, n_inputs):
        new_v, new_a = f"vx{i}", f"ax{i}"
        offset = max(0.0, cur_dur - fade_dur)
        parts.append(f"[{cur_v}][v{i}]xfade=transition=fade:"
                     f"duration={fade_dur:.3f}:offset={offset:.3f}[{new_v}]")
        parts.append(f"[{cur_a}][a{i}]acrossfade=d={fade_dur:.3f}:c1=tri:c2=tri[{new_a}]")
        cur_dur = cur_dur + durations[i] - fade_dur
        cur_v, cur_a = new_v, new_a
    parts.append(f"[{cur_v}]setsar=1[v]")
    parts.append(f"[{cur_a}]anull[a]")
    return ";".join(parts), ["-map", "[v]", "-map", "[a]"]


def concat_files(
    files: list,
    res_key: str,
    out_dir: Path,
    *,
    fade: bool = False,
    out_name: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Склейка нескольких видео в одно (с ресайзом каждого в res_key). fade=плавный переход."""
    if res_key not in RESOLUTIONS:
        raise ValueError(f"Неизвестное разрешение: {res_key}")
    files = [Path(f) for f in files]
    if len(files) < 2:
        raise ValueError("Для склейки нужно минимум 2 файла")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_w, target_h = RESOLUTIONS[res_key]

    infos = [ffprobe_info(f) for f in files]
    has_audios = [i["has_audio"] for i in infos]
    durations = [max(0.05, i["duration"]) for i in infos]
    n = len(files)

    if fade and n >= 2:
        fc, maps = _build_filter_concat_xfade(n, target_w, target_h, has_audios, durations)
        total_dur = sum(durations) - FADE_DURATION * (n - 1)
    else:
        fc, maps = _build_filter_concat_simple(n, target_w, target_h, has_audios)
        total_dur = sum(durations)
    total_us = max(1, int(total_dur * 1_000_000))

    stem = out_name or f"concat_{res_key}"
    dst = _unique_path(out_dir / f"{stem}.mp4")

    cmd = ["ffmpeg", "-y"]
    for f in files:
        cmd += ["-i", str(f)]
    cmd += ["-filter_complex", fc, *maps,
            "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
            "-pix_fmt", PIX_FMT, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-loglevel", "error",
            str(dst)]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    rx_time = re.compile(r"^out_time_us=(-?\d+)$")
    last = -1
    try:
        for line in proc.stdout:
            if stop_check and stop_check():
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: proc.kill()
                raise InterruptedError("Остановлено")
            line = line.strip()
            m = rx_time.match(line)
            if m and progress_cb:
                pct = int(min(100, max(0, int(m.group(1))) * 100 / total_us))
                if pct != last:
                    last = pct
                    progress_cb(pct)
    finally:
        err = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
    if rc != 0:
        try:
            if dst.exists(): dst.unlink()
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg concat rc={rc}: {err.strip()[:300]}")
    return dst


def resize_file(
    src: Path,
    res_key: str,
    out_dir: Path,
    *,
    out_name: Optional[str] = None,
    packshot: Optional[Path] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """
    Ресайзит один файл в одно разрешение. Возвращает путь к результату.
    packshot — опц. концевой клип, дописывается в конец (тоже ресайзится в res_key).
    progress_cb(pct) вызывается по ходу (0..100). stop_check() → True прерывает.
    """
    if res_key not in RESOLUTIONS:
        raise ValueError(f"Неизвестное разрешение: {res_key}")
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    target_w, target_h = RESOLUTIONS[res_key]
    info = ffprobe_info(src)

    pack_info = None
    if packshot is not None:
        packshot = Path(packshot)
        pack_info = ffprobe_info(packshot)
        fc, maps = _build_filter_resize_packshot(
            target_w, target_h, info["has_audio"], pack_info["has_audio"])
        dur = max(0.05, info["duration"]) + max(0.05, pack_info["duration"])
    else:
        fc, maps = _build_filter_resize_only(target_w, target_h, info["has_audio"])
        dur = max(0.05, info["duration"])
    total_us = max(1, int(dur * 1_000_000))

    stem = out_name or f"{src.stem}_{res_key}"
    dst = _unique_path(out_dir / f"{stem}.mp4")

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if packshot is not None:
        cmd += ["-i", str(packshot)]
    cmd += [
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
