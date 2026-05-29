#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Resizer v4 для macOS — pywebview + HTML/CSS/JS UI.

Все фичи Windows-версии:
  - Ресайз 1 файл → N выбранных разрешений
  - 4 разных пекшота под каждое разрешение
  - Склейка 2–10 файлов → один файл в каждом из выбранных разрешений
  - Фейд-переход между клипами в склейке
  - OS-шаблон нейминга / "сохранить исходный нейминг" / свой шаблон
  - Авто-извлечение имени из исходника и инициалы для склейки
  - Лимит размера 100 МБ
"""

from __future__ import annotations

import os
import sys

# macOS: при запуске из .app PATH урезан Finder-ом — добавим Homebrew
if sys.platform == "darwin":
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    _extras = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    _paths = os.environ.get("PATH", "").split(":")
    for _p in _extras:
        if _p not in _paths and os.path.isdir(_p):
            _paths.insert(0, _p)
    os.environ["PATH"] = ":".join(filter(None, _paths))

import datetime as _dt
import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse as _urlparse
from pathlib import Path

import webview


# =============================================================================
# HTTP video preview server (local, supports Range requests for seeking)
# =============================================================================
_VIDEO_PORT = None
_VIDEO_PORT_LOCK = threading.Lock()


import http.server as _http_server


class _VHandler(_http_server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._serve(head_only=False)

    def do_HEAD(self):
        self._serve(head_only=True)

    def _serve(self, head_only=False):
        params = _urlparse.parse_qs(_urlparse.urlparse(self.path).query)
        raw = params.get("p", [None])[0]
        if not raw:
            self._err(400); return
        path = _urlparse.unquote(raw)
        if not os.path.isfile(path):
            self._err(404); return
        size = os.path.getsize(path)
        rng = self.headers.get("Range", "")
        try:
            fh = open(path, "rb")
        except OSError:
            self._err(403); return
        try:
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                start = int(m.group(1)) if m else 0
                end = int(m.group(2)) if (m and m.group(2)) else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if not head_only:
                    fh.seek(start)
                    self.wfile.write(fh.read(length))
            else:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if not head_only:
                    shutil.copyfileobj(fh, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            fh.close()

    def _err(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass  # silence console output


def _ensure_video_server():
    global _VIDEO_PORT
    with _VIDEO_PORT_LOCK:
        if _VIDEO_PORT is not None:
            return _VIDEO_PORT
        import socketserver
        srv = socketserver.TCPServer(("127.0.0.1", 0), _VHandler)
        srv.allow_reuse_address = True
        _VIDEO_PORT = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return _VIDEO_PORT


# =============================================================================
# Параметры обработки
# =============================================================================
APP_TITLE = "Video Resizer"

# Папка приложения по умолчанию: ~/VideoResizer/...
APP_DIR = Path.home() / "VideoResizer"
DEFAULT_PACKSHOT_NAME_TPL = "ID01788_OS_In-House_3D_Packshot-November_10-25_EN_4s_{res}.mp4"
DEFAULT_PACKSHOT_DIRS = [
    str(APP_DIR / "PackShot"),
    str(APP_DIR),
]


def _find_default_packshots():
    """Возвращает {res_key: path_or_empty} для каждого разрешения."""
    result = {}
    for res in ("1080x1080", "1080x1350", "1080x1920", "1920x1080"):
        result[res] = ""
        for d in DEFAULT_PACKSHOT_DIRS:
            cand = Path(d) / DEFAULT_PACKSHOT_NAME_TPL.format(res=res)
            if cand.exists():
                result[res] = str(cand)
                break
    return result


RESOLUTIONS = {
    "1080x1080": (1080, 1080),
    "1080x1350": (1080, 1350),
    "1080x1920": (1080, 1920),
    "1920x1080": (1920, 1080),
}
DEFAULT_RES = "1080x1350"

KNOWN_TEAMS = ["In-House", "Freelance"]
KNOWN_TYPES = ["Gameplay", "Unreal", "Cinematic", "Combo", "UGC", "AI", "AI-Hook"]
_PARSE_TYPES = set(KNOWN_TYPES) | {"Сinematic", "Hook"}
_PARSE_TEAMS = set(KNOWN_TEAMS)
_RX_SPECIAL = re.compile(r"^(EN|\d{2}-\d{2}|\d+s|\d+x\d+)$")
_RX_RES_SUFFIX = re.compile(r"_\d+x\d+$")

DEFAULT_TEMPLATE_RESIZE = "{name}_{resolution}"
DEFAULT_TEMPLATE_CONCAT = "concat_{date}_{resolution}"

BLUR_SIGMA = 30
FPS = 30
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "medium"
VIDEO_CRF = "18"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
AUDIO_KBPS = 192
PIX_FMT = "yuv420p"
FADE_DURATION = 0.5

SIZE_LIMIT_MB = 100
SIZE_LIMIT_SAFETY = 0.95
ACCEPT_EXT = {".mp4", ".mov", ".m4v", ".mkv"}


# =============================================================================
# Утилиты
# =============================================================================
def have_ffmpeg():
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def compute_video_kbps(target_size_mb, duration_sec, audio_kbps):
    if duration_sec <= 0 or target_size_mb <= 0:
        return 0
    target_bits = target_size_mb * 1024 * 1024 * 8 * SIZE_LIMIT_SAFETY
    audio_bits = audio_kbps * 1000 * duration_sec
    video_bits = target_bits - audio_bits
    if video_bits <= 0:
        return 0
    return int(video_bits / duration_sec / 1000)


def ffprobe_info(path):
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


def unique_path(p):
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "untitled"


# =============================================================================
# Naming parsers + NamingConfig
# =============================================================================
def extract_name_from_filename(stem):
    """Best-effort извлечение 'имени' из стандартного OS-нейминга."""
    parts = stem.split("_")
    try:
        os_idx = parts.index("OS")
    except ValueError:
        return None
    i = os_idx + 1
    if i < len(parts) and parts[i] in _PARSE_TEAMS:
        i += 1
    skips = 0
    while i < len(parts) and parts[i] in _PARSE_TYPES and skips < 2:
        i += 1
        skips += 1
    if i >= len(parts) or _RX_SPECIAL.match(parts[i]):
        return None
    return parts[i]


def concat_initials_from_stems(stems):
    """['Roulette-Update', 'Trafic-Control', 'Taxi-Patrol'] → 'RU-TC-TP'."""
    inits = []
    for stem in stems:
        name = extract_name_from_filename(stem) or stem
        words = [w for w in name.split("-") if w]
        ini = "".join(w[0].upper() for w in words) if words else "X"
        inits.append(ini)
    return "-".join(inits)


def keep_original_naming(src_stem, res_key):
    new = _RX_RES_SUFFIX.sub(f"_{res_key}", src_stem)
    if new == src_stem:
        new = f"{src_stem}_{res_key}"
    return new


def format_date_short(now=None):
    return (now or _dt.datetime.now()).strftime("%m-%y")


def format_duration(seconds):
    return f"{int(round(max(seconds, 0)))}s"


def render_template(template, *, name, tag, resolution, index=1):
    date_str = _dt.datetime.now().strftime("%Y-%m-%d")
    vars_ = {"name": name, "tag": tag or "", "resolution": resolution,
             "date": date_str, "index": f"{index:02d}"}
    result = template
    if not tag:
        result = re.sub(r"_\{tag\}", "", result, count=1)
        result = re.sub(r"\{tag\}_", "", result, count=1)
        result = result.replace("{tag}", "")
    for k, v in vars_.items():
        result = result.replace("{" + k + "}", str(v))
    result = re.sub(r"\{[^}]*\}", "", result)
    result = re.sub(r"__+", "_", result)
    result = result.strip("_- ")
    return safe_filename(result) or "output"


class NamingConfig:
    MODE_OS = "os"
    MODE_KEEP = "keep"
    MODE_CUSTOM = "custom"

    def __init__(self, *, mode="os",
                 team="In-House", type_="Unreal",
                 name_source="auto", name_manual="",
                 template=DEFAULT_TEMPLATE_RESIZE, tag=""):
        self.mode = mode
        self.team = team
        self.type = type_
        self.name_source = name_source
        self.name_manual = (name_manual or "").strip()
        self.template = template
        self.tag = (tag or "").strip()

    def make_name(self, *, src_stem, res_key, output_duration_sec,
                  all_src_stems=None):
        if self.mode == self.MODE_KEEP:
            return keep_original_naming(src_stem, res_key)
        if self.mode == self.MODE_CUSTOM:
            return render_template(self.template, name=src_stem,
                                   tag=self.tag, resolution=res_key)
        # OS-режим
        if all_src_stems and len(all_src_stems) > 1:
            if self.name_source == "manual" and self.name_manual:
                name_part = self.name_manual
            else:
                name_part = concat_initials_from_stems(all_src_stems) or "VIDEO"
        else:
            if self.name_source == "manual" and self.name_manual:
                name_part = self.name_manual
            else:
                name_part = extract_name_from_filename(src_stem) or src_stem
        date_s = format_date_short()
        dur_s = format_duration(output_duration_sec)
        full = f"OS_{self.team}_{self.type}_{name_part}_{date_s}_EN_{dur_s}_{res_key}"
        return safe_filename(full)


# =============================================================================
# Filter builders
# =============================================================================
def fc_resize_chunk(in_label, out_label, target_w, target_h, suffix):
    return (
        f"{in_label}split=2[base_{suffix}][blur_{suffix}];"
        f"[blur_{suffix}]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},gblur=sigma={BLUR_SIGMA}[bg_{suffix}];"
        f"[base_{suffix}]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg_{suffix}];"
        f"[bg_{suffix}][fg_{suffix}]overlay=(W-w)/2:(H-h)/2,"
        f"setsar=1,fps={FPS},format={PIX_FMT}{out_label}"
    )


def fc_audio_chunk(in_label, out_label, has_audio):
    if has_audio:
        return (f"{in_label}aresample=async=1,"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{out_label}")
    return f"anullsrc=channel_layout=stereo:sample_rate=44100{out_label}"


def build_filter_resize_only(target_w, target_h, src_has_audio):
    fc = fc_resize_chunk("[0:v]", "[v]", target_w, target_h, "s")
    maps = ["-map", "[v]"]
    if src_has_audio:
        maps += ["-map", "0:a"]
    return fc, maps


def build_filter_resize_packshot(target_w, target_h, src_has_audio, pack_has_audio):
    parts = [
        fc_resize_chunk("[0:v]", "[v0]", target_w, target_h, "s"),
        fc_resize_chunk("[1:v]", "[v1]", target_w, target_h, "p"),
        fc_audio_chunk("[0:a]", "[a0]", src_has_audio),
        fc_audio_chunk("[1:a]", "[a1]", pack_has_audio),
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
    ]
    return ";".join(parts), ["-map", "[v]", "-map", "[a]"]


def build_filter_concat_simple(n_inputs, target_w, target_h, has_audios):
    parts = []
    for i in range(n_inputs):
        parts.append(fc_resize_chunk(f"[{i}:v]", f"[v{i}]", target_w, target_h, f"c{i}"))
        parts.append(fc_audio_chunk(f"[{i}:a]", f"[a{i}]", has_audios[i]))
    chain = "".join(f"[v{i}][a{i}]" for i in range(n_inputs))
    parts.append(f"{chain}concat=n={n_inputs}:v=1:a=1[v][a]")
    return ";".join(parts), ["-map", "[v]", "-map", "[a]"]


def build_filter_concat_xfade(n_inputs, target_w, target_h, has_audios, durations,
                              fade_dur=FADE_DURATION):
    parts = []
    for i in range(n_inputs):
        parts.append(fc_resize_chunk(f"[{i}:v]", f"[v{i}]", target_w, target_h, f"c{i}"))
        parts.append(fc_audio_chunk(f"[{i}:a]", f"[a{i}]", has_audios[i]))
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


# =============================================================================
# Workers
# =============================================================================
class ResizeWorker(threading.Thread):
    def __init__(self, window, jobs, packshots, out_dir,
                 max_size_mb=None, naming_config=None,
                 trim_start=0.0, trim_end=None,
                 subtitle_segments=None, subtitle_style=None):
        super().__init__(daemon=True)
        self.window = window
        self.jobs = jobs  # [(Path, [res_key1, res_key2, ...])]
        self.packshots = packshots  # None или {res_key: path_or_None}
        self.out_dir = out_dir  # Path или None
        self.max_size_mb = max_size_mb
        self.naming_config = naming_config or NamingConfig(
            mode=NamingConfig.MODE_CUSTOM, template=DEFAULT_TEMPLATE_RESIZE)
        self.trim_start = float(trim_start or 0.0)
        self.trim_end = float(trim_end) if trim_end is not None else None
        self.subtitle_segments = subtitle_segments or []
        self.subtitle_style = subtitle_style or {}
        self.stop_event = threading.Event()
        self.current_proc = None

    def _js(self, fn, *args):
        try:
            payload = json.dumps(args, ensure_ascii=False)
            self.window.evaluate_js(f"window.api.{fn}.apply(null, {payload})")
        except Exception:
            pass

    def log(self, text, level="info"):
        self._js("onLog", text, level)

    def stop(self):
        self.stop_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
            except Exception: pass

    def process_one(self, idx, total, src, res_key):
        target_w, target_h = RESOLUTIONS[res_key]
        try:
            src_info = ffprobe_info(src)
        except Exception as e:
            self.log(f"{src.name}: {e}", "error")
            return False

        # Выбираем нужный пекшот под текущее разрешение (с fallback)
        packshot_path = None
        if self.packshots is not None:
            packshot_path = self.packshots.get(res_key)
            if packshot_path is None:
                fallback = next((v for v in self.packshots.values() if v), None)
                if fallback is not None:
                    self.log(f"Нет пекшота для {res_key}, используем {Path(fallback).name}", "warn")
                    packshot_path = fallback

        pack_info = None
        if packshot_path is not None:
            try:
                pack_info = ffprobe_info(packshot_path)
            except Exception as e:
                self.log(f"Пекшот: {e}", "error")
                return False

        if packshot_path is not None:
            fc, maps = build_filter_resize_packshot(
                target_w, target_h, src_info["has_audio"], pack_info["has_audio"])
        else:
            fc, maps = build_filter_resize_only(
                target_w, target_h, src_info["has_audio"])

        # Определяем используемый кусок исходника (с учётом пользовательского трима)
        src_dur = src_info["duration"]
        clip_start = max(0.0, self.trim_start)
        clip_end = self.trim_end  # None = до конца исходника

        if clip_end is not None:
            clip_dur = max(0.05, min(float(clip_end), src_dur) - clip_start)
        else:
            clip_dur = max(0.05, src_dur - clip_start)

        # Аргументы seek/trim для input 0 (исходник)
        cmd_trim = []
        if clip_start > 0.01:
            cmd_trim += ["-ss", f"{clip_start:.3f}"]
        cmd_trim += ["-t", f"{clip_dur:.3f}"]

        # Пекшот добавляется КАК ДОПОЛНИТЕЛЬНОЕ время в конец (не вычитается!)
        if packshot_path is not None:
            pack_dur = pack_info["duration"]
            total_us = int((clip_dur + pack_dur) * 1_000_000)
        else:
            total_us = int(clip_dur * 1_000_000)
        total_us = max(total_us, 1)
        out_dur = total_us / 1_000_000.0

        out_name = self.naming_config.make_name(
            src_stem=src.stem, res_key=res_key, output_duration_sec=out_dur)
        out_dir = self.out_dir if self.out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = unique_path(out_dir / f"{out_name}.mp4")

        # CRF vs ABR
        if self.max_size_mb and out_dur > 0:
            kbps = compute_video_kbps(self.max_size_mb, out_dur, AUDIO_KBPS)
            if kbps <= 0:
                self.log(f"{src.name}: не уложиться в {self.max_size_mb} МБ", "error")
                return False
            rate_opts = ["-b:v", f"{kbps}k",
                         "-maxrate", f"{int(kbps * 1.5)}k",
                         "-bufsize", f"{int(kbps * 2)}k"]
        else:
            rate_opts = ["-crf", VIDEO_CRF]

        cmd = ["ffmpeg", "-y", *cmd_trim, "-i", str(src)]
        if packshot_path is not None:
            cmd += ["-i", str(packshot_path)]
        cmd += ["-filter_complex", fc, *maps,
                "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, *rate_opts,
                "-pix_fmt", PIX_FMT, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                str(dst)]

        self.log(f"({idx}/{total}) {src.name} → {res_key} ⇒ {dst.name}", "info")
        self._js("onCurrentFile", idx, total, f"{src.name}  →  {res_key}")

        try:
            self.current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            self.log("FFmpeg не найден в PATH", "error")
            return False

        rx_time = re.compile(r"^out_time_us=(-?\d+)$")
        last_pct = -1
        for line in self.current_proc.stdout:
            if self.stop_event.is_set():
                self.log("Остановлено пользователем", "warn")
                return False
            line = line.strip()
            m = rx_time.match(line)
            if m:
                us = max(0, int(m.group(1)))
                pct = int(min(100, us * 100 / total_us))
                if pct != last_pct:
                    last_pct = pct
                    self._js("onFileProgress", idx, total, pct)
            elif line == "progress=end":
                self._js("onFileProgress", idx, total, 100)

        err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
        rc = self.current_proc.wait()
        self.current_proc = None

        if rc != 0:
            self.log(f"Ошибка ({rc}): {err.strip()[:200]}", "error")
            try:
                if dst.exists(): dst.unlink()
            except Exception: pass
            return False

        self.log(f"Готово: {dst.name}", "success")

        if self.subtitle_segments:
            tmp = dst.with_name("_ss_" + dst.name)
            dst.rename(tmp)
            ok = self._burn_subs(tmp, dst, total_us, idx, total)
            try:
                tmp.unlink()
            except Exception:
                pass
            return ok

        return True

    def _burn_subs(self, src_path, dst_path, total_us, job_idx=1, job_total=1):
        """Burn subtitle ASS file into video as a second pass."""
        # Write ASS to a simple /tmp path — no special chars, avoids FFmpeg filter parser issues on macOS
        _ass_simple = f"/tmp/vr_bs_{id(self) & 0xFFFF:04x}.ass"
        ass_path = Path(_ass_simple)
        try:
            ass_content = make_ass_content(self.subtitle_segments, self.subtitle_style)
            with open(_ass_simple, "w", encoding="utf-8") as f:
                f.write(ass_content)

            cmd = [
                "ffmpeg", "-y", "-i", str(src_path),
                "-vf", f"ass=filename={_ass_simple}",
                "-c:a", "copy",
                "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET,
                "-crf", VIDEO_CRF,
                "-pix_fmt", PIX_FMT,
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                str(dst_path),
            ]

            self.log(f"Вжигаем субтитры → {dst_path.name}", "info")

            try:
                self.current_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
            except FileNotFoundError:
                self.log("FFmpeg не найден в PATH", "error")
                return False

            rx_time = re.compile(r"^out_time_us=(-?\d+)$")
            last_pct = -1
            for line in self.current_proc.stdout:
                if self.stop_event.is_set():
                    self.log("Остановлено пользователем", "warn")
                    return False
                line = line.strip()
                m = rx_time.match(line)
                if m:
                    us = max(0, int(m.group(1)))
                    pct = int(min(100, us * 100 / max(total_us, 1)))
                    if pct != last_pct:
                        last_pct = pct
                        self._js("onFileProgress", job_idx, job_total, pct)
                elif line == "progress=end":
                    self._js("onFileProgress", job_idx, job_total, 100)

            err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
            rc = self.current_proc.wait()
            self.current_proc = None

            if rc != 0:
                self.log(f"Ошибка вжигания субтитров ({rc}): {err.strip()[:200]}", "error")
                try:
                    if dst_path.exists(): dst_path.unlink()
                except Exception:
                    pass
                return False

            return True

        finally:
            try:
                ass_path.unlink()
            except Exception:
                pass

    def run(self):
        flat = [(src, r) for src, resolutions in self.jobs for r in resolutions]
        total = len(flat)
        ok = 0
        for i, (src, res_key) in enumerate(flat, start=1):
            if self.stop_event.is_set(): break
            if self.process_one(i, total, src, res_key):
                ok += 1
            self._js("onOverallProgress", i, total)
        self._js("onDone", ok, total, self.stop_event.is_set())


class ConcatWorker(threading.Thread):
    def __init__(self, window, files, resolutions, out_dir,
                 fade=False, max_size_mb=None, naming_config=None,
                 trims=None):
        super().__init__(daemon=True)
        self.window = window
        self.files = files
        self.resolutions = resolutions
        self.out_dir = out_dir
        self.fade = fade
        self.max_size_mb = max_size_mb
        self.naming_config = naming_config or NamingConfig(
            mode=NamingConfig.MODE_CUSTOM, template=DEFAULT_TEMPLATE_CONCAT)
        # trims: list of {start, end} dicts (or {} / None) per file
        self.trims = trims or []
        self.stop_event = threading.Event()
        self.current_proc = None

    def _js(self, fn, *args):
        try:
            payload = json.dumps(args, ensure_ascii=False)
            self.window.evaluate_js(f"window.api.{fn}.apply(null, {payload})")
        except Exception:
            pass

    def log(self, text, level="info"):
        self._js("onLog", text, level)

    def stop(self):
        self.stop_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
            except Exception: pass

    def _process_one_resolution(self, job_idx, job_total, res_key, has_audios, durations):
        target_w, target_h = RESOLUTIONS[res_key]
        n = len(self.files)

        if self.fade and n >= 2:
            fc, maps = build_filter_concat_xfade(n, target_w, target_h, has_audios, durations)
            total_dur = sum(durations) - FADE_DURATION * (n - 1)
        else:
            fc, maps = build_filter_concat_simple(n, target_w, target_h, has_audios)
            total_dur = sum(durations)

        total_us = max(int(total_dur * 1_000_000), 1)

        if self.max_size_mb and total_dur > 0:
            kbps = compute_video_kbps(self.max_size_mb, total_dur, AUDIO_KBPS)
            if kbps <= 0:
                self.log(f"Не уложиться в {self.max_size_mb} МБ ({res_key})", "error")
                return False
            rate_opts = ["-b:v", f"{kbps}k",
                         "-maxrate", f"{int(kbps * 1.5)}k",
                         "-bufsize", f"{int(kbps * 2)}k"]
        else:
            rate_opts = ["-crf", VIDEO_CRF]

        all_stems = [f.stem for f in self.files]
        out_name = self.naming_config.make_name(
            src_stem=all_stems[0], res_key=res_key,
            output_duration_sec=total_dur, all_src_stems=all_stems)
        out_path = unique_path(self.out_dir / f"{out_name}.mp4")

        cmd = ["ffmpeg", "-y"]
        for i, f in enumerate(self.files):
            trim = self.trims[i] if i < len(self.trims) else {}
            start = float(trim.get("start") or 0)
            if start > 0.01:
                cmd += ["-ss", f"{start:.3f}"]
            cmd += ["-t", f"{durations[i]:.3f}", "-i", str(f)]
        cmd += ["-filter_complex", fc, *maps,
                "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, *rate_opts,
                "-pix_fmt", PIX_FMT, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                str(out_path)]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.log(f"({job_idx}/{job_total}) Склейка → {res_key} ⇒ {out_path.name}", "info")
        self._js("onCurrentFile", job_idx, job_total,
                 f"Склейка {n} файлов → {res_key}")

        try:
            self.current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            self.log("FFmpeg не найден в PATH", "error")
            return False

        rx_time = re.compile(r"^out_time_us=(-?\d+)$")
        last_pct = -1
        for line in self.current_proc.stdout:
            if self.stop_event.is_set():
                self.log("Остановлено пользователем", "warn")
                return False
            line = line.strip()
            m = rx_time.match(line)
            if m:
                us = max(0, int(m.group(1)))
                pct = int(min(100, us * 100 / total_us))
                if pct != last_pct:
                    last_pct = pct
                    self._js("onFileProgress", job_idx, job_total, pct)
            elif line == "progress=end":
                self._js("onFileProgress", job_idx, job_total, 100)

        err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
        rc = self.current_proc.wait()
        self.current_proc = None

        if rc != 0:
            self.log(f"Ошибка ({rc}): {err.strip()[:200]}", "error")
            try:
                if out_path.exists(): out_path.unlink()
            except Exception: pass
            return False

        self.log(f"Готово: {out_path.name}", "success")
        return True

    def run(self):
        n = len(self.files)
        if n < 2:
            self.log("Нужно минимум 2 файла", "error")
            self._js("onDone", 0, 1, False)
            return

        infos = []
        for f in self.files:
            try:
                infos.append(ffprobe_info(f))
            except Exception as e:
                self.log(f"{f.name}: {e}", "error")
                self._js("onDone", 0, 1, False)
                return

        has_audios = [info["has_audio"] for info in infos]

        # Compute trimmed durations for each clip
        durations = []
        for i, info in enumerate(infos):
            raw_dur = info["duration"]
            trim = self.trims[i] if i < len(self.trims) else {}
            start = float(trim.get("start") or 0)
            end_raw = trim.get("end")
            end = float(end_raw) if end_raw is not None else raw_dur
            clipped = max(0.05, min(end, raw_dur) - max(0.0, start))
            durations.append(clipped)

        total = len(self.resolutions)
        ok = 0
        for i, res_key in enumerate(self.resolutions, start=1):
            if self.stop_event.is_set(): break
            if self._process_one_resolution(i, total, res_key, has_audios, durations):
                ok += 1
            self._js("onOverallProgress", i, total)

        self._js("onDone", ok, total, self.stop_event.is_set())


# =============================================================================
# ChainedConcatWorker — несколько разрешений × свои клипы, обрабатываем по очереди
# =============================================================================
class ChainedConcatWorker(threading.Thread):
    """Обрабатывает список jobs [{files, trims, resolution}] последовательно."""

    def __init__(self, window, jobs, out_dir,
                 fade=False, max_size_mb=None, naming_config=None):
        super().__init__(daemon=True)
        self.window = window
        self.jobs = jobs          # [{files:[Path], trims:[{start,end}], resolution:str}]
        self.out_dir = out_dir
        self.fade = fade
        self.max_size_mb = max_size_mb
        self.naming_config = naming_config or NamingConfig(
            mode=NamingConfig.MODE_CUSTOM, template=DEFAULT_TEMPLATE_CONCAT)
        self.stop_event = threading.Event()
        self.current_proc = None

    def _js(self, fn, *args):
        try:
            payload = json.dumps(args, ensure_ascii=False)
            self.window.evaluate_js(f"window.api.{fn}.apply(null, {payload})")
        except Exception:
            pass

    def log(self, text, level="info"):
        self._js("onLog", text, level)

    def stop(self):
        self.stop_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
            except Exception: pass

    def run(self):
        total = len(self.jobs)
        ok = 0

        for job_idx, job in enumerate(self.jobs, start=1):
            if self.stop_event.is_set():
                break

            files   = job["files"]
            res_key = job["resolution"]
            trims   = job.get("trims") or []
            n = len(files)

            if n < 2:
                self.log(f"[{job_idx}/{total}] {res_key}: нужно минимум 2 файла — пропуск", "warn")
                self._js("onOverallProgress", job_idx, total)
                continue

            # ffprobe каждый файл
            infos, err_flag = [], False
            for f in files:
                try:
                    infos.append(ffprobe_info(f))
                except Exception as e:
                    self.log(f"{f.name}: {e}", "error"); err_flag = True; break
            if err_flag:
                self._js("onOverallProgress", job_idx, total); continue

            has_audios = [i["has_audio"] for i in infos]

            # Вычисляем обрезанные длительности
            durations = []
            for i, info in enumerate(infos):
                raw = info["duration"]
                t = trims[i] if i < len(trims) else {}
                s = float(t.get("start") or 0)
                e_raw = t.get("end")
                e = float(e_raw) if e_raw is not None else raw
                durations.append(max(0.05, min(e, raw) - max(0.0, s)))

            target_w, target_h = RESOLUTIONS[res_key]
            if self.fade and n >= 2:
                fc, maps = build_filter_concat_xfade(n, target_w, target_h, has_audios, durations)
                total_dur = sum(durations) - FADE_DURATION * (n - 1)
            else:
                fc, maps = build_filter_concat_simple(n, target_w, target_h, has_audios)
                total_dur = sum(durations)
            total_us = max(int(total_dur * 1_000_000), 1)

            if self.max_size_mb and total_dur > 0:
                kbps = compute_video_kbps(self.max_size_mb, total_dur, AUDIO_KBPS)
                if kbps <= 0:
                    self.log(f"Не уложиться в {self.max_size_mb} МБ ({res_key})", "error")
                    self._js("onOverallProgress", job_idx, total); continue
                rate_opts = ["-b:v", f"{kbps}k",
                             "-maxrate", f"{int(kbps*1.5)}k",
                             "-bufsize", f"{int(kbps*2)}k"]
            else:
                rate_opts = ["-crf", VIDEO_CRF]

            all_stems = [f.stem for f in files]
            out_name = self.naming_config.make_name(
                src_stem=all_stems[0], res_key=res_key,
                output_duration_sec=total_dur, all_src_stems=all_stems)
            out_path = unique_path(self.out_dir / f"{out_name}.mp4")
            self.out_dir.mkdir(parents=True, exist_ok=True)

            cmd = ["ffmpeg", "-y"]
            for i, f in enumerate(files):
                t = trims[i] if i < len(trims) else {}
                ss = float(t.get("start") or 0)
                if ss > 0.01:
                    cmd += ["-ss", f"{ss:.3f}"]
                cmd += ["-t", f"{durations[i]:.3f}", "-i", str(f)]
            cmd += ["-filter_complex", fc, *maps,
                    "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, *rate_opts,
                    "-pix_fmt", PIX_FMT, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
                    "-movflags", "+faststart",
                    "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                    str(out_path)]

            self.log(f"[{job_idx}/{total}] Склейка {n} клипов → {res_key} ⇒ {out_path.name}", "info")
            self._js("onCurrentFile", job_idx, total, f"Склейка {n} файлов → {res_key}")

            try:
                self.current_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
            except FileNotFoundError:
                self.log("FFmpeg не найден в PATH", "error")
                self._js("onDone", ok, total, False); return

            rx_time = re.compile(r"^out_time_us=(-?\d+)$")
            last_pct = -1
            for line in self.current_proc.stdout:
                if self.stop_event.is_set():
                    self.log("Остановлено пользователем", "warn")
                    self._js("onDone", ok, total, True); return
                line = line.strip()
                m = rx_time.match(line)
                if m:
                    us = max(0, int(m.group(1)))
                    pct = int(min(100, us * 100 / total_us))
                    if pct != last_pct:
                        last_pct = pct
                        self._js("onFileProgress", job_idx, total, pct)
                elif line == "progress=end":
                    self._js("onFileProgress", job_idx, total, 100)

            err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
            rc = self.current_proc.wait()
            self.current_proc = None

            if rc != 0:
                self.log(f"Ошибка ({rc}): {err.strip()[:200]}", "error")
                try:
                    if out_path.exists(): out_path.unlink()
                except Exception: pass
            else:
                self.log(f"Готово: {out_path.name}", "success")
                ok += 1

            self._js("onOverallProgress", job_idx, total)

        self._js("onDone", ok, total, self.stop_event.is_set())


# =============================================================================
# Auto-subtitle helpers
# =============================================================================

def make_ass_content(segments, style):
    """Generate ASS subtitle file content from Whisper segments."""
    animation = style.get("animation", "none")
    font_size = int(style.get("font_size", 52))
    color_hex = style.get("color", "#ffffff").lstrip("#")
    position = style.get("position", "bottom")
    bg = style.get("bg", True)

    # Convert #RRGGBB to ASS &H00BBGGRR& format
    if len(color_hex) == 6:
        r = color_hex[0:2]
        g = color_hex[2:4]
        b = color_hex[4:6]
        ass_color = f"&H00{b}{g}{r}&"
    else:
        ass_color = "&H00FFFFFF&"

    if position == "bottom":
        alignment = 2
        margin_v = 50
    elif position == "center":
        alignment = 5
        margin_v = 0
    else:  # top
        alignment = 8
        margin_v = 30

    border_style = 3 if bg else 1

    def ts(secs):
        """Convert float seconds to ASS timestamp h:mm:ss.cc"""
        s = max(0.0, float(secs))
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        cs = int(round((sec - int(sec)) * 100))
        return f"{h}:{m:02d}:{int(sec):02d}.{cs:02d}"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{ass_color},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,{border_style},2,0,{alignment},40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []

    for seg in segments:
        start = seg.get("start", 0)
        end = seg.get("end", start + 1)
        text = (seg.get("text") or "").strip()
        words = seg.get("words") or []

        if animation == "none":
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{text}")

        elif animation == "fade":
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{{\\fad(180,180)}}{text}")

        elif animation == "pop":
            tag = "{\\t(0,200,\\fscx110\\fscy110)\\t(200,350,\\fscx100\\fscy100)\\fad(0,150)}"
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{tag}{text}")

        elif animation == "word":
            if words:
                for w in words:
                    ws = w.get("start", start)
                    we = w.get("end", ws + 0.3)
                    wt = (w.get("word") or "").strip()
                    if wt:
                        lines.append(f"Dialogue: 0,{ts(ws)},{ts(we)},Default,,0,0,0,,{{\\fad(80,80)}}{wt}")
            else:
                lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{{\\fad(180,180)}}{text}")

        elif animation == "karaoke":
            if words:
                kar_text = ""
                for w in words:
                    ws = w.get("start", start)
                    we = w.get("end", ws + 0.3)
                    wt = (w.get("word") or "").strip()
                    dur_cs = max(1, int(round((we - ws) * 100)))
                    kar_text += f"{{\\kf{dur_cs}}}{wt} "
                lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{kar_text.rstrip()}")
            else:
                lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{{\\fad(180,180)}}{text}")

        else:
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{text}")

    return header + "\n".join(lines) + "\n"


_whisper_model_cache = {}
_whisper_lock = threading.Lock()


class TranscribeWorker(threading.Thread):
    """Runs Whisper via the system Python (subprocess) so it works from
    inside a PyInstaller bundle where whisper is not bundled."""

    # Minimal script executed in the SYSTEM python subprocess.
    # IMPORTANT: we hijack sys.stdout → sys.stderr for the entire Whisper
    # call so that tqdm bars / language-detection prints don't pollute the
    # JSON we write at the very end.
    _SCRIPT = r"""
import sys, json

# ── redirect all whisper noise to stderr ──────────────────────────────
_real_stdout = sys.stdout
sys.stdout = sys.stderr

import whisper

file_path  = sys.argv[1]
lang_arg   = sys.argv[2]
model_size = sys.argv[3]
language   = None if lang_arg == "auto" else lang_arg

model  = whisper.load_model(model_size)
result = model.transcribe(file_path, language=language,
                          word_timestamps=True, verbose=False)

segs = []
for seg in result.get("segments", []):
    words = [{"start": float(w.get("start", 0)),
              "end":   float(w.get("end",   0)),
              "word":  str(w.get("word",  "")).strip()}
             for w in (seg.get("words") or [])]
    segs.append({"start": float(seg.get("start", 0)),
                 "end":   float(seg.get("end",   0)),
                 "text":  str(seg.get("text", "")).strip(),
                 "words": words})

# ── restore real stdout and write ONLY the JSON line ─────────────────
sys.stdout = _real_stdout
print(json.dumps(segs, ensure_ascii=False), flush=True)
"""

    def __init__(self, window, file_path, language="auto", model_size="base"):
        super().__init__(daemon=True)
        self.window = window
        self.file_path = file_path
        self.language = language
        self.model_size = model_size
        self.stop_event = threading.Event()
        self._proc = None

    # ── JS helper ──────────────────────────────────────────────────────
    def _js(self, fn, *args):
        """Call window.subsApi.<fn>(...args) safely from any thread."""
        try:
            payload = json.dumps(list(args), ensure_ascii=False)
            self.window.evaluate_js(
                f"(function(){{var f=window.subsApi&&window.subsApi.{fn};"
                f"if(f)f.apply(null,{payload});}})()"
            )
        except Exception:
            pass

    # ── find system Python that has whisper ────────────────────────────
    @staticmethod
    def _find_python():
        # IMPORTANT: never use sys.executable when running as a PyInstaller
        # bundle — it points to the app's own Python and would re-launch the
        # entire application as a subprocess (causing a second window).
        # Only use it in plain development mode (sys.frozen is not set).
        if not getattr(sys, "frozen", False):
            try:
                import whisper  # noqa: F401 — just a check
                return sys.executable
            except ImportError:
                pass

        # Walk common macOS Homebrew / system Python paths
        candidates = [
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3.12",
            "/opt/homebrew/bin/python3.11",
            "/opt/homebrew/bin/python3.10",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for py in candidates:
            if not os.path.isfile(py):
                continue
            try:
                r = subprocess.run(
                    [py, "-c", "import whisper; print('ok')"],
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and "ok" in r.stdout:
                    return py
            except Exception:
                pass
        return None

    # ── main run ───────────────────────────────────────────────────────
    def run(self):
        try:
            self._js("onProgress", "Поиск Whisper…", 3)

            python_exe = self._find_python()
            if python_exe is None:
                raise ImportError(
                    "openai-whisper не найден.\n"
                    "Откройте Терминал и выполните:\n"
                    "  pip install openai-whisper")

            self._js("onProgress",
                     f"Загрузка модели '{self.model_size}'…", 10)

            if self.stop_event.is_set():
                return

            cmd = [python_exe, "-c", self._SCRIPT,
                   self.file_path,
                   self.language or "auto",
                   self.model_size]

            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True)

            self._js("onProgress", "Транскрипция… 0с", 20)

            # Tick thread: updates progress every second so the user can see
            # work is happening (Whisper can take minutes on long files).
            import time as _time
            _t0 = _time.time()

            def _tick():
                while self._proc and self._proc.poll() is None:
                    if self.stop_event.is_set():
                        break
                    elapsed = int(_time.time() - _t0)
                    pct = min(90, 20 + elapsed)   # ~+1% / second, cap at 90
                    self._js("onProgress",
                             f"Транскрипция… {elapsed}с", pct)
                    _time.sleep(1)

            _ticker = threading.Thread(target=_tick, daemon=True)
            _ticker.start()

            stdout, stderr = self._proc.communicate()
            rc = self._proc.returncode
            self._proc = None
            _ticker.join(timeout=2)

            if self.stop_event.is_set():
                return

            if rc != 0:
                err_msg = stderr.strip()[-400:] if stderr.strip() else f"exit code {rc}"
                raise RuntimeError(f"Ошибка Whisper (code {rc}):\n{err_msg}")

            stdout = stdout.strip()
            if not stdout:
                # Empty stdout — show last lines of stderr for debugging
                hint = stderr.strip()[-400:] if stderr.strip() else "(нет вывода)"
                raise RuntimeError(
                    f"Whisper не вернул результат.\n"
                    f"Вывод:\n{hint}")

            try:
                segments = json.loads(stdout)
            except Exception as je:
                raise RuntimeError(
                    f"Ошибка разбора JSON: {je}\n"
                    f"Первые 200 символов вывода: {stdout[:200]}")

            self._js("onDone", segments)

        except Exception as e:
            self._js("onError", str(e))


class SubtitleBurnWorker(threading.Thread):
    """Burns ASS subtitles into a video file (standalone subtitle tab)."""
    def __init__(self, window, src_path, segments, style, out_dir=None):
        super().__init__(daemon=True)
        self.window = window
        self.src_path = Path(src_path)
        self.segments = segments
        self.style = style
        self.out_dir = Path(out_dir) if out_dir else None
        self.stop_event = threading.Event()
        self.current_proc = None

    def _js(self, fn, *args):
        try:
            payload = json.dumps(args, ensure_ascii=False)
            self.window.evaluate_js(f"window.api.{fn}.apply(null, {payload})")
        except Exception:
            pass

    def log(self, text, level="info"):
        self._js("onLog", text, level)

    def stop(self):
        self.stop_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
            except Exception: pass

    # ── shared helpers ────────────────────────────────────────────────
    @staticmethod
    def _find_font():
        for p in [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/System/Library/Fonts/Geneva.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]:
            if os.path.isfile(p):
                return p
        return None

    # ── Attempt 1: drawtext filter (needs libfreetype, no libass) ────
    @staticmethod
    def _make_drawtext_filter(segments, style):
        font_size = int(style.get("font_size", 52))
        color_hex = style.get("color", "#ffffff").lstrip("#")
        position  = style.get("position", "bottom")
        use_bg    = style.get("bg", True)
        y_expr    = {"top": "80", "center": "(h-text_h)/2"}.get(position, "h-text_h-80")
        bg_part   = ":box=1:boxcolor=black@0.5:boxborderw=10" if use_bg else ""
        font      = SubtitleBurnWorker._find_font()
        font_arg  = f":fontfile='{font}'" if font else ""
        parts = []
        for seg in segments:
            start = float(seg.get("start", 0))
            end   = float(seg.get("end", start + 1))
            text  = str(seg.get("text", "")).strip().replace("\n", " ")
            if not text:
                continue
            text = text.replace("\\", "\\\\").replace("'", "\\'")
            parts.append(
                f"drawtext=text='{text}'{font_arg}"
                f":fontsize={font_size}:fontcolor=0x{color_hex}"
                f":x=(w-text_w)/2:y={y_expr}{bg_part}"
                f":enable='between(t,{start:.3f},{end:.3f})'"
            )
        return ",".join(parts) if parts else "null"

    # ── Attempt 2: Pillow PNG overlay (no text filters needed at all) ─
    def _pillow_overlay_cmd(self, src, dst, total_us):
        """Render each subtitle as transparent PNG via Pillow, overlay in FFmpeg.
        Returns (cmd, tmp_dir) or raises on error."""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            raise RuntimeError("Pillow не установлен. Выполни: pip install Pillow")

        info = ffprobe_info(src)
        vid_w, vid_h = info["width"], info["height"]

        font_size = int(self.style.get("font_size", 52))
        color_hex = self.style.get("color", "#ffffff").lstrip("#")
        r = int(color_hex[0:2], 16) if len(color_hex) >= 6 else 255
        g = int(color_hex[2:4], 16) if len(color_hex) >= 6 else 255
        b = int(color_hex[4:6], 16) if len(color_hex) >= 6 else 255
        position = self.style.get("position", "bottom")
        use_bg   = self.style.get("bg", True)

        font = None
        fp = self._find_font()
        if fp:
            try: font = ImageFont.truetype(fp, font_size)
            except Exception: pass
        if font is None:
            try: font = ImageFont.load_default(size=font_size)
            except Exception: font = ImageFont.load_default()

        import tempfile, shutil as _shutil
        tmp_dir = tempfile.mkdtemp(prefix="vr_subs_")

        img_inputs = []
        filter_parts = []
        vi = 0  # virtual input index (0 = main video, 1..N = PNGs)

        for idx, seg in enumerate(self.segments):
            text  = str(seg.get("text", "")).strip().replace("\n", " ")
            start = float(seg.get("start", 0))
            end   = float(seg.get("end", start + 1))
            if not text:
                continue

            # Render transparent subtitle PNG
            img = Image.new("RGBA", (vid_w, vid_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
            except Exception:
                bbox = (0, 0, len(text) * (font_size // 2), font_size)
            tw = max(1, bbox[2] - bbox[0])
            th = max(1, bbox[3] - bbox[1])
            x = max(0, (vid_w - tw) // 2)
            if position == "top":
                y = 80
            elif position == "center":
                y = (vid_h - th) // 2
            else:
                y = vid_h - th - 80
            if use_bg:
                pad = 10
                draw.rectangle([x-pad, y-pad, x+tw+pad, y+th+pad],
                                fill=(0, 0, 0, 140))
            draw.text((x, y), text, font=font, fill=(r, g, b, 255))

            png = os.path.join(tmp_dir, f"sub_{idx:04d}.png")
            img.save(png)

            vi += 1
            img_inputs += ["-i", png]
            prev = "[0:v]" if vi == 1 else f"[ov{vi-1}]"
            out  = f"[ov{vi}]"
            filter_parts.append(
                f"{prev}[{vi}:v]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'{out}"
            )

        if not filter_parts:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError("Нет сегментов для вжигания")

        filter_complex = ";".join(filter_parts)
        last_out = f"[ov{vi}]"

        cmd = (["ffmpeg", "-y", "-i", str(src)]
               + img_inputs
               + ["-filter_complex", filter_complex,
                  "-map", last_out, "-map", "0:a?",
                  "-c:a", "copy",
                  "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
                  "-pix_fmt", PIX_FMT, "-movflags", "+faststart",
                  "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                  str(dst)])
        return cmd, tmp_dir

    # ── run FFmpeg and track progress ────────────────────────────────
    def _run_ffmpeg(self, cmd, total_us):
        """Run FFmpeg cmd, stream progress. Returns (rc, stderr_text)."""
        try:
            self.current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            return -1, "ffmpeg not found"

        rx = re.compile(r"^out_time_us=(-?\d+)$")
        last_pct = -1
        for line in self.current_proc.stdout:
            if self.stop_event.is_set():
                break
            line = line.strip()
            m = rx.match(line)
            if m:
                us = max(0, int(m.group(1)))
                pct = int(min(100, us * 100 / total_us))
                if pct != last_pct:
                    last_pct = pct
                    self._js("onFileProgress", 1, 1, pct)
                    self._js("onOverallProgress", pct, 100)
            elif line == "progress=end":
                self._js("onFileProgress", 1, 1, 100)

        err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
        rc  = self.current_proc.wait()
        self.current_proc = None
        return rc, err

    # ── main run ──────────────────────────────────────────────────────
    def run(self):
        src     = self.src_path
        out_dir = self.out_dir if self.out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        dst     = unique_path(out_dir / f"{src.stem}_subtitled.mp4")

        try:
            info     = ffprobe_info(src)
            total_us = max(1, int(info["duration"] * 1_000_000))
        except Exception:
            total_us = 1_000_000

        # Write ASS temp file (for attempt 0)
        ass_path = f"/tmp/vr_burn_{id(self) & 0xFFFF:04x}.ass"
        try:
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(make_ass_content(self.segments, self.style))
        except Exception as e:
            self.log(f"Ошибка ASS: {e}", "error")
            self._js("onDone", 0, 1, False)
            return

        self.log(f"Субтитры → {dst.name}", "info")
        self._js("onCurrentFile", 1, 1, f"Вжигаем субтитры в {src.name}")

        tmp_dir = None
        try:
            for attempt in range(3):
                if attempt == 0:
                    # Best quality: ASS + animations (needs libass)
                    cmd = ["ffmpeg", "-y", "-i", str(src),
                           "-vf", f"ass=filename={ass_path}",
                           "-c:a", "copy",
                           "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET,
                           "-crf", VIDEO_CRF, "-pix_fmt", PIX_FMT,
                           "-movflags", "+faststart",
                           "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                           str(dst)]

                elif attempt == 1:
                    # Fallback: drawtext (needs libfreetype, no animations)
                    self.log("libass не найден → пробую drawtext…", "warn")
                    cmd = ["ffmpeg", "-y", "-i", str(src),
                           "-vf", self._make_drawtext_filter(self.segments, self.style),
                           "-c:a", "copy",
                           "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET,
                           "-crf", VIDEO_CRF, "-pix_fmt", PIX_FMT,
                           "-movflags", "+faststart",
                           "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                           str(dst)]

                else:
                    # Last resort: Pillow renders PNGs, FFmpeg overlay (no text libs needed)
                    self.log("drawtext не найден → рендерю через Pillow…", "warn")
                    self.log("Для анимаций: brew reinstall ffmpeg", "warn")
                    try:
                        cmd, tmp_dir = self._pillow_overlay_cmd(src, dst, total_us)
                    except Exception as e:
                        self.log(f"Ошибка Pillow: {e}", "error")
                        self._js("onDone", 0, 1, False)
                        return

                rc, err = self._run_ffmpeg(cmd, total_us)

                if self.stop_event.is_set():
                    self.log("Остановлено", "warn")
                    self._js("onDone", 0, 1, False)
                    return

                if rc == 0:
                    break  # success!

                if rc == -1:
                    self.log("FFmpeg не найден в PATH", "error")
                    self._js("onDone", 0, 1, False)
                    return

                # Check if we should try the next fallback
                no_filter = "No such filter" in err
                if attempt < 2 and no_filter:
                    try:
                        if dst.exists(): dst.unlink()
                    except Exception: pass
                    continue  # try next attempt

                # Real / unrecoverable error
                self.log(f"Ошибка ({rc}): {err.strip()[:200]}", "error")
                try:
                    if dst.exists(): dst.unlink()
                except Exception: pass
                self._js("onDone", 0, 1, False)
                return

        finally:
            try: os.unlink(ass_path)
            except Exception: pass
            if tmp_dir:
                import shutil as _sh
                _sh.rmtree(tmp_dir, ignore_errors=True)

        self.log(f"Готово: {dst.name}", "success")
        self._js("onDone", 1, 1, False)


# =============================================================================
# TTS Worker (Edge TTS → macOS say fallback)
# =============================================================================
class TTSWorker(threading.Thread):
    EDGE_VOICES = {
        "ru": {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"},
        "en": {"female": "en-US-JennyNeural",    "male": "en-US-GuyNeural"},
    }
    SAY_VOICES = {
        "ru": {"female": "Milena",   "male": "Milena"},
        "en": {"female": "Samantha", "male": "Alex"},
    }

    def __init__(self, window, text, lang="ru", gender="female", speed=1.0):
        super().__init__(daemon=True)
        self.window = window
        self.text   = text
        self.lang   = lang
        self.gender = gender
        self.speed  = float(speed)
        self.stop_event = threading.Event()

    def _js(self, fn, *args):
        try:
            payload = json.dumps(list(args), ensure_ascii=False)
            self.window.evaluate_js(
                f"(function(){{var f=window.soundApi&&window.soundApi.{fn};"
                f"if(f)f.apply(null,{payload});}})()"
            )
        except Exception:
            pass

    @staticmethod
    def _has_edge_tts():
        try:
            import edge_tts  # noqa
            return True
        except ImportError:
            return False

    def _edge_tts(self, out_path):
        try:
            import edge_tts, asyncio
            voice    = self.EDGE_VOICES.get(self.lang, self.EDGE_VOICES["en"]).get(self.gender, "en-US-JennyNeural")
            delta    = self.speed - 1.0
            rate_str = f"{'+' if delta >= 0 else ''}{int(delta * 100)}%"
            async def _gen():
                com = edge_tts.Communicate(self.text, voice, rate=rate_str)
                await com.save(out_path)
            asyncio.run(_gen())
            return os.path.isfile(out_path) and os.path.getsize(out_path) > 100
        except Exception:
            return False

    def _say_tts(self, out_path):
        try:
            voice = self.SAY_VOICES.get(self.lang, self.SAY_VOICES["en"]).get(self.gender, "Samantha")
            rate  = int(175 * self.speed)
            aiff  = out_path.replace(".m4a", ".aiff")
            r = subprocess.run(
                ["say", "-v", voice, "-r", str(rate), "-o", aiff, "--", self.text],
                capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.isfile(aiff):
                return False
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-i", aiff, "-c:a", "aac", "-b:a", "128k", out_path],
                capture_output=True, timeout=60)
            try: os.unlink(aiff)
            except Exception: pass
            return r2.returncode == 0 and os.path.isfile(out_path)
        except Exception:
            return False

    def run(self):
        self._js("onTTSProgress", "Запуск…", 5)
        out_path = f"/tmp/vr_tts_{id(self) & 0xFFFF:04x}.m4a"
        ok = False

        if self._has_edge_tts():
            self._js("onTTSProgress", "Edge TTS (онлайн)…", 20)
            ok = self._edge_tts(out_path)

        if not ok:
            self._js("onTTSProgress", "macOS say (офлайн)…", 50)
            ok = self._say_tts(out_path)

        if not ok:
            self._js("onTTSError", "Не удалось создать озвучку. Проверьте интернет или голоса macOS.")
            return

        self._js("onTTSProgress", "Готово!", 100)
        try:
            dur = ffprobe_info(out_path)["duration"]
        except Exception:
            dur = 0.0
        self._js("onTTSDone", out_path, dur)


# =============================================================================
# Sound Mix Worker
# =============================================================================
class SoundMixWorker(threading.Thread):
    """Mix voiceover tracks into video using FFmpeg adelay + amix."""

    def __init__(self, window, src_path, tracks, original_audio="mix", out_dir=None):
        super().__init__(daemon=True)
        self.window         = window
        self.src_path       = Path(src_path)
        self.tracks         = tracks           # [{start, file, duration, name}]
        self.original_audio = original_audio   # "mix" | "replace"
        self.out_dir        = Path(out_dir) if out_dir else None
        self.stop_event     = threading.Event()
        self.current_proc   = None

    def _js(self, fn, *args):
        try:
            payload = json.dumps(list(args), ensure_ascii=False)
            self.window.evaluate_js(
                f"(function(){{var f=window.soundApi&&window.soundApi.{fn};"
                f"if(f)f.apply(null,{payload});}})()"
            )
        except Exception:
            pass

    def log(self, text, level="info"):
        self._js("onMixLog", text, level)

    def stop(self):
        self.stop_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
            except Exception: pass

    def run(self):
        src     = self.src_path
        out_dir = self.out_dir if self.out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        dst     = unique_path(out_dir / f"{src.stem}_voiced.mp4")

        try:
            info     = ffprobe_info(src)
            total_us = max(1, int(info["duration"] * 1_000_000))
            has_orig = info["has_audio"]
        except Exception as e:
            self.log(f"Ошибка: {e}", "error")
            self._js("onMixDone", False, "")
            return

        self.log(f"Миксуем {len(self.tracks)} трек(ов) → {dst.name}", "info")
        self._js("onMixFile", f"Обрабатываем {src.name}")

        cmd_inputs   = ["-i", str(src)]
        filter_parts = []
        vo_labels    = []

        for i, tr in enumerate(self.tracks):
            delay_ms = int(float(tr["start"]) * 1000)
            cmd_inputs += ["-i", tr["file"]]
            lbl = f"[vo{i}]"
            filter_parts.append(f"[{i+1}:a]adelay={delay_ms}|{delay_ms}{lbl}")
            vo_labels.append(lbl)

        use_orig = self.original_audio == "mix" and has_orig
        all_labels = (["[0:a]"] + vo_labels) if use_orig else vo_labels
        n = len(all_labels)
        dur_mode = "first" if use_orig else "longest"
        filter_parts.append(
            f"{''.join(all_labels)}amix=inputs={n}:duration={dur_mode}:normalize=0:dropout_transition=0[aout]"
        )

        cmd = (["ffmpeg", "-y"]
               + cmd_inputs
               + ["-filter_complex", ";".join(filter_parts),
                  "-map", "0:v", "-map", "[aout]",
                  "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                  "-movflags", "+faststart",
                  "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                  str(dst)])

        try:
            self.current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            self.log("FFmpeg не найден", "error")
            self._js("onMixDone", False, "")
            return

        rx = re.compile(r"^out_time_us=(-?\d+)$")
        last_pct = -1
        for line in self.current_proc.stdout:
            if self.stop_event.is_set():
                self.log("Остановлено", "warn")
                break
            line = line.strip()
            m = rx.match(line)
            if m:
                us  = max(0, int(m.group(1)))
                pct = int(min(100, us * 100 / total_us))
                if pct != last_pct:
                    last_pct = pct
                    self._js("onMixProgress", pct)
            elif line == "progress=end":
                self._js("onMixProgress", 100)

        err = self.current_proc.stderr.read() if self.current_proc.stderr else ""
        rc  = self.current_proc.wait()
        self.current_proc = None

        if rc != 0:
            self.log(f"Ошибка ({rc}): {err.strip()[:200]}", "error")
            try:
                if dst.exists(): dst.unlink()
            except Exception: pass
            self._js("onMixDone", False, "")
            return

        self.log(f"Готово: {dst.name}", "success")
        self._js("onMixDone", True, str(dst))


# =============================================================================
# API: мост Python <-> JS
# =============================================================================
class API:
    def __init__(self):
        self.window             = None
        self.worker             = None
        self.transcribe_worker  = None
        self.subtitle_worker    = None
        self.tts_worker         = None
        self.sound_mix_worker   = None
        self.clipopus_worker    = None

    # ----- общая инфо -----
    def get_state(self):
        return {
            "ffmpeg_ok": have_ffmpeg(),
            "default_packshots": _find_default_packshots(),
            "size_limit_mb": SIZE_LIMIT_MB,
            "fade_duration": FADE_DURATION,
            "resolutions": list(RESOLUTIONS.keys()),
            "default_res": DEFAULT_RES,
            "known_teams": KNOWN_TEAMS,
            "known_types": KNOWN_TYPES,
            "video_server_port": _ensure_video_server(),
        }

    def get_video_url(self, path):
        """Возвращает локальный HTTP URL для HTML5 <video> предпросмотра."""
        port = _ensure_video_server()
        encoded = _urlparse.quote(str(os.path.expanduser(path)))
        return f"http://127.0.0.1:{port}/?p={encoded}"

    # ----- file pickers -----
    def pick_files(self):
        types = ("Видео (*.mp4;*.mov;*.m4v;*.mkv)", "Все файлы (*.*)")
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        return self._files_info(result or [])

    def pick_folder_with_videos(self):
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return []
        folder = Path(result[0])
        files = [p for p in sorted(folder.iterdir())
                 if p.is_file() and p.suffix.lower() in ACCEPT_EXT]
        return self._files_info([str(p) for p in files])

    def pick_one_video(self):
        types = ("Видео (*.mp4;*.mov;*.m4v;*.mkv)", "Все файлы (*.*)")
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        return result[0] if result else None

    def pick_folder(self):
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def _files_info(self, paths):
        infos = []
        for p in paths:
            path = Path(p)
            if not path.exists() or path.suffix.lower() not in ACCEPT_EXT:
                continue
            infos.append({
                "path": str(path),
                "name": path.name,
                "stem": path.stem,
                "parent": str(path.parent),
            })
        return infos

    def file_exists(self, path):
        return bool(path and Path(os.path.expanduser(path)).exists())

    def get_file_info(self, path):
        """Возвращает длительность и размеры видео (для ползунков тайминга)."""
        try:
            info = ffprobe_info(os.path.expanduser(path))
            return {
                "duration": info["duration"],
                "width": info["width"],
                "height": info["height"],
            }
        except Exception as e:
            return {"duration": 0.0, "error": str(e)}

    # ----- preview-сервер: считаем имя на стороне Python -----
    def preview_name(self, params):
        try:
            nc = NamingConfig(
                mode=params.get("mode", "os"),
                team=params.get("team", "In-House"),
                type_=params.get("type", "Unreal"),
                name_source=params.get("name_source", "auto"),
                name_manual=params.get("name_manual", ""),
                template=params.get("template", DEFAULT_TEMPLATE_RESIZE),
                tag=params.get("tag", ""),
            )
            src_stem = params.get("src_stem", "ID0001_OS_In-House_Unreal_Roulette-Update_12-24_EN_22s_1080x1080")
            all_stems = params.get("all_stems") or None
            res_key = params.get("res_key", DEFAULT_RES)
            dur = float(params.get("duration_sec", 22))
            return nc.make_name(src_stem=src_stem, res_key=res_key,
                                output_duration_sec=dur, all_src_stems=all_stems)
        except Exception as e:
            return f"ERR: {e}"

    # ----- subtitle / whisper -----
    def check_whisper(self):
        try:
            import whisper  # noqa: F401
            return {"ok": True, "install": ""}
        except ImportError:
            return {"ok": False, "install": "pip install openai-whisper"}

    def start_transcribe(self, params):
        if self.transcribe_worker and self.transcribe_worker.is_alive():
            return {"ok": False, "error": "Уже идёт транскрипция"}
        file_path = params.get("file_path", "")
        if not file_path:
            return {"ok": False, "error": "Нет файла для транскрипции"}
        language = params.get("language", "auto")
        model_size = params.get("model", "base")
        self.transcribe_worker = TranscribeWorker(
            self.window, file_path, language=language, model_size=model_size)
        self.transcribe_worker.start()
        return {"ok": True}

    def cancel_transcribe(self):
        w = self.transcribe_worker
        if w and w.is_alive():
            w.stop_event.set()
            p = getattr(w, "_proc", None)
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        return {"ok": True}

    def start_subtitle_burn(self, params):
        segments = params.get("segments") or []
        if not segments:
            return {"ok": False, "error": "Нет субтитров для вжигания"}
        src = params.get("src_path", "")
        if not src or not os.path.exists(os.path.expanduser(src)):
            return {"ok": False, "error": "Файл не найден"}
        style = {
            "animation": params.get("animation", "fade"),
            "font_size": params.get("font_size", 52),
            "color": params.get("color", "#ffffff"),
            "position": params.get("position", "bottom"),
            "bg": params.get("bg", True),
        }
        out_dir = params.get("outdir", "")
        if params.get("outdir_same", True):
            out_dir = None
        elif out_dir:
            out_dir = os.path.expanduser(out_dir)

        if self.subtitle_worker and self.subtitle_worker.is_alive():
            return {"ok": False, "error": "Уже выполняется"}

        self.subtitle_worker = SubtitleBurnWorker(
            self.window, os.path.expanduser(src), segments, style, out_dir)
        self.subtitle_worker.start()
        return {"ok": True}

    # ----- processing -----
    def start_resize(self, params):
        if self.worker and self.worker.is_alive():
            return {"ok": False, "error": "Идёт обработка, дождитесь окончания"}
        if not have_ffmpeg():
            return {"ok": False, "error": "FFmpeg не найден.\nbrew install ffmpeg"}

        # jobs: [{file: str, resolutions: [str, ...]}]
        jobs_raw = params.get("jobs", [])
        if not jobs_raw:
            return {"ok": False, "error": "Нет файлов для обработки"}
        jobs = [
            (Path(j["file"]), j["resolutions"])
            for j in jobs_raw
            if j.get("file") and j.get("resolutions")
        ]
        if not jobs:
            return {"ok": False, "error": "Нет файлов с выбранными разрешениями"}

        # Все уникальные разрешения для проверки пекшотов
        all_resolutions = list({r for _, ress in jobs for r in ress})

        packshots = None
        if params.get("packshot_on"):
            packshots = {}
            for res in all_resolutions:
                p = (params.get("packshots") or {}).get(res, "").strip()
                if p:
                    pp = Path(os.path.expanduser(p))
                    packshots[res] = pp if pp.exists() else None
                else:
                    packshots[res] = None
            if not any(v for v in packshots.values()):
                return {"ok": False,
                        "error": "Включён пекшот, но ни один файл не указан"}

        out_dir = None
        if not params.get("outdir_same"):
            d = (params.get("outdir") or "").strip()
            if d:
                out_dir = Path(os.path.expanduser(d))
        nc = NamingConfig(
            mode=params.get("naming_mode", "os"),
            team=params.get("team", "In-House"),
            type_=params.get("type", "Unreal"),
            name_source=params.get("name_source", "auto"),
            name_manual=params.get("name_manual", ""),
            template=params.get("template", DEFAULT_TEMPLATE_RESIZE),
            tag=params.get("tag", ""))
        max_size_mb = SIZE_LIMIT_MB if params.get("size_limit_on") else None

        trim_start = float(params.get("trim_start") or 0.0)
        trim_end_raw = params.get("trim_end")
        trim_end = float(trim_end_raw) if trim_end_raw is not None else None

        subtitle_segments = params.get("subtitle_segments") or []
        subtitle_style = params.get("subtitle_style") or {}

        self.worker = ResizeWorker(
            self.window, jobs, packshots, out_dir,
            max_size_mb=max_size_mb, naming_config=nc,
            trim_start=trim_start, trim_end=trim_end,
            subtitle_segments=subtitle_segments, subtitle_style=subtitle_style)
        self.worker.start()
        return {"ok": True}

    def start_concat(self, params):
        if self.worker and self.worker.is_alive():
            return {"ok": False, "error": "Идёт обработка"}
        if not have_ffmpeg():
            return {"ok": False, "error": "FFmpeg не найден.\nbrew install ffmpeg"}

        raw_jobs = params.get("jobs") or []
        if not raw_jobs:
            return {"ok": False, "error": "Нет задач для обработки"}

        jobs = []
        for j in raw_jobs:
            files = [Path(p) for p in j.get("files", []) if p]
            res = j.get("resolution", "1920x1080")
            if res not in RESOLUTIONS or len(files) < 2:
                continue
            jobs.append({"files": files, "trims": j.get("trims") or [], "resolution": res})

        if not jobs:
            return {"ok": False, "error": "Нет групп с минимум 2 файлами"}

        first_file = jobs[0]["files"][0]
        if params.get("outdir_same"):
            out_dir = first_file.parent
        else:
            d = (params.get("outdir") or "").strip()
            out_dir = Path(os.path.expanduser(d)) if d else first_file.parent

        nc = NamingConfig(
            mode=params.get("naming_mode", "os"),
            team=params.get("team", "In-House"),
            type_=params.get("type", "Combo"),
            name_source=params.get("name_source", "auto"),
            name_manual=params.get("name_manual", ""),
            template=params.get("template", DEFAULT_TEMPLATE_CONCAT),
            tag=params.get("tag", ""))
        max_size_mb = SIZE_LIMIT_MB if params.get("size_limit_on") else None

        self.worker = ChainedConcatWorker(
            self.window, jobs, out_dir,
            fade=bool(params.get("fade")),
            max_size_mb=max_size_mb, naming_config=nc)
        self.worker.start()
        return {"ok": True}

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            return {"ok": True}
        return {"ok": False}

    # ----- звук / TTS -----
    def pick_sound_video(self):
        types = ("Видео (*.mp4;*.mov;*.m4v;*.mkv)", "Все файлы (*.*)")
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        if not result:
            return None
        p = str(result[0]) if isinstance(result, (list, tuple)) else str(result)
        try:
            info = ffprobe_info(p)
            return {"path": p, "name": Path(p).name, "duration": info["duration"]}
        except Exception:
            return {"path": p, "name": Path(p).name, "duration": 0}

    def pick_audio_files(self):
        types = ("Аудио (*.mp3;*.m4a;*.wav;*.aac;*.ogg;*.flac)", "Все файлы (*.*)")
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        if not result:
            return []
        out = []
        for p in result:
            p = str(p)
            try:
                info = ffprobe_info(p)
                out.append({"path": p, "name": Path(p).name, "duration": info["duration"]})
            except Exception:
                out.append({"path": p, "name": Path(p).name, "duration": 0})
        return out

    def generate_tts(self, params):
        text = (params.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Текст пустой"}
        if self.tts_worker and self.tts_worker.is_alive():
            return {"ok": False, "error": "Генерация уже идёт"}
        self.tts_worker = TTSWorker(
            self.window,
            text   = text,
            lang   = params.get("lang", "ru"),
            gender = params.get("gender", "female"),
            speed  = float(params.get("speed", 1.0)),
        )
        self.tts_worker.start()
        return {"ok": True}

    def cancel_tts(self):
        w = self.tts_worker
        if w and w.is_alive():
            w.stop_event.set()
        return {"ok": True}

    def start_sound_mix(self, params):
        if not have_ffmpeg():
            return {"ok": False, "error": "FFmpeg не найден"}
        src    = params.get("src_path", "")
        tracks = params.get("tracks") or []
        if not src or not tracks:
            return {"ok": False, "error": "Укажите видео и войсоверы"}
        if self.sound_mix_worker and self.sound_mix_worker.is_alive():
            return {"ok": False, "error": "Уже выполняется"}
        out_dir = None if params.get("outdir_same") else params.get("outdir")
        self.sound_mix_worker = SoundMixWorker(
            self.window,
            src_path       = os.path.expanduser(src),
            tracks         = tracks,
            original_audio = params.get("original_audio", "mix"),
            out_dir        = out_dir,
        )
        self.sound_mix_worker.start()
        return {"ok": True}

    def cancel_sound_mix(self):
        w = self.sound_mix_worker
        if w and w.is_alive():
            w.stop()
        return {"ok": True}

    def play_audio(self, path):
        """Play an audio file via macOS afplay (bypasses WKWebView sandbox)."""
        try:
            subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_video_thumbnails(self, path, count=10):
        """Extract `count` evenly-spaced JPEG frames as base64 data-URLs."""
        import base64, tempfile as _tf
        try:
            info = ffprobe_info(path)
            dur  = info.get("duration", 0)
            if dur <= 0:
                return {"ok": True, "thumbs": [], "duration": 0}
            fps_target = count / dur
            with _tf.TemporaryDirectory() as tmpdir:
                pattern = os.path.join(tmpdir, "th%03d.jpg")
                cmd = ["ffmpeg", "-i", path,
                       "-vf", f"fps={fps_target:.6f},scale=80:-2",
                       "-vframes", str(count), "-q:v", "6",
                       "-f", "image2", pattern]
                subprocess.run(cmd, capture_output=True, timeout=30)
                thumbs = []
                for i in range(1, count + 1):
                    fp = os.path.join(tmpdir, f"th{i:03d}.jpg")
                    if os.path.isfile(fp):
                        with open(fp, "rb") as fh:
                            b64 = base64.b64encode(fh.read()).decode()
                        thumbs.append(f"data:image/jpeg;base64,{b64}")
                    else:
                        thumbs.append("")
            return {"ok": True, "thumbs": thumbs, "duration": dur}
        except Exception as e:
            return {"ok": False, "error": str(e), "thumbs": [], "duration": 0}

    # ----- ClipOpus -----

    def load_clipopus_key(self):
        p = Path.home() / "VideoResizer" / "clipopus_key.txt"
        if p.exists():
            return {"key": p.read_text().strip()}
        return {"key": ""}

    def save_clipopus_key(self, api_key):
        d = Path.home() / "VideoResizer"
        d.mkdir(parents=True, exist_ok=True)
        (d / "clipopus_key.txt").write_text((api_key or "").strip())
        return {"ok": True}

    def fetch_clipopus_projects(self, api_key):
        import urllib.request as _req, json as _j
        try:
            req = _req.Request(
                "https://api.opus.pro/api/projects",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            with _req.urlopen(req, timeout=15) as r:
                raw = _j.loads(r.read())
            projects = raw if isinstance(raw, list) else raw.get("data", raw.get("projects", []))
            return {"ok": True, "projects": projects}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def fetch_clipopus_clips(self, api_key, project_id):
        import urllib.request as _req, json as _j, urllib.parse as _up
        try:
            url = (f"https://api.opus.pro/api/clips"
                   f"?projectId={_up.quote(str(project_id))}")
            req = _req.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            with _req.urlopen(req, timeout=15) as r:
                raw = _j.loads(r.read())
            clips = raw if isinstance(raw, list) else raw.get("data", raw.get("clips", []))
            return {"ok": True, "clips": clips}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start_clipopus_resize(self, params):
        if self.clipopus_worker and self.clipopus_worker.is_alive():
            return {"ok": False, "error": "Уже идёт обработка ClipOpus"}
        if not have_ffmpeg():
            return {"ok": False, "error": "FFmpeg не найден"}
        clips_data = params.get("clips", [])
        if not clips_data:
            return {"ok": False, "error": "Нет клипов для обработки"}
        out_dir_raw = (params.get("outdir") or "").strip()
        out_dir = Path(os.path.expanduser(out_dir_raw)) if out_dir_raw else None
        nc = NamingConfig(mode=NamingConfig.MODE_CUSTOM, template=DEFAULT_TEMPLATE_RESIZE)
        self.clipopus_worker = ClipOpusWorker(self.window, clips_data, out_dir, nc)
        self.clipopus_worker.start()
        return {"ok": True}

    def stop_clipopus(self):
        w = self.clipopus_worker
        if w and w.is_alive():
            w.stop()
            return {"ok": True}
        return {"ok": False}


# =============================================================================
# ClipOpus Worker: скачивает клипы из OpusClip и запускает ресайз
# =============================================================================
class ClipOpusWorker(threading.Thread):
    """clips_data: [{"url": str, "title": str, "resolutions": ["1080x1920", ...]}]"""

    def __init__(self, window, clips_data, out_dir, naming_config=None):
        super().__init__(daemon=True)
        self.window         = window
        self.clips_data     = clips_data
        self.out_dir        = out_dir
        self.naming_config  = naming_config or NamingConfig(
            mode=NamingConfig.MODE_CUSTOM, template=DEFAULT_TEMPLATE_RESIZE)
        self.stop_event     = threading.Event()
        self._resize_worker = None
        self._dl_dir        = Path.home() / "VideoResizer" / "ClipOpus"

    def _js(self, fn, *args):
        try:
            payload = json.dumps(args, ensure_ascii=False)
            self.window.evaluate_js(f"window.clipOpusApi.{fn}.apply(null, {payload})")
        except Exception:
            pass

    def _log(self, text, level="info"):
        try:
            payload = json.dumps([text, level], ensure_ascii=False)
            self.window.evaluate_js(f"window.api.onLog.apply(null, {payload})")
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()
        w = self._resize_worker
        if w:
            w.stop_event.set()
            p = w.current_proc
            if p and p.poll() is None:
                try:
                    p.terminate()
                    try: p.wait(timeout=2)
                    except subprocess.TimeoutExpired: p.kill()
                except Exception:
                    pass

    def run(self):
        self._dl_dir.mkdir(parents=True, exist_ok=True)
        total = len(self.clips_data)
        downloaded = []

        for i, clip in enumerate(self.clips_data):
            if self.stop_event.is_set():
                break
            url        = clip.get("url", "").strip()
            title      = clip.get("title") or f"clip_{i + 1}"
            resolutions = [r for r in clip.get("resolutions", []) if r in RESOLUTIONS]
            if not url or not resolutions:
                self._log(f"Слот {i + 1}: нет URL или разрешений, пропускаем", "warn")
                continue

            safe = re.sub(r"[^\w\-_. ]", "_", title)[:50]
            dst  = self._dl_dir / f"clip_{i + 1}_{safe}.mp4"

            self._log(f"[{i + 1}/{total}] Скачиваем: {title}", "info")
            self._js("onDownload", i, total, title, 0)

            try:
                self._download(url, dst,
                               lambda pct, _i=i, _t=total, _n=title:
                               self._js("onDownload", _i, _t, _n, pct))
                downloaded.append((dst, resolutions))
                self._log(f"✓ Скачан: {dst.name}", "info")
            except InterruptedError:
                break
            except Exception as e:
                self._log(f"Ошибка скачивания «{title}»: {e}", "error")
                self._js("onError", str(e))

        if self.stop_event.is_set() or not downloaded:
            self._js("onStopped")
            return

        self._log("Начинаем ресайз…", "info")
        self._js("onResizeStart", len(downloaded))

        w = ResizeWorker(
            self.window, downloaded,
            packshots=None,
            out_dir=self.out_dir,
            naming_config=self.naming_config,
        )
        w.stop_event       = self.stop_event
        self._resize_worker = w
        w.run()

        if not self.stop_event.is_set():
            self._js("onAllDone")
        else:
            self._js("onStopped")

    def _download(self, url, dst: Path, progress_cb=None):
        import urllib.request as _req
        dst.parent.mkdir(parents=True, exist_ok=True)
        req = _req.Request(url, headers={"User-Agent": "VideoResizer/4"})
        with _req.urlopen(req, timeout=180) as resp:
            total      = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            with open(dst, "wb") as fh:
                while True:
                    if self.stop_event.is_set():
                        raise InterruptedError("Остановлено")
                    chunk = resp.read(1 << 17)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(int(downloaded / total * 100))


# =============================================================================
# HTML / CSS / JS — нативное WebKit-окно
# =============================================================================
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Video Resizer</title>
<style>
:root {
  --bg: #0a0d14;
  --bg-elev: #11151f;
  --card: #171b27;
  --card-hover: #1f2433;
  --card-active: #1c2235;
  --border: #252a3a;
  --border-subtle: #1c2030;
  --text: #e8eaf0;
  --text-muted: #9aa0b0;
  --text-dim: #5a6075;
  --accent: #4f8cf7;
  --accent-hover: #6aa3ff;
  --accent-dim: #22386b;
  --violet: #a78bfa;
  --violet-hover: #bda4ff;
  --violet-dim: #3d2f6e;
  --success: #44d39a;
  --success-dim: #1f4d3a;
  --warn: #ffb02e;
  --warn-dim: #553a0d;
  --danger: #ff6b6b;
  --danger-dim: #5a2828;
  --log-bg: #06080d;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
               "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  user-select: none;
  -webkit-user-select: none;
  overflow: hidden;
}

/* === Главный layout: sidebar + main === */
.app {
  display: grid;
  grid-template-columns: 200px 1fr;
  height: 100vh;
}

/* === Sidebar === */
.sidebar {
  background: var(--bg-elev);
  display: flex;
  flex-direction: column;
  padding: 18px 14px;
  border-right: 1px solid var(--border-subtle);
}
.brand {
  display: flex; gap: 12px; align-items: center;
  padding: 4px 4px 16px;
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: 10px;
}
.brand-logo {
  width: 44px; height: 44px; border-radius: 12px;
  background: linear-gradient(135deg, var(--accent) 0%, #2c6cd6 100%);
  color: white; display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 16px; letter-spacing: 0.5px;
  box-shadow: 0 4px 14px rgba(79, 140, 247, 0.35);
}
.brand-text { display: flex; flex-direction: column; }
.brand-title { font-weight: 700; font-size: 15px; }
.brand-version { color: var(--text-dim); font-size: 11px; }

.nav { display: flex; flex-direction: column; gap: 4px; }
.nav-item {
  padding: 10px 12px; border-radius: 10px;
  cursor: pointer; transition: all 0.15s;
  border: none; background: transparent;
  text-align: left; font-family: inherit; width: 100%;
}
.nav-item:hover { background: var(--card-hover); }
.nav-item.active { background: var(--card-active); }
.nav-item-title {
  font-weight: 600; font-size: 13px; color: var(--text);
  margin-bottom: 2px;
}
.nav-item.active[data-tab="resize"] .nav-item-title { color: var(--accent); }
.nav-item.active[data-tab="concat"] .nav-item-title { color: var(--violet); }
.nav-item.active[data-tab="subs"]   .nav-item-title { color: var(--success); }
.nav-item.active[data-tab="sound"]    .nav-item-title { color: #f97316; }
.nav-item.active[data-tab="clipopus"] .nav-item-title { color: #a855f7; }
/* ClipOpus tab */
.co-key-row { display:flex; gap:8px; margin-bottom:14px; }
.co-key-row input { flex:1; }
.co-fetch-row { display:flex; gap:8px; margin-bottom:18px; align-items:flex-end; }
.co-fetch-row input { flex:1; }
.co-clips-grid { display:flex; flex-direction:column; gap:12px; margin-bottom:16px; }
.co-slot { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.co-slot-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.co-slot-num { width:24px; height:24px; border-radius:50%; background:#a855f7; color:#fff;
               font-size:.75rem; font-weight:700; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.co-slot-title { font-size:.85rem; font-weight:600; color:var(--text); flex:1; }
.co-slot.loaded { border-color:#a855f7; background:rgba(168,85,247,.05); }
.co-url-input { width:100%; margin-bottom:8px; }
.co-res-row { display:flex; flex-wrap:wrap; gap:6px; }
.co-res-chip { padding:4px 10px; border-radius:6px; border:1.5px solid var(--border);
               background:var(--card); color:var(--text-muted); font-size:.75rem; font-weight:600;
               cursor:pointer; transition:all .15s; }
.co-res-chip.on { background:rgba(168,85,247,.18); border-color:#a855f7; color:#c084fc; }
.co-status-bar { background:rgba(168,85,247,.08); border:1px solid rgba(168,85,247,.2);
                 border-radius:8px; padding:10px 14px; font-size:.83rem; color:#c084fc;
                 margin-bottom:12px; display:none; }
.co-status-bar.visible { display:block; }
.co-btn-fetch { background:linear-gradient(135deg,#a855f7,#7c3aed); color:#fff;
                border:none; border-radius:8px; padding:9px 18px; font-size:.85rem;
                font-weight:700; cursor:pointer; white-space:nowrap; }
.co-btn-fetch:hover { opacity:.88; }
.co-btn-fetch:disabled { opacity:.45; cursor:not-allowed; }
.co-select { width:100%; margin-bottom:8px; background:var(--input-bg);
             border:1.5px solid var(--border); border-radius:8px; padding:7px 10px;
             color:var(--text); font-size:.85rem; }
.co-outdir-row { display:flex; gap:8px; align-items:center; margin-bottom:4px; }
.co-outdir-row input { flex:1; }
.section-title.subs  { color: var(--success); }
.section-title.sound { color: #f97316; }
.nav-item-sub { font-size: 10px; color: var(--text-dim); }
/* Sound tab – timeline */
.snd-timeline {
  position: relative; border: 1px solid var(--border); border-radius: 6px;
  overflow: hidden; cursor: crosshair; user-select: none; margin-bottom: 6px;
}
.snd-ruler {
  position: relative; height: 22px;
  background: var(--bg); border-bottom: 1px solid var(--border-subtle);
}
.snd-marker {
  position: absolute; top: 0; transform: translateX(-50%);
}
.snd-marker-tick { width: 1px; height: 6px; background: var(--border); margin: 2px auto 0; }
.snd-marker-label { font-size: 9px; color: var(--text-dim); text-align: center; white-space: nowrap; }
.snd-video-bar {
  height: 26px; background: var(--accent-dim);
  border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center;
}
.snd-tracks-bar { height: 36px; position: relative; background: var(--card); }
.snd-track-block {
  position: absolute; top: 4px; height: calc(100% - 8px);
  border: 1px solid; border-radius: 3px;
  overflow: hidden; display: flex; align-items: center; min-width: 3px;
}
.snd-block-label { font-size: 9px; padding: 0 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.snd-cursor {
  position: absolute; top: 0; bottom: 0; width: 2px;
  background: #ffb02e; pointer-events: none;
}
/* Sound tab – track list */
.track-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 0; border-bottom: 1px solid var(--border-subtle);
}
.track-row:last-child { border-bottom: none; }
.track-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.track-name { flex: 1; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.track-dur  { font-size: 11px; color: var(--text-dim); flex-shrink: 0; min-width: 38px; text-align: right; }
/* Sound tab – voice gender / audio mode buttons */
.voice-gender-row { display: flex; gap: 6px; }
.voice-gender-btn {
  flex: 1; padding: 9px 6px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg-elev);
  color: var(--text-muted); cursor: pointer; text-align: center;
  font-size: 12px; transition: all 0.15s;
}
.voice-gender-btn.active { border-color: var(--accent); background: var(--accent-dim); color: var(--accent); }
.audio-mode-row { display: flex; gap: 6px; }
.audio-mode-btn {
  flex: 1; padding: 7px 6px; border: 1px solid var(--border);
  border-radius: 5px; background: var(--bg-elev);
  color: var(--text-muted); cursor: pointer; font-size: 11px; text-align: center;
}
.audio-mode-btn.active { border-color: var(--violet); background: var(--violet-dim); color: var(--violet); }
/* TTS progress */
.tts-progress { height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin-top: 8px; }
.tts-progress-fill { height: 100%; background: var(--accent); border-radius: 2px; transition: width 0.4s; }

.status-bar {
  margin-top: auto;
  padding-top: 12px; border-top: 1px solid var(--border-subtle);
  display: flex; gap: 8px; align-items: center;
  font-size: 11px; color: var(--text-muted);
}
.status-dot { font-size: 14px; color: var(--text-dim); }
.status-dot.ok { color: var(--success); }
.status-dot.bad { color: var(--danger); }

/* === Main === */
.main {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.section-head {
  padding: 18px 20px 8px;
}
.section-title { font-size: 22px; font-weight: 700; color: var(--accent); }
.section-title.concat { color: var(--violet); }
.section-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

.content {
  flex: 1;
  overflow-y: auto;
  padding: 4px 20px 12px;
}
.content::-webkit-scrollbar { width: 8px; }
.content::-webkit-scrollbar-track { background: transparent; }
.content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.content::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

/* === Cards === */
.card {
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  margin-bottom: 10px;
  overflow: hidden;
}
.card-head {
  padding: 12px 16px 4px;
  display: flex; justify-content: space-between; align-items: baseline;
}
.card-title {
  font-size: 10px; font-weight: 700; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.card-sub { font-size: 10px; color: var(--text-dim); }
.card-body { padding: 4px 16px 14px; }

/* === Drop zone === */
.drop-zone {
  border: 2px dashed var(--border);
  border-radius: 14px; padding: 22px;
  text-align: center; cursor: pointer;
  background: var(--card);
  transition: all 0.2s;
  margin-bottom: 10px;
}
.drop-zone:hover { border-color: var(--accent); background: var(--card-hover); }
.drop-zone.concat:hover { border-color: var(--violet); }
.drop-zone-title { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
.drop-zone-sub { font-size: 11px; color: var(--text-dim); }
.drop-zone-buttons {
  display: flex; gap: 8px; justify-content: center; margin-top: 12px;
}

/* === Buttons === */
.btn {
  border: 1px solid var(--border);
  background: var(--card-hover);
  color: var(--text);
  padding: 7px 14px; border-radius: 8px;
  font-size: 12px; font-weight: 600; cursor: pointer;
  font-family: inherit;
  transition: all 0.15s;
}
.btn:hover { background: var(--border); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}
.btn-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.btn-primary.concat { background: var(--violet); border-color: var(--violet); }
.btn-primary.concat:hover { background: var(--violet-hover); border-color: var(--violet-hover); }
.btn-ghost {
  background: transparent;
  color: var(--text-muted);
}
.btn-ghost:hover { background: var(--card-hover); color: var(--text); }
.btn-danger { color: var(--danger); }
.btn-icon {
  width: 26px; height: 26px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
}

/* === File list === */
.file-list {
  background: var(--bg-elev);
  border-radius: 10px;
  max-height: 180px; overflow-y: auto;
  padding: 4px;
}
.file-list::-webkit-scrollbar { width: 6px; }
.file-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.file-list-empty {
  padding: 18px; text-align: center;
  color: var(--text-dim); font-size: 11px;
}
.file-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 8px;
  background: var(--card);
  margin: 2px 0;
}
.file-row-num {
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--accent-dim); color: var(--accent-hover);
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 700;
  flex-shrink: 0;
}
.file-row-num.concat {
  background: var(--violet-dim); color: var(--violet-hover);
}
.file-row-name {
  flex: 1; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* === Resolution chips === */
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  padding: 7px 14px; border-radius: 10px;
  border: 2px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px; font-weight: 700;
  cursor: pointer; transition: all 0.15s;
}
.chip:hover { background: var(--card-hover); color: var(--text); }
.chip.active.resize {
  border-color: var(--accent); background: var(--accent); color: white;
}
.chip.active.concat {
  border-color: var(--violet); background: var(--violet); color: white;
}
.chip-actions { display: flex; gap: 8px; margin-top: 10px; }

/* === Inputs === */
.input, .select {
  width: 100%;
  padding: 8px 12px; border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-family: inherit; font-size: 12px;
  outline: none;
  transition: border-color 0.15s;
}
.input:focus, .select:focus { border-color: var(--accent); }
.input::placeholder { color: var(--text-dim); }
.input.mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 11px; }
.input:disabled { opacity: 0.4; cursor: not-allowed; }

.field-row {
  display: grid; gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}
.field-label {
  font-size: 11px; color: var(--text-muted); font-weight: 600;
}

/* === Naming mode tabs === */
.mode-tabs {
  display: flex; gap: 6px; margin-bottom: 10px;
}
.mode-tab {
  padding: 7px 14px; border-radius: 8px;
  background: transparent; border: 1px solid var(--border);
  color: var(--text-muted);
  font-family: inherit; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: all 0.15s;
}
.mode-tab:hover { background: var(--card-hover); color: var(--text); }
.mode-tab.active.resize {
  background: var(--accent); border-color: var(--accent);
  color: white;
}
.mode-tab.active.concat {
  background: var(--violet); border-color: var(--violet);
  color: white;
}

.naming-section { display: none; }
.naming-section.active { display: block; }

.radio-row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.radio {
  display: flex; gap: 6px; align-items: center; cursor: pointer;
  font-size: 12px;
}
.radio input { margin: 0; accent-color: var(--accent); }

/* === Checkbox custom === */
.check {
  display: flex; gap: 8px; align-items: center;
  cursor: pointer; font-size: 12px;
}
.check-box {
  width: 18px; height: 18px; border-radius: 4px;
  border: 1.5px solid var(--border);
  background: var(--bg-elev);
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: all 0.15s;
}
.check.on .check-box {
  background: var(--accent); border-color: var(--accent);
}
.check.concat.on .check-box {
  background: var(--violet); border-color: var(--violet);
}
.check.on .check-box::after {
  content: ""; width: 5px; height: 9px;
  border: solid white; border-width: 0 2px 2px 0;
  transform: rotate(45deg) translate(-1px, -1px);
}

/* === Packshot rows === */
.packshot-row {
  display: grid;
  grid-template-columns: auto 100px 1fr auto;
  gap: 8px; align-items: center;
  margin-bottom: 4px;
}
.pack-dot {
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--text-dim);
  transition: background 0.2s;
}
.pack-dot.ok { background: var(--success); box-shadow: 0 0 8px var(--success); }
.pack-dot.bad { background: var(--danger); box-shadow: 0 0 8px var(--danger); }
.pack-res {
  font-family: "SF Mono", Menlo, monospace;
  font-size: 11px; font-weight: 700;
  color: var(--text-muted);
  background: var(--bg-elev);
  padding: 5px 10px; border-radius: 6px;
  text-align: center;
}

/* === Preview === */
.preview {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
  font-family: "SF Mono", Menlo, monospace; font-size: 11px;
  color: var(--accent);
}
.preview.concat { color: var(--violet); }

/* === Progress === */
.progress-wrap {
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 12px 16px;
  margin: 4px 20px;
}
.progress-head {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 6px;
}
.progress-title { font-size: 13px; font-weight: 700; }
.progress-pct { font-size: 20px; font-weight: 700; color: var(--text-muted); }
.progress {
  height: 10px; background: var(--bg-elev);
  border-radius: 5px; overflow: hidden;
}
.progress > div {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--success));
  transition: width 0.25s ease;
}
.progress.thin { height: 4px; margin-top: 2px; }
.current-name {
  font-family: "SF Mono", Menlo, monospace; font-size: 11px;
  color: var(--text-dim); margin-top: 6px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* === Actions === */
.actions {
  display: flex; justify-content: space-between;
  padding: 4px 20px 6px;
}
.btn-stop {
  padding: 10px 18px; border-radius: 10px;
  border: 1px solid var(--border);
  background: transparent; color: var(--text-muted);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: all 0.15s;
}
.btn-stop:hover:not(:disabled) {
  background: var(--card-hover); color: var(--text);
}
.btn-stop:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-start {
  padding: 12px 28px; border-radius: 12px;
  border: none; background: var(--accent);
  color: white; font-family: inherit; font-size: 14px; font-weight: 700;
  cursor: pointer; transition: all 0.15s;
  box-shadow: 0 4px 12px rgba(79, 140, 247, 0.3);
}
.btn-start.concat {
  background: var(--violet);
  box-shadow: 0 4px 12px rgba(167, 139, 250, 0.3);
}
.btn-start:hover { transform: translateY(-1px); }
.btn-start:disabled {
  opacity: 0.5; cursor: not-allowed;
  transform: none; box-shadow: none;
}

/* === Log === */
.log-wrap {
  background: var(--card);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  margin: 4px 20px 12px;
  padding: 8px 14px 12px;
}
.log-head {
  font-size: 10px; font-weight: 700; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.08em;
  margin-bottom: 4px;
}
.log {
  background: var(--log-bg);
  border-radius: 10px;
  padding: 8px 10px;
  height: 80px; overflow-y: auto;
  font-family: "SF Mono", Menlo, monospace; font-size: 10.5px;
  line-height: 1.6;
  user-select: text; -webkit-user-select: text;
}
.log::-webkit-scrollbar { width: 6px; }
.log::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.log-line { white-space: pre-wrap; }
.log-info { color: #cdd5e0; }
.log-success { color: var(--success); }
.log-warn { color: var(--warn); }
.log-error { color: var(--danger); }

/* === Ползунки тайминга === */
.range-slider {
  width: 100%;
  -webkit-appearance: none;
  height: 5px;
  border-radius: 3px;
  background: var(--border);
  outline: none;
  cursor: pointer;
}
.range-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--accent);
  cursor: pointer;
  border: 2px solid #fff;
  box-shadow: 0 1px 5px rgba(0,0,0,0.45);
}
.range-slider:disabled { opacity: 0.35; cursor: not-allowed; }
.trim-timeline {
  position: relative; height: 22px;
  background: var(--bg-elev); border-radius: 5px;
  margin: 10px 0 6px;
  overflow: hidden;
}
.trim-bar {
  position: absolute; top: 0; height: 100%;
  background: var(--accent-dim);
  border-left: 2px solid var(--accent);
  border-right: 2px solid var(--accent);
  transition: left 0.05s, width 0.05s;
  min-width: 4px;
}
.trim-slider-row {
  display: grid;
  grid-template-columns: 58px 1fr 44px;
  gap: 8px; align-items: center;
  margin-bottom: 5px;
}
.trim-val {
  font-family: "SF Mono", Menlo, monospace;
  font-size: 11px; color: var(--text);
  text-align: right;
}
.trim-info {
  font-size: 11px; color: var(--text-muted);
  display: flex; gap: 14px; flex-wrap: wrap;
  margin-top: 6px;
}
.trim-info b { color: var(--accent); font-family: "SF Mono", Menlo, monospace; }
.trim-info .pack-tag { color: var(--success); }

/* === Subtitle card === */
.subs-anim-grid { display: grid; grid-template-columns: repeat(5,1fr); gap:5px; margin:8px 0; }
.subs-anim-card { border-radius:8px; border:1.5px solid var(--border); padding:6px 4px;
  text-align:center; cursor:pointer; background:var(--bg-elev); font-size:11px; transition:all 0.15s; }
.subs-anim-card:hover { border-color:var(--accent); }
.subs-anim-card.active { border-color:var(--accent); background:var(--accent-dim); color:var(--accent); }
.subs-anim-icon { font-size:16px; margin-bottom:3px; }
.subs-style-row { display:flex; gap:8px; align-items:center; margin:6px 0; flex-wrap:wrap; }
.subs-pos-btn { padding:3px 10px; border-radius:6px; border:1.5px solid var(--border);
  background:var(--bg-elev); color:var(--text); font-size:11px; cursor:pointer; transition:all 0.15s; }
.subs-pos-btn.active { border-color:var(--accent); background:var(--accent-dim); color:var(--accent); }
.subs-progress-bar { height:4px; background:var(--bg-elev); border-radius:2px; margin:6px 0; overflow:hidden; }
.subs-progress-fill { height:100%; background:var(--accent); transition:width 0.3s; border-radius:2px; }
.subs-segment { display:grid; grid-template-columns:60px 1fr auto; gap:6px; align-items:start;
  padding:4px 0; border-bottom:1px solid var(--border-subtle); font-size:11px; }
.subs-seg-time { color:var(--text-dim); font-family:monospace; padding-top:3px; }
.subs-seg-text { resize:none; background:transparent; border:none; color:var(--text);
  font-size:11px; width:100%; outline:none; }
.subs-seg-text:focus { background:var(--bg-elev); border-radius:4px; padding:2px 4px; }
.subs-seg-del { color:var(--text-dim); cursor:pointer; font-size:13px; padding:0 2px; }
.subs-seg-del:hover { color:var(--error); }

/* === Слоты загрузки (ресайз) === */
.slot-pick-btn {
  width: 100%; padding: 10px 0; border-radius: 9px;
  border: 2px dashed var(--border);
  background: transparent; color: var(--text-muted);
  font-family: inherit; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: all 0.15s; text-align: center;
}
.slot-pick-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.slot-pick-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.slot-loaded .file-row-num { background: var(--success-dim); color: var(--success); }

/* === Concat Node Canvas === */
.cn-canvas {
  position: relative;
  overflow: auto;
  height: 420px;
  background: var(--bg-elev);
  border-radius: 10px;
  border: 1px solid var(--border-subtle);
  margin-bottom: 6px;
  cursor: default;
}
.cn-canvas::-webkit-scrollbar { width: 8px; height: 8px; }
.cn-canvas::-webkit-scrollbar-track { background: transparent; }
.cn-canvas::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.cn-inner {
  position: relative;
  min-width: 100%;
  min-height: 100%;
}
.cn-svg {
  position: absolute;
  top: 0; left: 0;
  pointer-events: none;
  overflow: visible;
}
.cn-node {
  position: absolute;
  width: 264px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: visible;
  transition: box-shadow 0.15s, border-color 0.15s;
  z-index: 5;
}
.cn-node:hover { border-color: var(--violet-dim); box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
.cn-node.cn-dragging { box-shadow: 0 8px 32px rgba(0,0,0,0.6); z-index: 100; border-color: var(--violet); }
.cn-node-head {
  display: flex; align-items: center; gap: 6px;
  padding: 9px 26px 9px 22px;
  cursor: grab;
  background: var(--card-hover);
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 12px 12px 0 0;
  -webkit-user-select: none; user-select: none;
}
.cn-node-head:active { cursor: grabbing; }
.cn-node-num {
  min-width: 22px; height: 22px; border-radius: 6px;
  background: var(--violet-dim); color: var(--violet-hover);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; flex-shrink: 0;
}
.cn-node-num.unconnected {
  background: var(--warn-dim); color: var(--warn);
}
.cn-node-name {
  flex: 1; font-size: 11px; color: var(--text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cn-node-close {
  width: 20px; height: 20px; padding: 0;
  border: none; background: transparent;
  color: var(--text-dim); font-size: 14px; line-height: 1;
  cursor: pointer; border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.15s; flex-shrink: 0;
}
.cn-node-close:hover { background: var(--danger-dim); color: var(--danger); }
.cn-port {
  position: absolute;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: var(--text-dim);
  border: 2.5px solid var(--bg-elev);
  top: 50%;
  transform: translateY(-50%);
  z-index: 20;
  transition: background 0.15s, transform 0.15s;
  pointer-events: all;
}
.cn-port.in { left: -9px; cursor: default; }
.cn-port.out { right: -9px; cursor: crosshair; }
.cn-port.out:hover { background: var(--violet); transform: translateY(-50%) scale(1.3); }
.cn-port.has-edge { background: var(--violet); border-color: var(--bg-elev); }
.cn-port.in.has-edge { background: var(--success); }
.cn-body { padding: 8px 10px 10px; }
.cn-placeholder {
  padding: 18px 10px; text-align: center;
  color: var(--text-dim); font-size: 11px;
  cursor: pointer; border-radius: 8px;
  border: 1.5px dashed var(--border);
  transition: all 0.15s; line-height: 1.6;
}
.cn-placeholder:hover { border-color: var(--violet); color: var(--violet); background: var(--violet-dim); }
.cn-video-wrap {
  border-radius: 8px; overflow: hidden;
  background: #000; margin-bottom: 8px;
  cursor: default; position: relative;
}
.cn-video-wrap video {
  width: 100%; height: 145px;
  display: block; object-fit: contain;
  cursor: default;
}
.cn-play-btn {
  position: absolute; bottom: 6px; left: 6px;
  width: 28px; height: 28px; border-radius: 50%;
  background: rgba(0,0,0,0.55); border: 1.5px solid rgba(255,255,255,0.35);
  color: #fff; font-size: 11px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; transition: background 0.15s;
  user-select: none;
}
.cn-play-btn:hover { background: rgba(0,0,0,0.8); }
.cn-mute-btn {
  position: absolute; bottom: 6px; left: 40px;
  width: 28px; height: 28px; border-radius: 50%;
  background: rgba(0,0,0,0.55); border: 1.5px solid rgba(255,255,255,0.35);
  color: #fff; font-size: 11px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; transition: background 0.15s;
  user-select: none;
}
.cn-mute-btn:hover { background: rgba(0,0,0,0.8); }
.cn-mute-btn.muted { border-color: var(--warn); color: var(--warn); }
.cn-file-row {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 6px; border-radius: 7px;
  background: var(--bg-elev);
  margin-bottom: 6px;
}
.cn-file-name {
  flex: 1; font-size: 10px; color: var(--text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cn-trim-label {
  font-size: 9px; font-weight: 700; color: var(--text-dim);
  text-transform: uppercase; letter-spacing: 0.07em;
  margin-bottom: 3px; margin-top: 4px;
}
.cn-duration-row {
  display: flex; justify-content: space-between;
  font-size: 10px; color: var(--text-muted);
  margin-top: 3px;
}
.cn-duration-row b { color: var(--violet); font-family: "SF Mono", Menlo, monospace; }
.cn-hint {
  font-size: 10px; color: var(--text-dim);
  margin-top: 5px; line-height: 1.5;
}
.cn-sequence-bar {
  display: flex; align-items: center; gap: 4px;
  flex-wrap: nowrap; overflow-x: auto;
  padding: 5px 8px; border-radius: 8px;
  background: var(--bg-elev);
  margin-bottom: 8px; min-height: 32px;
}
.cn-sequence-bar::-webkit-scrollbar { height: 4px; }
.cn-sequence-bar::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.cn-seq-item {
  background: var(--violet-dim); color: var(--violet);
  padding: 3px 8px; border-radius: 5px;
  font-size: 10px; font-weight: 700;
  white-space: nowrap; flex-shrink: 0;
}
.cn-seq-arrow { color: var(--text-dim); flex-shrink: 0; font-size: 11px; }
.cn-empty-hint {
  position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  text-align: center; pointer-events: none;
}
.cn-playhead {
  position: absolute; top: 0; height: 100%;
  width: 2px; background: var(--warn);
  pointer-events: none; opacity: 0.9;
  box-shadow: 0 0 4px var(--warn);
}
.cn-ph-label {
  position: absolute; bottom: calc(100% + 3px); left: 50%;
  transform: translateX(-50%);
  background: var(--warn); color: #111;
  font-size: 9px; font-weight: 700; line-height: 1.4;
  padding: 1px 5px; border-radius: 3px;
  white-space: nowrap; pointer-events: none;
}
.trim-manual {
  width: 46px; height: 22px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-size: 10px;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  text-align: center; padding: 0 3px;
  outline: none; -moz-appearance: textfield;
}
.trim-manual::-webkit-outer-spin-button,
.trim-manual::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
.trim-manual:focus { border-color: var(--violet); }
.cn-res-badge {
  font-size: 9px; font-weight: 800; padding: 2px 5px;
  border-radius: 4px; white-space: nowrap; flex-shrink: 0;
  cursor: pointer; transition: all 0.15s;
  border: 1.5px solid transparent;
}
.cn-res-badge:hover { border-color: var(--violet); }
</style>
</head>
<body>
<div class="app">

  <!-- ============= SIDEBAR ============= -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-logo">VR</div>
      <div class="brand-text">
        <div class="brand-title">Video Resizer</div>
        <div class="brand-version">v4.0 · macOS</div>
      </div>
    </div>
    <nav class="nav">
      <button class="nav-item active" data-tab="resize" onclick="switchTab('resize')">
        <div class="nav-item-title">Ресайз</div>
        <div class="nav-item-sub">видео → N разрешений</div>
      </button>
      <button class="nav-item" data-tab="concat" onclick="switchTab('concat')">
        <div class="nav-item-title">Склейка</div>
        <div class="nav-item-sub">2–10 роликов в один</div>
      </button>
      <button class="nav-item" data-tab="subs" onclick="switchTab('subs')">
        <div class="nav-item-title">Субтитры</div>
        <div class="nav-item-sub">авто + анимация (Whisper)</div>
      </button>
      <button class="nav-item" data-tab="sound" onclick="switchTab('sound')">
        <div class="nav-item-title">Звук</div>
        <div class="nav-item-sub">войсовер + TTS генератор</div>
      </button>
      <button class="nav-item" data-tab="clipopus" onclick="switchTab('clipopus')">
        <div class="nav-item-title">ClipOpus</div>
        <div class="nav-item-sub">ресайз из OpusClip API</div>
      </button>
    </nav>
    <div class="status-bar">
      <span id="status-dot" class="status-dot">●</span>
      <span id="status-text">проверка…</span>
    </div>
  </aside>

  <!-- ============= MAIN ============= -->
  <main class="main">
    <div class="section-head">
      <div id="section-title" class="section-title">Ресайз</div>
      <div id="section-sub" class="section-sub">1 файл → выбранные разрешения + пекшоты</div>
    </div>

    <div id="content" class="content">
      <!-- Содержимое заполняется JavaScript при switchTab -->
    </div>

    <!-- Прогресс -->
    <div class="progress-wrap">
      <div class="progress-head">
        <div id="progress-title" class="progress-title">Готов к запуску</div>
        <div id="progress-pct" class="progress-pct">0 %</div>
      </div>
      <div class="progress"><div id="bar-overall"></div></div>
      <div id="current-name" class="current-name">—</div>
      <div class="progress thin"><div id="bar-current"></div></div>
    </div>

    <!-- Кнопки -->
    <div class="actions">
      <button id="btn-stop" class="btn-stop" disabled onclick="stopProcessing()">■ Стоп</button>
      <button id="btn-start" class="btn-start" onclick="startProcessing()">▶ СТАРТ</button>
    </div>

    <!-- Лог -->
    <div class="log-wrap">
      <div class="log-head">Лог</div>
      <div id="log" class="log"></div>
    </div>
  </main>

</div>

<script>
// =====================================================================
// Описание слотов загрузки (ресайз): исходный формат → выходные разрешения
// =====================================================================
const SLOT_DEFS = [
  { id: "sq",   label: "1:1 → 1080×1080 + 1080×1350",
    hint: "квадратный исходник",
    outputs: ["1080x1080", "1080x1350"] },
  { id: "v916", label: "9:16 → 1080×1920",
    hint: "вертикальный исходник",
    outputs: ["1080x1920"] },
  { id: "h169", label: "16:9 → 1920×1080",
    hint: "горизонтальный исходник",
    outputs: ["1920x1080"] },
];

// Порядок и метки разрешений для склейки
const RES_ORDER  = ["1080x1080","1080x1350","1080x1920","1920x1080"];
const RES_LABELS = {"1080x1080":"1:1","1080x1350":"4:5","1080x1920":"9:16","1920x1080":"16:9"};
const RES_COLORS = {"1080x1080":"var(--accent)","1080x1350":"#a78bfa","1080x1920":"var(--success)","1920x1080":"var(--warn)"};
const RES_BG     = {"1080x1080":"var(--accent-dim)","1080x1350":"var(--violet-dim)","1080x1920":"var(--success-dim)","1920x1080":"var(--warn-dim)"};

// =====================================================================
// State
// =====================================================================
const state = {
  ffmpeg_ok: false,
  size_limit_mb: 100,
  fade_duration: 0.5,
  resolutions: ["1080x1080","1080x1350","1080x1920","1920x1080"],
  default_res: "1080x1350",
  known_teams: ["In-House","Freelance"],
  known_types: ["Gameplay","Unreal","Cinematic","Combo","UGC","AI","AI-Hook"],
  default_packshots: {},

  active_tab: "resize",
  running: false,

  // --- Resize ---
  resize: {
    // Слоты загрузки: {id: {file, on: {res: bool}}}
    slots: {
      sq:   { file: null, on: { "1080x1080": true, "1080x1350": true } },
      v916: { file: null, on: { "1080x1920": true } },
      h169: { file: null, on: { "1920x1080": true } },
    },
    naming_mode: "os",     // os | keep | custom
    team: "In-House",
    type: "Unreal",
    name_source: "auto",
    name_manual: "",
    template: "{name}_{resolution}",
    tag: "",
    packshot_on: true,
    packshots: {},         // {res: path}
    outdir: "",
    outdir_same: true,
    size_limit_on: false,
    // Тайминг ролика
    trim_on: false,
    trim_start: 0,
    trim_end: null,        // null = до конца исходника
    trim_src_duration: 0,  // длина первого файла (для ползунков)
  },

  // --- Subtitles (standalone tab) ---
  subs: {
    file: null,          // {path, name, stem} — picked video file
    outdir: "",
    outdir_same: true,
    language: "auto", model: "base",
    animation: "fade",
    font_size: 52, color: "#ffffff",
    position: "bottom",
    bg: true,
    segments: [],
    transcribing: false, progress_msg: "", progress_pct: 0,
    last_error: "",
    libass_missing: false,
  },

  // --- Sound tab ---
  sound: {
    file: null, duration: 0,
    thumbs: [],              // base64 JPEG data-URLs for timeline preview
    tracks: [],              // [{id, start, duration, file, name}]
    cursor: 0,
    tts_text: "",
    tts_lang: "ru",
    tts_gender: "female",
    tts_speed: 1.0,
    tts_generating: false,
    tts_progress: "",
    tts_pct: 0,
    tts_last_file: null,
    tts_last_dur: 0,
    tts_library: [],         // [{id, file, dur, label, lang, gender}]
    mixing: false,
    mix_progress: 0,
    mix_status: "",
    mix_last_dst: "",
    original_audio: "mix",
    outdir: "", outdir_same: true,
  },

  // --- Concat ---
  concat: {
    nodes: {},             // {id: {file, trim_start, trim_end, trim_dur, video_url, x, y, resolution}}
    edges: [],             // [{from: id, to: id}]
    nextId: 1,
    naming_mode: "os",     // os | custom
    team: "In-House",
    type: "Combo",
    name_source: "auto",
    name_manual: "",
    template: "concat_{date}_{resolution}",
    tag: "",
    outdir: "",
    outdir_same: true,
    size_limit_on: false,
    fade: false,
  },

  // --- ClipOpus ---
  clipopus: {
    api_key:    "",
    project_id: "",
    clips:      [],   // результат fetch_clipopus_clips
    slots: [
      { url: "", title: "", clip_idx: null, resolutions: { "1080x1080": true, "1080x1920": true, "1920x1080": false, "1080x1350": false } },
      { url: "", title: "", clip_idx: null, resolutions: { "1080x1080": true, "1080x1920": true, "1920x1080": false, "1080x1350": false } },
      { url: "", title: "", clip_idx: null, resolutions: { "1080x1080": true, "1080x1920": true, "1920x1080": false, "1080x1350": false } },
    ],
    outdir:      "",
    outdir_same: true,
    loading:     false,    // идёт загрузка клипов из API
    processing:  false,    // идёт скачивание + ресайз
    phase:       "idle",   // "idle" | "downloading" | "resizing"
    status_msg:  "",
  },
};

// =====================================================================
// API callbacks (Python -> JS)
// =====================================================================
window.api = {
  onLog: (text, level) => appendLog(text, level || "info"),
  onCurrentFile: (idx, total, name) => {
    document.getElementById("progress-title").textContent = `${idx} / ${total}`;
    document.getElementById("current-name").textContent = name;
    setBar("bar-current", 0);
  },
  onFileProgress: (idx, total, pct) => {
    setBar("bar-current", pct);
    const overall = ((idx - 1) + pct / 100) / Math.max(total, 1) * 100;
    setBar("bar-overall", overall);
    document.getElementById("progress-pct").textContent = `${Math.floor(overall)} %`;
  },
  onOverallProgress: (done, total) => {
    const v = done / Math.max(total, 1) * 100;
    setBar("bar-overall", v);
    document.getElementById("progress-pct").textContent = `${Math.floor(v)} %`;
  },
  onDone: (ok, total, cancelled) => {
    setBar("bar-current", 0);
    document.getElementById("progress-title").textContent =
      cancelled ? `Остановлено · ${ok} из ${total}` : `Готово · ${ok} из ${total}`;
    setRunning(false);
  },
};

window.subsApi = {
  onProgress(msg, pct) {
    state.subs.progress_msg = msg;
    state.subs.progress_pct = pct;
    // Try to update existing elements first (no full re-render)
    const fill  = document.getElementById("subs-progress-fill");
    const msgEl = document.getElementById("subs-progress-msg");
    if (fill)  fill.style.width   = pct + "%";
    if (msgEl) msgEl.textContent  = msg;
    // If the progress bar doesn't exist yet — render it
    if (!fill || !msgEl) renderSubsTabContent();
  },
  onDone(segments) {
    state.subs.segments     = segments;
    state.subs.transcribing = false;
    renderSubsTabContent();
    appendLog(`Субтитры: ${segments.length} фраз распознано`, "success");
  },
  onError(err) {
    state.subs.transcribing = false;
    state.subs.last_error   = err;
    renderSubsTabContent();
    appendLog("Субтитры: " + err, "error");
  },
  onSubsLibassError() {
    state.subs.libass_missing = true;
    renderSubsTabContent();
  },
};

window.soundApi = {
  // ── TTS ──────────────────────────────────────────────────────────
  onTTSProgress(msg, pct) {
    state.sound.tts_generating = true;
    state.sound.tts_progress   = msg;
    state.sound.tts_pct        = pct;
    const fill  = document.getElementById("tts-prog-fill");
    const msgEl = document.getElementById("tts-prog-msg");
    if (fill)  fill.style.width  = pct + "%";
    if (msgEl) msgEl.textContent = msg;
  },
  onTTSDone(file, dur) {
    state.sound.tts_generating = false;
    state.sound.tts_last_file  = file;
    state.sound.tts_last_dur   = dur;
    state.sound.tts_pct        = 100;
    // Add to library
    const rawText = (state.sound.tts_text || "").trim();
    const label   = rawText.length > 35 ? rawText.substring(0, 35) + "…" : (rawText || "Голос");
    state.sound.tts_library.push({
      id: Date.now() + Math.random(),
      file, dur,
      label,
      lang:   state.sound.tts_lang,
      gender: state.sound.tts_gender,
    });
    if (state.active_tab === "sound") renderSoundTabContent();
    appendLog(`TTS готово: ${label} (${dur.toFixed(1)}s)`, "success");
  },
  onTTSError(err) {
    state.sound.tts_generating = false;
    state.sound.tts_last_file  = null;
    if (state.active_tab === "sound") renderSoundTabContent();
    appendLog("TTS: " + err, "error");
  },
  // ── Mix ──────────────────────────────────────────────────────────
  onMixLog(text, level) {
    appendLog(text, level || "info");
  },
  onMixFile(name) {
    state.sound.mix_status = name;
    const el = document.getElementById("mix-status-txt");
    if (el) el.textContent = name;
  },
  onMixProgress(pct) {
    state.sound.mix_progress = pct;
    const fill = document.getElementById("mix-prog-fill");
    const pctEl = document.getElementById("mix-prog-pct");
    if (fill)  fill.style.width   = pct + "%";
    if (pctEl) pctEl.textContent  = pct + "%";
  },
  onMixDone(success, dst_path) {
    state.sound.mixing       = false;
    state.sound.mix_progress = success ? 100 : 0;
    state.sound.mix_last_dst = success ? dst_path : "";
    if (state.active_tab === "sound") renderSoundTabContent();
    if (success) appendLog("Готово: " + dst_path, "success");
  },
};

// =====================================================================
// ClipOpus API callbacks (Python -> JS)
// =====================================================================
window.clipOpusApi = {
  onDownload(idx, total, title, pct) {
    const co = state.clipopus;
    co.phase      = "downloading";
    co.status_msg = `Скачиваем ${idx + 1}/${total}: ${escapeHtml(title)} — ${pct}%`;
    const el = document.getElementById("co-status-bar");
    if (el) { el.textContent = co.status_msg; el.classList.add("visible"); }
    if (pct > 0) {
      setBar("bar-overall", (idx / total) * 100 + pct / total);
      setBar("bar-current", pct);
      document.getElementById("progress-pct").textContent =
        Math.floor((idx / total) * 100 + pct / total) + " %";
    }
  },
  onResizeStart(count) {
    state.clipopus.phase      = "resizing";
    state.clipopus.status_msg = `Ресайзим ${count} клип(а)…`;
    const el = document.getElementById("co-status-bar");
    if (el) el.textContent = state.clipopus.status_msg;
    document.getElementById("progress-title").textContent = "Ресайз ClipOpus…";
  },
  onAllDone() {
    state.clipopus.processing = false;
    state.clipopus.phase      = "idle";
    state.clipopus.status_msg = "";
    const el = document.getElementById("co-status-bar");
    if (el) el.classList.remove("visible");
    setBar("bar-overall", 100);
    document.getElementById("progress-pct").textContent = "100 %";
    document.getElementById("progress-title").textContent = "Готово";
    appendLog("✅ ClipOpus: все клипы ресайзнуты!", "success");
    if (state.active_tab === "clipopus") renderActiveTab();
  },
  onStopped() {
    state.clipopus.processing = false;
    state.clipopus.phase      = "idle";
    state.clipopus.status_msg = "";
    const el = document.getElementById("co-status-bar");
    if (el) el.classList.remove("visible");
    document.getElementById("progress-title").textContent = "Остановлено";
    if (state.active_tab === "clipopus") renderActiveTab();
  },
  onError(err) {
    state.clipopus.processing = false;
    state.clipopus.phase      = "idle";
    const el = document.getElementById("co-status-bar");
    if (el) el.classList.remove("visible");
    appendLog("ClipOpus: " + err, "error");
    if (state.active_tab === "clipopus") renderActiveTab();
  },
};

function setBar(id, pct) {
  document.getElementById(id).style.width = Math.max(0, Math.min(100, pct)) + "%";
}

function appendLog(text, level) {
  const el = document.getElementById("log");
  const line = document.createElement("div");
  line.className = "log-line log-" + (level || "info");
  const glyph = {info: "·", success: "✓", warn: "!", error: "✕"}[level] || "·";
  line.textContent = `${glyph}  ${text}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

// =====================================================================
// Init
// =====================================================================
async function init() {
  const s = await pywebview.api.get_state();
  Object.assign(state, s);
  // Заполним default packshots
  state.resize.packshots = {...(s.default_packshots || {})};
  updateStatus(state.ffmpeg_ok);
  if (!state.ffmpeg_ok) {
    appendLog("ffmpeg не найден в PATH", "error");
    appendLog("Установите:  brew install ffmpeg", "info");
  }

  // Загружаем сохранённый ClipOpus API-ключ
  try {
    const ck = await pywebview.api.load_clipopus_key();
    if (ck && ck.key) state.clipopus.api_key = ck.key;
  } catch(e) {}

  renderActiveTab();
}

function updateStatus(ok) {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  if (ok) { dot.className = "status-dot ok"; text.textContent = "FFmpeg готов"; }
  else { dot.className = "status-dot bad"; text.textContent = "FFmpeg не найден"; }
}

// =====================================================================
// Tab switching
// =====================================================================
function switchTab(tab) {
  state.active_tab = tab;
  // Обновим sidebar
  document.querySelectorAll(".nav-item").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  // Заголовок
  const title = document.getElementById("section-title");
  const sub = document.getElementById("section-sub");
  if (tab === "resize") {
    title.textContent = "Ресайз"; title.className = "section-title";
    sub.textContent = "1 файл → выбранные разрешения + пекшоты";
  } else if (tab === "concat") {
    title.textContent = "Склейка"; title.className = "section-title concat";
    sub.textContent = "2–10 роликов в один файл (для каждого размера отдельно)";
  } else if (tab === "subs") {
    title.textContent = "Субтитры"; title.className = "section-title subs";
    sub.textContent = "авто-распознавание + анимации (Whisper, локально)";
  } else if (tab === "sound") {
    title.textContent = "Звук"; title.className = "section-title sound";
    sub.textContent = "войсовер · TTS генератор · микшер";
  } else if (tab === "clipopus") {
    title.textContent = "ClipOpus"; title.className = "section-title";
    title.style.color = "#a855f7";
    sub.textContent = "скачать клипы из OpusClip API и ресайзнуть";
  }
  if (tab !== "clipopus") title.style.color = "";
  // Кнопка СТАРТ
  document.getElementById("btn-start").className =
    "btn-start" + (tab === "concat" ? " concat" : "");
  // Скрываем кнопки СТАРТ/СТОП для вкладки субтитров, звука и clipopus
  const actions = document.querySelector(".actions");
  if (actions) actions.style.display =
    (tab === "subs" || tab === "sound" || tab === "clipopus") ? "none" : "";

  renderActiveTab();
}

// =====================================================================
// Render: текущая вкладка
// =====================================================================
function renderActiveTab() {
  const content = document.getElementById("content");
  if (state.active_tab === "resize")   content.innerHTML = renderResizeTab();
  else if (state.active_tab === "concat")   content.innerHTML = renderConcatTab();
  else if (state.active_tab === "subs")     content.innerHTML = renderSubsTab();
  else if (state.active_tab === "sound")    content.innerHTML = renderSoundTab();
  else if (state.active_tab === "clipopus") content.innerHTML = renderClipOpusTab();
  // После того как DOM построен — навешиваем обработчики и обновляем превью
  bindEvents();
  if (state.active_tab === "subs" || state.active_tab === "sound" || state.active_tab === "clipopus") { updatePreview(); return; }
  renderFileList();
  renderChips();
  renderModeTabs();
  renderPackshotRows();
  renderTrimCard();
  updatePreview();
  // Concat: mount canvas drag handlers + draw edges after DOM settles
  if (state.active_tab === "concat") {
    setTimeout(() => { mountConcatCanvas(); drawCnEdges(); }, 20);
  }
}

function renderResizeTab() {
  const slots = state.resize.slots;
  const slotCards = SLOT_DEFS.map(def => {
    const slot = slots[def.id];
    const chips = def.outputs.map(r =>
      `<button class="chip ${slot.on[r] ? 'active resize' : ''}"
               onclick="toggleSlotOutput('${def.id}','${r}')">${r}</button>`
    ).join("");
    const fileRow = slot.file
      ? `<div class="file-row slot-loaded">
           <div class="file-row-num">✓</div>
           <div class="file-row-name">${escapeHtml(slot.file.name)}</div>
           <button class="btn btn-icon btn-ghost btn-danger" onclick="removeSlotFile('${def.id}')">✕</button>
         </div>`
      : `<button class="slot-pick-btn" onclick="pickSlotFile('${def.id}')">
           + Выбрать видео…
         </button>`;
    return `
      <div class="card">
        <div class="card-head">
          <span class="card-title">${def.label}</span>
          <span class="card-sub">${def.hint}</span>
        </div>
        <div class="card-body">
          <div class="chips" style="margin-bottom:8px;">${chips}</div>
          ${fileRow}
        </div>
      </div>`;
  }).join("");

  return slotCards + `

    <div class="card">
      <div class="card-head"><span class="card-title">Имя выходного файла</span></div>
      <div class="card-body">
        <div id="mode-tabs-resize" class="mode-tabs">
          <button class="mode-tab" data-mode="os" onclick="setNamingMode('resize', 'os')">OS-стандарт</button>
          <button class="mode-tab" data-mode="keep" onclick="setNamingMode('resize', 'keep')">Исходный нейминг</button>
          <button class="mode-tab" data-mode="custom" onclick="setNamingMode('resize', 'custom')">Свой шаблон</button>
        </div>

        <div id="naming-os-resize" class="naming-section">
          ${renderOSFields("resize")}
        </div>

        <div id="naming-keep-resize" class="naming-section">
          <div style="font-size:12px;color:var(--text);margin-bottom:4px;">
            Сохранит имя исходника как есть, поменяв только хвост с разрешением.
          </div>
          <div style="font-family:'SF Mono',Menlo,monospace;font-size:11px;color:var(--text-dim);">
            ID0001_OS_..._1080x1080.mp4 → ID0001_OS_..._1080x1350.mp4
          </div>
        </div>

        <div id="naming-custom-resize" class="naming-section">
          ${renderCustomFields("resize")}
        </div>

        <div id="preview-resize" class="preview">→ превью</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Пекшоты</span>
        <span class="card-sub">по одному на разрешение</span>
      </div>
      <div class="card-body">
        <label class="check ${state.resize.packshot_on ? 'on' : ''}" onclick="togglePackshotOn()">
          <span class="check-box"></span>
          Добавлять пекшот в конец ролика (+тайминг)
        </label>
        <div id="packshot-rows" style="margin-top:8px;"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Тайминг ролика</span>
        <span class="card-sub">обрезать исходник ползунками</span>
      </div>
      <div class="card-body">
        <label class="check ${state.resize.trim_on ? 'on' : ''}" onclick="toggleTrimOn()">
          <span class="check-box"></span>
          Задать тайминг вручную
        </label>
        <div id="trim-sliders"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Куда сохранять</span></div>
      <div class="card-body">
        <label class="check ${state.resize.outdir_same ? 'on' : ''}" onclick="toggleOutdirSame('resize')">
          <span class="check-box"></span>
          Рядом с исходником
        </label>
        <div class="field-row" style="grid-template-columns:1fr auto;margin-top:6px;">
          <input class="input mono" id="outdir-resize" placeholder="…или папка вывода"
                 value="${escapeHtml(state.resize.outdir)}"
                 ${state.resize.outdir_same ? "disabled" : ""}>
          <button class="btn" onclick="pickOutdir('resize')">Обзор…</button>
        </div>
        <div style="height:1px;background:var(--border-subtle);margin:10px 0;"></div>
        <label class="check ${state.resize.size_limit_on ? 'on' : ''}" onclick="toggleSizeLimit('resize')">
          <span class="check-box"></span>
          Ограничить размер каждого файла до ${state.size_limit_mb} МБ
        </label>
      </div>
    </div>
  `;
}

function renderConcatTab() {
  const s = state.concat;
  const ids = Object.keys(s.nodes);
  const csz = concatCanvasSize();
  const nodesHTML = ids.map(id => buildCnNodeHTML(parseInt(id))).join("");
  const seq = getSequence();
  const seqHTML = seq.length > 1
    ? seq.map((id, i) => {
        const n = s.nodes[id];
        const nm = (n && n.file)
          ? escapeHtml(n.file.name.replace(/\.[^.]+$/, "").substring(0, 16))
          : `#${id}`;
        return (i > 0 ? `<span class="cn-seq-arrow">→</span>` : "") +
               `<span class="cn-seq-item">${nm}</span>`;
      }).join("")
    : `<span style="font-size:10px;color:var(--text-dim)">
         Тяните ● OUT → ● IN между нодами чтобы задать порядок воспроизведения
       </span>`;
  const emptyHint = ids.length === 0
    ? `<div class="cn-empty-hint">
         <div style="font-size:28px;margin-bottom:8px;">🎬</div>
         <div style="font-size:12px;color:var(--text-dim)">Нажмите «+ Клип» для добавления</div>
       </div>` : "";

  return `
    <div class="card">
      <div class="card-head">
        <span class="card-title">Ноды клипов</span>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <label class="check concat ${state.concat.fade ? 'on' : ''}"
                 onclick="toggleFade()" style="margin-right:2px;">
            <span class="check-box"></span>
            <span style="font-size:11px;">Фейд (${state.fade_duration}с)</span>
          </label>
          <span style="font-size:10px;color:var(--text-dim);">+ клип:</span>
          ${RES_ORDER.map(r =>
            `<button class="btn" style="font-size:10px;padding:4px 9px;border-color:${RES_COLORS[r]};color:${RES_COLORS[r]};"
                     onclick="addConcatNode('${r}')" ${state.running ? "disabled" : ""}>${RES_LABELS[r]}</button>`
          ).join("")}
          <div style="width:1px;height:14px;background:var(--border);flex-shrink:0;"></div>
          <button class="btn" id="btn-clone-all-res"
                  style="font-size:10px;padding:4px 10px;
                         background:var(--violet-dim);color:var(--violet);
                         border-color:var(--violet)60;white-space:nowrap;"
                  onclick="cloneSeqToAllResolutions()"
                  ${state.running ? "disabled" : ""}
                  title="Клонировать текущую последовательность на все остальные форматы">
            ⊕ На все форматы
          </button>
        </div>
      </div>
      <div class="card-body" style="padding:8px 12px 10px;">
        <div class="cn-sequence-bar" id="cn-sequence">${seqHTML}</div>
        <div class="cn-canvas" id="cn-canvas">
          <div class="cn-inner" id="cn-inner"
               style="width:${csz.w}px;height:${csz.h}px;">
            <svg class="cn-svg" id="cn-svg"
                 width="${csz.w}" height="${csz.h}"
                 xmlns="http://www.w3.org/2000/svg"></svg>
            ${nodesHTML}
            ${emptyHint}
          </div>
        </div>
        <div class="cn-hint">
          Перемещайте ноды за заголовок · Тяните <span style="color:var(--violet)">●</span> OUT (правый порт) → <span style="color:var(--success)">●</span> IN (левый порт) для задания порядка
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Имя выходного файла</span></div>
      <div class="card-body">
        <div id="mode-tabs-concat" class="mode-tabs">
          <button class="mode-tab" data-mode="os" onclick="setNamingMode('concat', 'os')">OS-стандарт</button>
          <button class="mode-tab" data-mode="custom" onclick="setNamingMode('concat', 'custom')">Свой шаблон</button>
        </div>
        <div id="naming-os-concat" class="naming-section">
          ${renderOSFields("concat")}
        </div>
        <div id="naming-custom-concat" class="naming-section">
          ${renderCustomFields("concat")}
        </div>
        <div id="preview-concat" class="preview concat">→ превью</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Куда сохранять</span></div>
      <div class="card-body">
        <label class="check concat ${state.concat.outdir_same ? 'on' : ''}" onclick="toggleOutdirSame('concat')">
          <span class="check-box"></span>
          В папке первого файла
        </label>
        <div class="field-row" style="grid-template-columns:1fr auto;margin-top:6px;">
          <input class="input mono" id="outdir-concat" placeholder="…или папка вывода"
                 value="${escapeHtml(state.concat.outdir)}"
                 ${state.concat.outdir_same ? "disabled" : ""}>
          <button class="btn" onclick="pickOutdir('concat')">Обзор…</button>
        </div>
        <div style="height:1px;background:var(--border-subtle);margin:10px 0;"></div>
        <label class="check concat ${state.concat.size_limit_on ? 'on' : ''}" onclick="toggleSizeLimit('concat')">
          <span class="check-box"></span>
          Ограничить размер каждого файла до ${state.size_limit_mb} МБ
        </label>
      </div>
    </div>
  `;
}

// =====================================================================
// Concat node graph
// =====================================================================
let cnDrag = null;
// cnDrag = {type:"node", id, sx, sy, ox, oy}  OR
//          {type:"edge", fromId, fx, fy, cx, cy}

function addConcatNode(resolution) {
  if (state.running) return;
  resolution = resolution || RES_ORDER[RES_ORDER.length - 1];
  const id = state.concat.nextId++;
  const count = Object.keys(state.concat.nodes).length;
  state.concat.nodes[id] = {
    file: null, trim_start: 0, trim_end: null,
    trim_dur: 0, video_url: null,
    x: 20 + count * 295, y: 20,
    resolution: resolution,
  };
  renderActiveTab();
}

function cycleCnNodeRes(id) {
  if (state.running) return;
  const node = state.concat.nodes[id];
  if (!node) return;
  const idx = RES_ORDER.indexOf(node.resolution);
  node.resolution = RES_ORDER[(idx + 1) % RES_ORDER.length];
  refreshCnNode(id);
  updatePreview();
}

function removeConcatNode(id) {
  if (state.running) return;
  delete state.concat.nodes[id];
  state.concat.edges = state.concat.edges.filter(e => e.from !== id && e.to !== id);
  renderActiveTab();
}

async function pickNodeFile(id) {
  if (state.running) return;
  const files = await pywebview.api.pick_files();
  if (!files || !files.length) return;
  const f = files[0];
  const node = state.concat.nodes[id];
  if (!node) return;
  node.file = f;
  const info = await pywebview.api.get_file_info(f.path);
  if (info && info.duration > 0) {
    node.trim_dur = info.duration;
    node.trim_end = info.duration;
  }
  const url = await pywebview.api.get_video_url(f.path);
  node.video_url = url;
  refreshCnNode(id);
  updatePreview();
}

function onNodeTrimStart(id, val) {
  const node = state.concat.nodes[id];
  if (!node) return;
  const dur = node.trim_dur || 60;
  const end = node.trim_end !== null ? node.trim_end : dur;
  // Зажимаем start чтобы не превышал end-0.1; end не трогаем
  node.trim_start = Math.max(0, Math.min(parseFloat(val), end - 0.1));
  const el = document.getElementById(`cn-trim-${id}`);
  if (el) el.innerHTML = buildCnTrimHTML(id);
}

function onNodeTrimEnd(id, val) {
  const node = state.concat.nodes[id];
  if (!node) return;
  const dur = node.trim_dur || 60;
  // Зажимаем end чтобы не упал ниже start+0.1; start не трогаем
  node.trim_end = Math.max(node.trim_start + 0.1, Math.min(parseFloat(val), dur));
  const el = document.getElementById(`cn-trim-${id}`);
  if (el) el.innerHTML = buildCnTrimHTML(id);
}

function onNodeTrimStartManual(id, val) {
  if (isNaN(val)) return;
  const node = state.concat.nodes[id];
  if (!node) return;
  const dur = node.trim_dur || 60;
  const end = node.trim_end !== null ? node.trim_end : dur;
  node.trim_start = Math.max(0, Math.min(val, end - 0.1));
  const el = document.getElementById(`cn-trim-${id}`);
  if (el) el.innerHTML = buildCnTrimHTML(id);
}

function onNodeTrimEndManual(id, val) {
  if (isNaN(val)) return;
  const node = state.concat.nodes[id];
  if (!node) return;
  const dur = node.trim_dur || 60;
  node.trim_end = Math.max(node.trim_start + 0.1, Math.min(val, dur));
  const el = document.getElementById(`cn-trim-${id}`);
  if (el) el.innerHTML = buildCnTrimHTML(id);
}

function cnUpdatePlayhead(id, video) {
  const ph = document.getElementById(`cn-ph-${id}`);
  if (!ph) return;
  const dur = video.duration || (state.concat.nodes[id] || {}).trim_dur || 0;
  if (!dur || isNaN(dur)) return;
  const pct = (video.currentTime / dur * 100).toFixed(1);
  ph.style.left = pct + "%";
  ph.style.display = "block";
  const lbl = document.getElementById(`cn-phl-${id}`);
  if (lbl) lbl.textContent = video.currentTime.toFixed(1) + "s";
}

function cnSeek(id, e) {
  const tl = document.getElementById(`cn-tl-${id}`);
  const video = document.getElementById(`cn-vid-${id}`);
  if (!tl || !video) return;
  const rect = tl.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const dur = video.duration || (state.concat.nodes[id] || {}).trim_dur || 0;
  if (!dur || isNaN(dur)) return;
  video.currentTime = pct * dur;
  // Show playhead immediately
  const ph = document.getElementById(`cn-ph-${id}`);
  if (ph) { ph.style.left = (pct * 100).toFixed(1) + "%"; ph.style.display = "block"; }
  const lbl = document.getElementById(`cn-phl-${id}`);
  if (lbl) lbl.textContent = (pct * dur).toFixed(1) + "s";
}

function cnToggleMute(id) {
  const video = document.getElementById(`cn-vid-${id}`);
  const btn   = document.getElementById(`cn-mbtn-${id}`);
  if (!video || !btn) return;
  video.muted = !video.muted;
  btn.textContent = video.muted ? "🔇" : "🔊";
  btn.classList.toggle("muted", video.muted);
}

function buildCnTrimHTML(id) {
  const node = state.concat.nodes[id];
  if (!node || !node.file) return "";
  const dur   = node.trim_dur > 0 ? node.trim_dur : 60;
  const start = Math.max(0, node.trim_start);
  const end   = node.trim_end !== null ? Math.min(node.trim_end, dur) : dur;
  const clip  = Math.max(0, end - start);
  const ps    = (start / dur * 100).toFixed(1);
  const pw    = Math.max(0, (end - start) / dur * 100).toFixed(1);
  const res   = node.resolution || RES_ORDER[RES_ORDER.length - 1];
  const col   = RES_COLORS[res] || "var(--violet)";
  const bgCol = RES_BG[res]    || "var(--violet-dim)";
  return `
    <div>
      <div class="cn-trim-label">Тайминг</div>
      <div class="trim-timeline" style="height:14px;margin:14px 0 4px;overflow:visible;cursor:pointer;" id="cn-tl-${id}"
           onmousedown="event.stopPropagation(); cnSeek(${id}, event)"
           onclick="cnSeek(${id}, event)">
        <div class="trim-bar" style="left:${ps}%;width:${pw}%;
             border-color:${col};background:${bgCol};"></div>
        <div class="cn-playhead" id="cn-ph-${id}" style="display:none;left:0%;">
          <div class="cn-ph-label" id="cn-phl-${id}">0.0s</div>
        </div>
      </div>
      <div class="trim-slider-row" style="grid-template-columns:38px 1fr 50px;margin-bottom:3px;">
        <span class="field-label">Start</span>
        <input type="range" class="range-slider" style="height:3px;"
               min="0" max="${Math.max(0, end - 0.1).toFixed(1)}" step="0.1"
               value="${start.toFixed(1)}"
               oninput="onNodeTrimStart(${id},parseFloat(this.value))"
               onmousedown="event.stopPropagation()">
        <input type="number" class="trim-manual"
               value="${start.toFixed(1)}" min="0"
               max="${Math.max(0, end - 0.1).toFixed(1)}" step="0.1"
               onchange="onNodeTrimStartManual(${id},parseFloat(this.value))"
               onmousedown="event.stopPropagation()">
      </div>
      <div class="trim-slider-row" style="grid-template-columns:38px 1fr 50px;">
        <span class="field-label">End</span>
        <input type="range" class="range-slider" style="height:3px;"
               min="${Math.min(dur, start + 0.1).toFixed(1)}" max="${dur.toFixed(1)}"
               step="0.1" value="${end.toFixed(1)}"
               oninput="onNodeTrimEnd(${id},parseFloat(this.value))"
               onmousedown="event.stopPropagation()">
        <input type="number" class="trim-manual"
               value="${end.toFixed(1)}"
               min="${Math.min(dur, start + 0.1).toFixed(1)}" max="${dur.toFixed(1)}"
               step="0.1"
               onchange="onNodeTrimEndManual(${id},parseFloat(this.value))"
               onmousedown="event.stopPropagation()">
      </div>
      <div class="cn-duration-row">
        <span>Клип: <b style="color:${col}">${clip.toFixed(1)}s</b></span>
        <span style="color:var(--text-dim)">из ${dur.toFixed(1)}s</span>
      </div>
    </div>`;
}

function buildCnNodeHTML(id) {
  const node = state.concat.nodes[id];
  if (!node) return "";
  const seq    = getSequence();
  const seqPos = seq.indexOf(id);
  const hasIn  = state.concat.edges.some(e => e.to   === id);
  const hasOut = state.concat.edges.some(e => e.from === id);
  const name   = node.file ? escapeHtml(node.file.name) : "Нет файла";
  const res    = node.resolution || RES_ORDER[RES_ORDER.length - 1];
  const resLbl = RES_LABELS[res] || res;
  const resCol = RES_COLORS[res] || "var(--text-muted)";
  const resBg  = RES_BG[res]    || "var(--card-hover)";
  const seqNum = seqPos >= 0 ? `<span style="font-size:9px;color:var(--text-dim);margin-left:2px;">#${seqPos+1}</span>` : "";

  const bodyHTML = node.file
    ? `${node.video_url
        ? `<div class="cn-video-wrap">
             <video id="cn-vid-${id}" src="${escapeHtml(node.video_url)}" preload="metadata"
                    onmousedown="event.stopPropagation()"
                    ontimeupdate="cnUpdatePlayhead(${id},this)"
                    onplay="document.getElementById('cn-pbtn-${id}').textContent='⏸'"
                    onpause="document.getElementById('cn-pbtn-${id}').textContent='▶'"
                    onended="document.getElementById('cn-pbtn-${id}').textContent='▶'"></video>
             <div class="cn-play-btn" id="cn-pbtn-${id}"
                  onmousedown="event.stopPropagation()"
                  onclick="(function(){const v=document.getElementById('cn-vid-${id}');v.paused?v.play():v.pause();})()">▶</div>
             <div class="cn-mute-btn" id="cn-mbtn-${id}"
                  onmousedown="event.stopPropagation()"
                  onclick="cnToggleMute(${id})">🔊</div>
           </div>`
        : `<div class="cn-file-row">
             <span class="cn-node-num" style="background:var(--success-dim);color:var(--success);">✓</span>
             <span class="cn-file-name">${escapeHtml(node.file.name)}</span>
             <button class="btn btn-icon btn-ghost btn-danger"
                     onclick="removeNodeFile(${id});event.stopPropagation()">✕</button>
           </div>`
      }
       <div id="cn-trim-${id}">${buildCnTrimHTML(id)}</div>`
    : `<div class="cn-placeholder"
              onclick="pickNodeFile(${id});event.stopPropagation()">
         + Нажмите для загрузки видео
       </div>`;

  return `
    <div class="cn-node" id="cn-node-${id}" data-cn-id="${id}"
         style="left:${node.x}px;top:${node.y}px;">
      <div class="cn-port in${hasIn ? ' has-edge' : ''}"
           data-cn-port="in" data-cn-id="${id}"></div>
      <div class="cn-node-head" data-cn-drag="${id}">
        <div class="cn-res-badge"
             style="background:${resBg};color:${resCol};"
             onclick="cycleCnNodeRes(${id});event.stopPropagation()"
             title="Разрешение: ${escapeHtml(res)} · клик — сменить">${resLbl}</div>
        ${seqNum}
        <div class="cn-node-name" style="margin-left:2px;flex:1;min-width:0;">${name}</div>
        ${node.file ? `<button class="cn-node-close" style="opacity:0.55;font-size:13px;"
                onclick="pickNodeFile(${id});event.stopPropagation()"
                title="Заменить видео">⇄</button>` : ""}
        <button class="cn-node-close"
                onclick="removeConcatNode(${id});event.stopPropagation()"
                title="Удалить">✕</button>
      </div>
      <div class="cn-body">${bodyHTML}</div>
      <div class="cn-port out${hasOut ? ' has-edge' : ''}"
           data-cn-port="out" data-cn-id="${id}"></div>
    </div>`;
}

function removeNodeFile(id) {
  if (state.running) return;
  const node = state.concat.nodes[id];
  if (!node) return;
  node.file = null; node.video_url = null;
  node.trim_start = 0; node.trim_end = null; node.trim_dur = 0;
  refreshCnNode(id);
}

function refreshCnNode(id) {
  const el = document.getElementById(`cn-node-${id}`);
  if (!el) { renderActiveTab(); return; }
  const tmp = document.createElement("div");
  tmp.innerHTML = buildCnNodeHTML(id);
  el.parentNode.replaceChild(tmp.firstElementChild, el);
  updateSeqBar();
  drawCnEdges();
}

function concatCanvasSize() {
  const nodes = state.concat.nodes;
  const ids = Object.keys(nodes);
  const NW = 264, NH = 360;
  let w = 600, h = 340;
  for (const id of ids) {
    const n = nodes[id];
    const el = document.getElementById(`cn-node-${id}`);
    const nh = el ? el.offsetHeight : NH;
    w = Math.max(w, n.x + NW + 60);
    h = Math.max(h, n.y + nh + 40);
  }
  return {w, h};
}

function getSequence() {
  const nodes = state.concat.nodes;
  const edges = state.concat.edges;
  const ids = Object.keys(nodes).map(Number);
  if (!ids.length) return [];
  if (!edges.length) return ids.sort((a, b) => a - b);
  const nextMap = {};
  const hasPrev = new Set();
  for (const e of edges) { nextMap[e.from] = e.to; hasPrev.add(e.to); }
  const roots = ids.filter(id => !hasPrev.has(id));
  const result = [], visited = new Set();
  for (const root of roots.sort((a,b)=>a-b)) {
    let cur = root;
    while (cur !== undefined && !visited.has(cur) && nodes[cur]) {
      visited.add(cur); result.push(cur);
      cur = nextMap[cur];
    }
  }
  // append orphaned nodes not reachable from any root
  for (const id of ids.sort((a,b)=>a-b)) {
    if (!visited.has(id)) result.push(id);
  }
  return result;
}

function getPortPos(id, side) {
  const el = document.getElementById(`cn-node-${id}`);
  if (!el) {
    const n = state.concat.nodes[id];
    if (!n) return {x: 0, y: 0};
    return {x: side === "in" ? n.x : n.x + 264, y: n.y + 30};
  }
  const x = side === "in" ? el.offsetLeft : el.offsetLeft + el.offsetWidth;
  const y = el.offsetTop + el.offsetHeight / 2;
  return {x, y};
}

function makeBezier(p1, p2, color, w, dashed) {
  const dx = Math.max(Math.abs(p2.x - p1.x) * 0.55, 60);
  const dash = dashed ? 'stroke-dasharray="7 4"' : '';
  return `<path d="M${p1.x},${p1.y} C${p1.x+dx},${p1.y} ${p2.x-dx},${p2.y} ${p2.x},${p2.y}"
               stroke="${color}" stroke-width="${w}" fill="none"
               stroke-linecap="round" ${dash}/>`;
}

function drawCnEdges(tempEdge) {
  const svg = document.getElementById("cn-svg");
  if (!svg) return;
  let html = "";
  for (const e of state.concat.edges) {
    if (!state.concat.nodes[e.from] || !state.concat.nodes[e.to]) continue;
    const p1 = getPortPos(e.from, "out");
    const p2 = getPortPos(e.to,   "in");
    html += makeBezier(p1, p2, "var(--violet)", 2.5, false);
  }
  if (tempEdge) {
    html += makeBezier(
      {x: tempEdge.fx, y: tempEdge.fy},
      {x: tempEdge.cx, y: tempEdge.cy},
      "rgba(167,139,250,0.45)", 2, true);
  }
  svg.innerHTML = html;
}

function updateSeqBar() {
  const el = document.getElementById("cn-sequence");
  if (!el) return;
  const seq = getSequence();
  const nodes = state.concat.nodes;
  el.innerHTML = seq.length > 1
    ? seq.map((id, i) => {
        const n = nodes[id];
        const nm = (n && n.file)
          ? escapeHtml(n.file.name.replace(/\.[^.]+$/,"").substring(0,16))
          : `#${id}`;
        return (i > 0 ? `<span class="cn-seq-arrow">→</span>` : "") +
               `<span class="cn-seq-item">${nm}</span>`;
      }).join("")
    : `<span style="font-size:10px;color:var(--text-dim)">
         Тяните ● OUT → ● IN между нодами чтобы задать порядок
       </span>`;
}

function getCnCoords(e) {
  const canvas = document.getElementById("cn-canvas");
  if (!canvas) return {x: 0, y: 0};
  const cr = canvas.getBoundingClientRect();
  return {
    x: e.clientX - cr.left + canvas.scrollLeft,
    y: e.clientY - cr.top  + canvas.scrollTop,
  };
}

function mountConcatCanvas() {
  const canvas = document.getElementById("cn-canvas");
  if (!canvas || canvas._cnMounted) return;
  canvas._cnMounted = true;
  canvas.addEventListener("mousedown", onCnDown);
  // NOTE: mousemove / mouseup are added ONCE at startup (see below),
  // NOT here — adding them here caused accumulation on every re-render.
}

function onCnDown(e) {
  if (state.running) return;
  const target = e.target;

  // OUT port drag → start edge drawing
  if (target.dataset.cnPort === "out") {
    const id  = parseInt(target.dataset.cnId);
    const pos = getPortPos(id, "out");
    cnDrag = {type: "edge", fromId: id, fx: pos.x, fy: pos.y, cx: pos.x, cy: pos.y};
    e.preventDefault(); return;
  }

  // Node head drag → start moving node
  const head = target.closest("[data-cn-drag]");
  if (head) {
    const id = parseInt(head.dataset.cnDrag);
    const node = state.concat.nodes[id];
    if (!node) return;
    const coords = getCnCoords(e);
    cnDrag = {type: "node", id, sx: coords.x, sy: coords.y, ox: node.x, oy: node.y};
    const el = document.getElementById(`cn-node-${id}`);
    if (el) el.classList.add("cn-dragging");
    e.preventDefault();
  }
}

function onCnMove(e) {
  if (!cnDrag) return;
  if (cnDrag.type === "node") {
    const coords = getCnCoords(e);
    const node = state.concat.nodes[cnDrag.id];
    if (!node) return;
    node.x = Math.max(0, cnDrag.ox + coords.x - cnDrag.sx);
    node.y = Math.max(0, cnDrag.oy + coords.y - cnDrag.sy);
    const el = document.getElementById(`cn-node-${cnDrag.id}`);
    if (el) { el.style.left = node.x + "px"; el.style.top = node.y + "px"; }
    const sz = concatCanvasSize();
    const inner = document.getElementById("cn-inner");
    const svg   = document.getElementById("cn-svg");
    if (inner) { inner.style.width = sz.w + "px"; inner.style.height = sz.h + "px"; }
    if (svg)   { svg.setAttribute("width", sz.w); svg.setAttribute("height", sz.h); }
    drawCnEdges();
  } else if (cnDrag.type === "edge") {
    const coords = getCnCoords(e);
    cnDrag.cx = coords.x; cnDrag.cy = coords.y;
    drawCnEdges(cnDrag);
  }
}

function onCnUp(e) {
  if (!cnDrag) return;
  if (cnDrag.type === "node") {
    const el = document.getElementById(`cn-node-${cnDrag.id}`);
    if (el) el.classList.remove("cn-dragging");
  } else if (cnDrag.type === "edge") {
    // Find if we dropped on an IN port
    const hits = document.elementsFromPoint(e.clientX, e.clientY);
    const inPort = hits.find(el => el.dataset && el.dataset.cnPort === "in");
    if (inPort) {
      const toId = parseInt(inPort.dataset.cnId);
      if (toId !== cnDrag.fromId) {
        // Remove old edges from this OUT and into this IN
        state.concat.edges = state.concat.edges.filter(
          ed => ed.from !== cnDrag.fromId && ed.to !== toId);
        state.concat.edges.push({from: cnDrag.fromId, to: toId});
        // Refresh port dots
        [cnDrag.fromId, toId].forEach(id => {
          const el = document.getElementById(`cn-node-${id}`);
          if (!el) return;
          const hasIn  = state.concat.edges.some(ed => ed.to   === id);
          const hasOut = state.concat.edges.some(ed => ed.from === id);
          const ip = el.querySelector("[data-cn-port='in']");
          const op = el.querySelector("[data-cn-port='out']");
          if (ip) ip.classList.toggle("has-edge", hasIn);
          if (op) op.classList.toggle("has-edge", hasOut);
        });
        updateSeqBar();
        updatePreview();
      }
    }
    drawCnEdges();
  }
  cnDrag = null;
}

function renderOSFields(tab) {
  const s = state[tab];
  const teams = state.known_teams.map(t =>
    `<option value="${t}" ${t === s.team ? "selected" : ""}>${t}</option>`).join("");
  const types = state.known_types.map(t =>
    `<option value="${t}" ${t === s.type ? "selected" : ""}>${t}</option>`).join("");
  const autoLabel = tab === "concat" ? "из инициалов файлов" : "из имени файла";
  return `
    <div class="field-row" style="grid-template-columns:auto 1fr auto 1fr;">
      <div class="field-label">Команда</div>
      <select class="select" data-os="team" data-tab="${tab}">${teams}</select>
      <div class="field-label">Тип</div>
      <select class="select" data-os="type" data-tab="${tab}">${types}</select>
    </div>
    <div class="field-row" style="grid-template-columns:auto 1fr;margin-top:8px;">
      <div class="field-label">Имя</div>
      <div class="radio-row">
        <label class="radio">
          <input type="radio" name="ns-${tab}" value="auto" data-os="name_source" data-tab="${tab}"
                 ${s.name_source === "auto" ? "checked" : ""}>
          ${autoLabel}
        </label>
        <label class="radio">
          <input type="radio" name="ns-${tab}" value="manual" data-os="name_source" data-tab="${tab}"
                 ${s.name_source === "manual" ? "checked" : ""}>
          вручную:
        </label>
        <input class="input" data-os="name_manual" data-tab="${tab}"
               placeholder="My-Cool-Video" style="max-width:240px;"
               value="${escapeHtml(s.name_manual)}">
      </div>
    </div>
  `;
}

function renderCustomFields(tab) {
  const s = state[tab];
  return `
    <div class="field-row" style="grid-template-columns:auto 1fr auto 1fr;">
      <div class="field-label">Шаблон</div>
      <input class="input mono" data-cust="template" data-tab="${tab}"
             value="${escapeHtml(s.template)}">
      <div class="field-label">Тег</div>
      <input class="input" data-cust="tag" data-tab="${tab}"
             placeholder="UGC, Combo…" value="${escapeHtml(s.tag)}">
    </div>
    <div style="font-size:10px;color:var(--text-dim);margin-top:4px;">
      Переменные: {name} · {tag} · {resolution} · {date} · {index}
    </div>
  `;
}

function bindEvents() {
  const tab = state.active_tab;
  // OS-поля
  document.querySelectorAll('[data-os][data-tab="' + tab + '"]').forEach(el => {
    const key = el.dataset.os;
    if (el.type === "radio") {
      el.addEventListener("change", e => {
        if (e.target.checked) { state[tab][key] = e.target.value; updatePreview(); }
      });
    } else {
      el.addEventListener("change", e => { state[tab][key] = e.target.value; updatePreview(); });
      el.addEventListener("input",  e => { state[tab][key] = e.target.value; updatePreview(); });
    }
  });
  // custom-поля
  document.querySelectorAll('[data-cust][data-tab="' + tab + '"]').forEach(el => {
    const key = el.dataset.cust;
    el.addEventListener("input", e => { state[tab][key] = e.target.value; updatePreview(); });
  });
  // папка вывода
  const out = document.getElementById("outdir-" + tab);
  if (out) {
    out.addEventListener("input", e => {
      state[tab].outdir = e.target.value;
      if (e.target.value) state[tab].outdir_same = false;
    });
  }
}

function renderFileList() {
  // Both tabs now use their own rendering (slots for resize, nodes for concat)
  return;
}

function renderChips() {
  if (state.active_tab === "resize") return; // resize использует слоты
  const tab = state.active_tab;
  const el = document.getElementById("chips-" + tab);
  if (!el) return;
  el.innerHTML = state.resolutions.map(r => {
    const on = state[tab].resolutions[r];
    return `<button class="chip ${on ? `active ${tab}` : ''}" onclick="toggleChip('${tab}','${r}')">${r}</button>`;
  }).join("");
}

function renderModeTabs() {
  const tab = state.active_tab;
  const el = document.getElementById("mode-tabs-" + tab);
  if (!el) return;
  const mode = state[tab].naming_mode;
  el.querySelectorAll(".mode-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
    btn.classList.toggle(tab, btn.dataset.mode === mode);
  });
  const validModes = tab === "concat" ? ["os","custom"] : ["os","keep","custom"];
  validModes.forEach(m => {
    const sec = document.getElementById(`naming-${m}-${tab}`);
    if (sec) sec.classList.toggle("active", m === mode);
  });
}

function renderPackshotRows() {
  const el = document.getElementById("packshot-rows");
  if (!el) return;
  const ps = state.resize.packshots;
  const on = state.resize.packshot_on;
  el.innerHTML = state.resolutions.map(r => {
    const path = ps[r] || "";
    const exists = !!path;
    return `
      <div class="packshot-row">
        <span class="pack-dot ${exists ? 'ok' : (path ? 'bad' : '')}"></span>
        <span class="pack-res">${r}</span>
        <input class="input mono" placeholder="путь к пекшоту ${r}"
               value="${escapeHtml(path)}"
               oninput="state.resize.packshots['${r}']=this.value; refreshPackshotDot('${r}', this)"
               ${on ? '' : 'disabled'}>
        <button class="btn" onclick="pickPackshot('${r}')" ${on ? '' : 'disabled'}>…</button>
      </div>
    `;
  }).join("");
}

function refreshPackshotDot(res, inputEl) {
  // Простая проверка длины пути — реальную проверку существования делаем при старте
  const dot = inputEl.parentElement.querySelector(".pack-dot");
  pywebview.api.file_exists(inputEl.value).then(exists => {
    dot.className = "pack-dot " + (exists ? "ok" : (inputEl.value ? "bad" : ""));
  });
}

// =====================================================================
// Actions (file pickers, toggles, etc.)
// =====================================================================
async function addFiles(tab) {
  if (state.running) return;
  const files = await pywebview.api.pick_files();
  mergeFiles(tab, files || []);
}

async function addFolder(tab) {
  if (state.running) return;
  const files = await pywebview.api.pick_folder_with_videos();
  if (!files || files.length === 0) {
    appendLog("В папке нет видео-файлов", "info");
    return;
  }
  mergeFiles(tab, files);
}

function mergeFiles(tab, newFiles) {
  const max = tab === "concat" ? 10 : Infinity;
  const existing = new Set(state[tab].files.map(f => f.path));
  let added = 0;
  for (const f of newFiles) {
    if (state[tab].files.length >= max) {
      appendLog(`Лимит ${max} файлов`, "warn");
      break;
    }
    if (!existing.has(f.path)) {
      state[tab].files.push(f);
      added++;
    }
  }
  if (added) {
    appendLog(`Добавлено: ${added} файлов`, "success");
    // Если трим включён и длительность ещё не получена — подгрузим
    if (tab === "resize" && state.resize.trim_on && state.resize.trim_src_duration === 0) {
      const file = state.resize.files[0];
      if (file) {
        pywebview.api.get_file_info(file.path).then(info => {
          if (info && info.duration > 0) {
            state.resize.trim_src_duration = info.duration;
            if (state.resize.trim_end === null) {
              state.resize.trim_end = info.duration;
            }
            renderTrimCard();
          }
        });
      }
    }
  }
  renderFileList();
  updatePreview();
}

function removeFile(idx) {
  if (state.running) return;
  const tab = state.active_tab;
  state[tab].files.splice(idx, 1);
  renderFileList();
  updatePreview();
}

function moveFile(idx, delta) {
  if (state.running) return;
  const tab = state.active_tab;
  const newIdx = idx + delta;
  const arr = state[tab].files;
  if (newIdx < 0 || newIdx >= arr.length) return;
  [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
  renderFileList();
  updatePreview();
}

function clearFiles(tab) {
  if (state.running) return;
  state[tab].files = [];
  renderFileList();
  updatePreview();
}

function toggleChip(tab, res) {
  if (state.running) return;
  state[tab].resolutions[res] = !state[tab].resolutions[res];
  renderChips();
  updatePreview();
}

function setAllResolutions(tab, on) {
  if (state.running) return;
  for (const r of state.resolutions) state[tab].resolutions[r] = on;
  renderChips();
  updatePreview();
}

function setNamingMode(tab, mode) {
  if (state.running) return;
  state[tab].naming_mode = mode;
  renderModeTabs();
  updatePreview();
}

function togglePackshotOn() {
  if (state.running) return;
  state.resize.packshot_on = !state.resize.packshot_on;
  renderActiveTab();
}

async function pickPackshot(res) {
  const f = await pywebview.api.pick_one_video();
  if (f) {
    state.resize.packshots[res] = f;
    renderPackshotRows();
  }
}

function toggleOutdirSame(tab) {
  if (state.running) return;
  state[tab].outdir_same = !state[tab].outdir_same;
  renderActiveTab();
}

async function pickOutdir(tab) {
  const d = await pywebview.api.pick_folder();
  if (d) {
    state[tab].outdir = d;
    state[tab].outdir_same = false;
    renderActiveTab();
  }
}

function toggleSizeLimit(tab) {
  if (state.running) return;
  state[tab].size_limit_on = !state[tab].size_limit_on;
  renderActiveTab();
}

function toggleFade() {
  if (state.running) return;
  state.concat.fade = !state.concat.fade;
  renderActiveTab();
}

// =====================================================================
// Слоты загрузки (ресайз)
// =====================================================================
async function pickSlotFile(slotId) {
  if (state.running) return;
  const types = ("Видео (*.mp4;*.mov;*.m4v;*.mkv)", "Все файлы (*.*)");
  const files = await pywebview.api.pick_files();
  if (!files || files.length === 0) return;
  state.resize.slots[slotId].file = files[0];
  // Обновим длительность для ползунков тайминга
  if (state.resize.trim_on && state.resize.trim_src_duration === 0) {
    pywebview.api.get_file_info(files[0].path).then(info => {
      if (info && info.duration > 0) {
        state.resize.trim_src_duration = info.duration;
        if (state.resize.trim_end === null) state.resize.trim_end = info.duration;
        renderTrimCard();
      }
    });
  }
  renderActiveTab();
  updatePreview();
}

function removeSlotFile(slotId) {
  if (state.running) return;
  state.resize.slots[slotId].file = null;
  renderActiveTab();
  updatePreview();
}

function toggleSlotOutput(slotId, res) {
  if (state.running) return;
  const slot = state.resize.slots[slotId];
  slot.on[res] = !slot.on[res];
  renderActiveTab();
  updatePreview();
}

// =====================================================================
// Preview (через Python)
// =====================================================================
let previewTimer = null;
async function updatePreview() {
  if (state.active_tab === "subs") return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const tab = state.active_tab;
    if (tab === "subs") return;
    const s = state[tab];
    const resKeys = tab === "concat"
      ? [...new Set(Object.values(s.nodes).filter(n => n.file).map(n => n.resolution))]
      : SLOT_DEFS.flatMap(d => d.outputs.filter(r => s.slots[d.id].on[r]));
    const res = resKeys[0] || state.default_res;
    const params = {
      mode: s.naming_mode,
      team: s.team, type: s.type,
      name_source: s.name_source, name_manual: s.name_manual,
      template: s.template, tag: s.tag,
      res_key: res,
      duration_sec: 22,
    };
    if (tab === "concat") {
      const seq = getSequence();
      const withFiles = seq.filter(id => s.nodes[id] && s.nodes[id].file);
      params.all_stems = withFiles.length
        ? withFiles.map(id => s.nodes[id].file.stem)
        : ["ID0001_OS_In-House_Unreal_Roulette-Update_12-24_EN_30s_1080x1080",
           "ID0321_OS_Freelance_Unreal_Trafic-Control_12-24_EN_40s_1080x1920",
           "OS_Cinematic_Hook_Taxi-Patrol_05-24_EN_30s_1920x1080"];
      params.src_stem = params.all_stems[0];
      params.duration_sec = 100;
    } else {
      // resize: берём первый загруженный слот
      const firstFile = SLOT_DEFS.map(d => s.slots[d.id].file).find(f => f);
      params.src_stem = firstFile ? firstFile.stem
        : "ID0001_OS_In-House_Unreal_Roulette-Update_12-24_EN_22s_1080x1080";
      const firstDef = SLOT_DEFS.find(d => s.slots[d.id].file);
      if (firstDef) params.res_key = firstDef.outputs[0];
    }
    const name = await pywebview.api.preview_name(params);
    const lbl = document.getElementById("preview-" + tab);
    if (lbl) {
      let extra = "";
      if (tab === "resize") {
        const totalOut = SLOT_DEFS.reduce((sum, d) => {
          const slot = s.slots[d.id];
          if (!slot.file) return sum;
          return sum + d.outputs.filter(r => slot.on[r]).length;
        }, 0);
        if (totalOut > 1) extra = `    (всего ${totalOut} файлов)`;
      } else if (tab === "concat" && resKeys.length > 1) {
        extra = `    (× ${resKeys.length} разрешений)`;
      }
      lbl.textContent = `→ ${name}.mp4${extra}`;
    }
  }, 120);
}

// =====================================================================
// Тайминг ролика (ползунки)
// =====================================================================
function toggleTrimOn() {
  if (state.running) return;
  state.resize.trim_on = !state.resize.trim_on;
  if (state.resize.trim_on) {
    // При включении — загружаем длительность первого загруженного слота
    const file = SLOT_DEFS.map(d => state.resize.slots[d.id].file).find(f => f);
    if (file) {
      pywebview.api.get_file_info(file.path).then(info => {
        if (info && info.duration > 0) {
          state.resize.trim_src_duration = info.duration;
          if (state.resize.trim_end === null) {
            state.resize.trim_end = info.duration;
          }
        }
        renderTrimCard();
      });
    }
  }
  // Перерендерим только карточку + чекбокс без полного перестроения таба
  const lbl = document.querySelector('.check[onclick="toggleTrimOn()"]');
  if (lbl) lbl.className = `check ${state.resize.trim_on ? 'on' : ''}`;
  renderTrimCard();
}

function renderTrimCard() {
  const el = document.getElementById("trim-sliders");
  if (!el) return;
  if (!state.resize.trim_on) { el.innerHTML = ""; return; }
  el.innerHTML = buildTrimSlidersHTML();
}

function buildTrimSlidersHTML() {
  const dur = state.resize.trim_src_duration > 0 ? state.resize.trim_src_duration : 60;
  const start = Math.max(0, state.resize.trim_start);
  const end   = state.resize.trim_end !== null
                  ? Math.min(state.resize.trim_end, dur)
                  : dur;
  const clipDur = Math.max(0, end - start);
  const pct_s = dur > 0 ? (start / dur * 100) : 0;
  const pct_e = dur > 0 ? (end   / dur * 100) : 100;
  const pct_w = Math.max(0, pct_e - pct_s);

  const packNote = state.resize.packshot_on
    ? '<span class="pack-tag">+ пекшот в конец</span>' : '';
  const noFileNote = state.resize.trim_src_duration === 0
    ? '<span style="color:var(--warn);">↑ добавьте файл, чтобы увидеть реальную длину</span>' : '';

  return `
    <div style="margin-top:10px;">
      <div class="trim-timeline">
        <div class="trim-bar" style="left:${pct_s.toFixed(2)}%;width:${pct_w.toFixed(2)}%;"></div>
      </div>
      <div class="trim-slider-row">
        <span class="field-label">Начало</span>
        <input type="range" class="range-slider"
               min="0" max="${Math.max(0, end - 0.1).toFixed(1)}"
               step="0.1" value="${start.toFixed(1)}"
               oninput="onTrimStart(parseFloat(this.value))">
        <span class="trim-val">${start.toFixed(1)}s</span>
      </div>
      <div class="trim-slider-row">
        <span class="field-label">Конец</span>
        <input type="range" class="range-slider"
               min="${Math.min(dur, start + 0.1).toFixed(1)}" max="${dur.toFixed(1)}"
               step="0.1" value="${end.toFixed(1)}"
               oninput="onTrimEnd(parseFloat(this.value))">
        <span class="trim-val">${end.toFixed(1)}s</span>
      </div>
      <div class="trim-info">
        <span>Ролик: <b>${clipDur.toFixed(1)}s</b></span>
        <span>Источник: ${dur.toFixed(1)}s</span>
        ${packNote}
        ${noFileNote}
      </div>
    </div>
  `;
}

function onTrimStart(val) {
  state.resize.trim_start = val;
  const dur = state.resize.trim_src_duration || 60;
  const end = state.resize.trim_end !== null ? state.resize.trim_end : dur;
  if (val >= end - 0.1) {
    state.resize.trim_end = Math.min(val + 0.1, dur);
  }
  renderTrimCard();
}

function onTrimEnd(val) {
  state.resize.trim_end = val;
  if (val <= state.resize.trim_start + 0.1) {
    state.resize.trim_start = Math.max(0, val - 0.1);
  }
  renderTrimCard();
}

// =====================================================================
// Subtitle tab functions
// =====================================================================
function setSubsAnim(v) {
  state.subs.animation = v;
  renderSubsTabContent();
}

function setSubsPos(v) {
  state.subs.position = v;
  renderSubsTabContent();
}

function startTranscription() {
  const subs = state.subs;
  if (subs.transcribing || !subs.file) return;
  subs.transcribing = true;
  subs.progress_msg = "Запуск…";
  subs.progress_pct = 0;
  renderSubsTabContent();
  pywebview.api.start_transcribe({
    file_path: subs.file.path,
    language: subs.language,
    model: subs.model,
  }).then(r => {
    if (!r.ok) {
      subs.transcribing = false;
      renderSubsTabContent();
      appendLog("Субтитры: " + (r.error || "Ошибка"), "error");
    }
  });
}

function cancelTranscription() {
  state.subs.transcribing = false;
  pywebview.api.cancel_transcribe();
  renderSubsTabContent();
}

function deleteSubsSegment(i) {
  state.subs.segments.splice(i, 1);
  renderSubsTabContent();
}

function editSubsSegment(i, text) {
  if (state.subs.segments[i]) {
    state.subs.segments[i].text = text;
  }
}

function renderSubsTab() {
  const sb = state.subs;

  // libass-missing banner
  const libassBanner = sb.libass_missing ? `
    <div class="card" style="border-color:#e74c3c;background:rgba(231,76,60,.08);">
      <div class="card-body" style="display:flex;gap:10px;align-items:flex-start;">
        <span style="font-size:22px;line-height:1;">⚠️</span>
        <div>
          <div style="font-weight:700;color:#e74c3c;margin-bottom:4px;">
            FFmpeg без поддержки субтитров (нет libass)
          </div>
          <div style="font-size:12px;color:var(--text);margin-bottom:8px;">
            Откройте Терминал и выполните одну команду:
          </div>
          <code style="display:block;background:var(--bg);border:1px solid var(--border);
                       border-radius:4px;padding:6px 10px;font-size:12px;
                       user-select:all;cursor:text;">
            brew reinstall ffmpeg
          </code>
          <div style="font-size:11px;color:var(--text-dim);margin-top:6px;">
            После установки перезапустите приложение.
          </div>
        </div>
      </div>
    </div>` : "";

  // File section
  const fileSection = sb.file
    ? `<div class="file-row slot-loaded">
         <div class="file-row-num">&#10003;</div>
         <div class="file-row-name">${escapeHtml(sb.file.name)}</div>
         <button class="btn btn-icon btn-ghost btn-danger" onclick="removeSubsFile()">&#10005;</button>
       </div>`
    : `<button class="slot-pick-btn" onclick="pickSubsFile()">+ Выбрать видео для субтитров&hellip;</button>`;

  const anims = [
    { id: "none", icon: "T", label: "Нет" },
    { id: "fade", icon: "&#10022;", label: "Фейд" },
    { id: "pop", icon: "&#9733;", label: "Поп" },
    { id: "word", icon: "W", label: "По словам" },
    { id: "karaoke", icon: "&#9835;", label: "Karaoke" },
  ];

  const animGrid = `<div class="subs-anim-grid">` +
    anims.map(a => `<div class="subs-anim-card${sb.animation === a.id ? ' active' : ''}" onclick="setSubsAnim('${a.id}')">
      <div class="subs-anim-icon">${a.icon}</div><div>${a.label}</div>
    </div>`).join("") + `</div>`;

  const styleRow = `<div class="subs-style-row">
    <label style="font-size:11px;color:var(--text-dim);">Размер</label>
    <input type="number" class="input" style="width:60px;padding:3px 6px;font-size:11px;"
           value="${sb.font_size}" oninput="state.subs.font_size=parseInt(this.value)||52">
    <label style="font-size:11px;color:var(--text-dim);">Цвет</label>
    <input type="color" style="width:32px;height:26px;border:none;background:none;cursor:pointer;"
           value="${sb.color}" oninput="state.subs.color=this.value">
    <label style="font-size:11px;color:var(--text-dim);">Фон</label>
    <label class="check ${sb.bg ? 'on' : ''}" onclick="state.subs.bg=!state.subs.bg; renderSubsTabContent();">
      <span class="check-box"></span>
    </label>
  </div>`;

  const posRow = `<div class="subs-style-row">
    <span style="font-size:11px;color:var(--text-dim);">Позиция:</span>
    ${["bottom","center","top"].map(p => {
      const labels = {bottom:"Низ",center:"Центр",top:"Верх"};
      return `<button class="subs-pos-btn${sb.position===p?' active':''}" onclick="setSubsPos('${p}')">${labels[p]}</button>`;
    }).join("")}
  </div>`;

  const langOpts = ["auto","ru","en","de","fr","es","it","ja","zh"]
    .map(v => { const l={auto:"Авто",ru:"Русский",en:"English",de:"Deutsch",fr:"Français",es:"Español",it:"Italiano",ja:"日本語",zh:"中文"}[v];
      return `<option value="${v}"${sb.language===v?" selected":""}>${l}</option>`; }).join("");
  const modelOpts = [{v:"tiny",l:"Tiny (быстро)"},{v:"base",l:"Base"},{v:"small",l:"Small"}]
    .map(o => `<option value="${o.v}"${sb.model===o.v?" selected":""}>${o.l}</option>`).join("");

  const selRow = `<div class="subs-style-row" style="margin-top:6px;">
    <label style="font-size:11px;color:var(--text-dim);">Язык</label>
    <select class="input" style="padding:3px 6px;font-size:11px;" onchange="state.subs.language=this.value">${langOpts}</select>
    <label style="font-size:11px;color:var(--text-dim);">Модель</label>
    <select class="input" style="padding:3px 6px;font-size:11px;" onchange="state.subs.model=this.value">${modelOpts}</select>
  </div>`;

  let progressHTML = "";
  if (sb.transcribing) {
    progressHTML = `
      <div id="subs-progress-msg" style="font-size:11px;color:var(--text-dim);margin-top:4px;">${escapeHtml(sb.progress_msg)}</div>
      <div class="subs-progress-bar"><div class="subs-progress-fill" id="subs-progress-fill" style="width:${sb.progress_pct}%;"></div></div>`;
  }

  const transcribeBtn = !sb.file
    ? `<button class="btn" disabled style="margin-top:6px;opacity:0.4;">&#9654; Распознать субтитры</button>`
    : sb.transcribing
      ? `<button class="btn" style="margin-top:6px;" onclick="cancelTranscription()">&#9632; Отмена</button>`
      : `<button class="btn btn-accent" style="margin-top:6px;" onclick="startTranscription()">&#9654; Распознать субтитры</button>`;

  let segList = "";
  if (sb.segments.length > 0) {
    const rows = sb.segments.map((seg, i) => {
      const start = parseFloat(seg.start || 0);
      const timeStr = Math.floor(start/60) + ":" + String(Math.floor(start%60)).padStart(2,"0");
      return `<div class="subs-segment">
        <span class="subs-seg-time">${timeStr}</span>
        <textarea class="subs-seg-text" rows="2" oninput="editSubsSegment(${i},this.value)">${escapeHtml(seg.text||"")}</textarea>
        <span class="subs-seg-del" onclick="deleteSubsSegment(${i})">&#10005;</span>
      </div>`;
    }).join("");
    segList = `<div style="max-height:280px;overflow-y:auto;margin-top:8px;">${rows}</div>
      <div style="font-size:10px;color:var(--text-dim);margin-top:4px;">${sb.segments.length} сегментов</div>`;
  }

  const burnBtn = sb.segments.length > 0 && sb.file
    ? `<button class="btn btn-accent" style="width:100%;padding:12px;font-size:14px;font-weight:700;margin-top:4px;background:var(--success);box-shadow:0 4px 12px rgba(52,211,153,0.3);" onclick="startSubtitleBurn()">&#9654; ВЖЕЧЬ СУБТИТРЫ</button>`
    : `<button class="btn" disabled style="width:100%;padding:12px;font-size:14px;font-weight:700;margin-top:4px;opacity:0.4;">&#9654; ВЖЕЧЬ СУБТИТРЫ</button>`;

  return libassBanner + `
    <div class="card">
      <div class="card-head">
        <span class="card-title">Исходное видео</span>
        <span class="card-sub">файл для субтитрования</span>
      </div>
      <div class="card-body">${fileSection}</div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Стиль анимации</span></div>
      <div class="card-body">${animGrid}${styleRow}${posRow}</div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Распознавание речи</span>
        <span class="card-sub">Whisper &middot; локально</span></div>
      <div class="card-body">${selRow}${progressHTML}${transcribeBtn}</div>
    </div>

    ${sb.segments.length > 0 ? `
    <div class="card">
      <div class="card-head"><span class="card-title">Субтитры</span>
        <span class="card-sub">${sb.segments.length} фраз</span></div>
      <div class="card-body" id="subs-seg-list">${segList}</div>
    </div>` : ""}

    <div class="card">
      <div class="card-head"><span class="card-title">Куда сохранять</span></div>
      <div class="card-body">
        <label class="check ${sb.outdir_same ? 'on' : ''}" onclick="state.subs.outdir_same=!state.subs.outdir_same; renderSubsTabContent();">
          <span class="check-box"></span>
          Рядом с исходником
        </label>
        <div class="field-row" style="grid-template-columns:1fr auto;margin-top:6px;">
          <input class="input mono" id="outdir-subs" placeholder="&hellip;или папка вывода"
                 value="${escapeHtml(sb.outdir)}"
                 ${sb.outdir_same ? "disabled" : ""}
                 oninput="state.subs.outdir=this.value">
          <button class="btn" onclick="pickSubsOutdir()">Обзор&hellip;</button>
        </div>
      </div>
    </div>

    <div style="padding:0 0 16px;">
      ${burnBtn}
    </div>
  `;
}

function renderSubsTabContent() {
  if (state.active_tab !== "subs") return;
  document.getElementById("content").innerHTML = renderSubsTab();
}

async function pickSubsFile() {
  const files = await pywebview.api.pick_files();
  if (!files || !files.length) return;
  const f = files[0];
  state.subs.file = f;
  state.subs.segments = [];
  renderSubsTabContent();
}

function removeSubsFile() {
  state.subs.file = null;
  state.subs.segments = [];
  renderSubsTabContent();
}

async function pickSubsOutdir() {
  const dir = await pywebview.api.pick_folder();
  if (dir) { state.subs.outdir = dir; renderSubsTabContent(); }
}

async function startSubtitleBurn() {
  if (state.running) return;
  const sb = state.subs;
  if (!sb.file || !sb.segments.length) return;
  setBar("bar-overall", 0); setBar("bar-current", 0);
  document.getElementById("progress-pct").textContent = "0 %";
  document.getElementById("progress-title").textContent = "Вжигаем субтитры…";
  document.getElementById("current-name").textContent = "—";
  const params = {
    src_path: sb.file.path,
    segments: sb.segments,
    animation: sb.animation, font_size: sb.font_size,
    color: sb.color, position: sb.position, bg: sb.bg,
    outdir: sb.outdir, outdir_same: sb.outdir_same,
  };
  const res = await pywebview.api.start_subtitle_burn(params);
  if (!res.ok) { alert(res.error || "Ошибка запуска"); return; }
  setRunning(true);
  // Show the progress area (it was hidden for subs tab)
  const actions = document.querySelector(".actions");
  if (actions) actions.style.display = "";
}

function renderResizeTabFull() {
  if (state.active_tab === "resize") renderActiveTab();
}

// =====================================================================
// Start / Stop
// =====================================================================
async function startProcessing() {
  if (state.running) return;
  const tab = state.active_tab;
  if (tab === "subs") { startSubtitleBurn(); return; }
  const s = state[tab];

  // Валидация
  if (tab === "resize") {
    const hasFile = SLOT_DEFS.some(d => s.slots[d.id].file);
    if (!hasFile) { alert("Загрузите хотя бы одно видео"); return; }
  } else {
    const seq = getSequence();
    const withFiles = seq.filter(id => s.nodes[id] && s.nodes[id].file);
    if (withFiles.length < 2) {
      alert("Добавьте минимум 2 клипа с загруженными видео");
      return;
    }
  }

  setBar("bar-overall", 0); setBar("bar-current", 0);
  document.getElementById("progress-pct").textContent = "0 %";
  document.getElementById("progress-title").textContent = "Запуск…";
  document.getElementById("current-name").textContent = "—";

  const params = {
    outdir: s.outdir,
    outdir_same: s.outdir_same,
    naming_mode: s.naming_mode,
    team: s.team, type: s.type,
    name_source: s.name_source, name_manual: s.name_manual,
    template: s.template, tag: s.tag,
    size_limit_on: s.size_limit_on,
  };

  let res;
  if (tab === "resize") {
    // Собираем jobs из слотов
    const jobs = [];
    for (const def of SLOT_DEFS) {
      const slot = s.slots[def.id];
      if (!slot.file) continue;
      const ress = def.outputs.filter(r => slot.on[r]);
      if (ress.length > 0) jobs.push({ file: slot.file.path, resolutions: ress });
    }
    if (jobs.length === 0) { alert("Нет файлов с выбранными разрешениями"); return; }
    const totalOut = jobs.reduce((n, j) => n + j.resolutions.length, 0);
    params.jobs = jobs;
    params.packshot_on = s.packshot_on;
    params.packshots = s.packshots;
    if (s.trim_on) { params.trim_start = s.trim_start; params.trim_end = s.trim_end; }
    const trimNote = s.trim_on
      ? ` · трим ${s.trim_start.toFixed(1)}–${(s.trim_end !== null ? s.trim_end : s.trim_src_duration).toFixed(1)}s`
      : "";
    appendLog(`Старт ресайза: ${jobs.length} исходник(ов) → ${totalOut} файлов${s.packshot_on ? ' (+ пекшот)' : ''}${trimNote}`, "info");
    res = await pywebview.api.start_resize(params);
  } else {
    // CONCAT — group nodes by resolution → build jobs for ChainedConcatWorker
    const seq = getSequence();
    const withFiles = seq.filter(id => s.nodes[id] && s.nodes[id].file);

    // Build per-resolution job map (preserving sequence order within each group)
    const jobMap = {};
    for (const id of withFiles) {
      const node = s.nodes[id];
      const res = node.resolution || "1920x1080";
      if (!jobMap[res]) jobMap[res] = { files: [], trims: [], resolution: res };
      jobMap[res].files.push(node.file.path);
      jobMap[res].trims.push({ start: node.trim_start, end: node.trim_end });
    }
    const jobs = Object.values(jobMap).filter(j => j.files.length >= 2);
    if (jobs.length === 0) {
      alert("Нет групп с минимум 2 клипами одного разрешения.\nДобавьте ноды с одинаковым разрешением.");
      return;
    }
    params.jobs = jobs;
    params.fade = s.fade;
    const totalRes = jobs.length;
    appendLog(`Старт склейки: ${withFiles.length} клипов → ${totalRes} разрешени${totalRes === 1 ? 'е' : 'й'}${s.fade ? ', с фейдом' : ''}`, "info");
    res = await pywebview.api.start_concat(params);
  }
  if (!res.ok) {
    alert(res.error || "Ошибка запуска");
    return;
  }
  setRunning(true);
}

async function stopProcessing() {
  await pywebview.api.stop();
  document.getElementById("btn-stop").textContent = "Останавливаю…";
  document.getElementById("btn-stop").disabled = true;
}

function setRunning(running) {
  state.running = running;
  document.getElementById("btn-start").disabled = running;
  document.getElementById("btn-start").textContent = running ? "Обработка…" : "▶ СТАРТ";
  document.getElementById("btn-stop").disabled = !running;
  document.getElementById("btn-stop").textContent = "■ Стоп";
  if (!running && state.active_tab === "subs") {
    const actions = document.querySelector(".actions");
    if (actions) actions.style.display = "none";
  }
}

// =====================================================================
// Concat canvas: global drag listeners — mounted ONCE, never duplicated
// =====================================================================
document.addEventListener("mousemove", onCnMove);
document.addEventListener("mouseup",   onCnUp);

// =====================================================================
// Concat: Clone sequence to all resolutions
// =====================================================================
function cloneSeqToAllResolutions() {
  if (state.running) return;

  const seq       = getSequence();
  const s         = state.concat;
  const withFiles = seq.filter(id => s.nodes[id] && s.nodes[id].file);

  if (withFiles.length < 1) {
    alert("Сначала добавьте хотя бы один клип с файлом.");
    return;
  }

  // ── Determine source resolution (most common in sequence) ─────────
  const resCounts = {};
  for (const id of withFiles) {
    const r = s.nodes[id].resolution || RES_ORDER[0];
    resCounts[r] = (resCounts[r] || 0) + 1;
  }
  const srcRes = Object.entries(resCounts).sort((a, b) => b[1] - a[1])[0][0];
  const srcRow = withFiles.filter(id => s.nodes[id].resolution === srcRes);
  if (srcRow.length < 1) return;

  // ── Snapshot source data BEFORE any mutations ─────────────────────
  // We capture all values as plain numbers (never null/undefined) so that
  // each cloned format row gets identical, isolated copies regardless of
  // how many iterations the outer loop runs.
  const srcSnap = srcRow.map(id => {
    const n   = s.nodes[id];
    const dur = n.trim_dur > 0 ? n.trim_dur : 0;
    return {
      file:       n.file,                                    // object ref (read-only in clones)
      video_url:  n.video_url || null,
      trim_dur:   dur,
      trim_start: typeof n.trim_start === "number" ? n.trim_start : 0,
      // resolve null → full duration so clones always carry an explicit number
      trim_end:   (n.trim_end !== null && n.trim_end !== undefined)
                    ? n.trim_end
                    : dur,
      x:          n.x,
    };
  });

  // ── Measure current max bottom edge (before we add anything) ──────
  let maxBottom = 0;
  for (const id of Object.keys(s.nodes)) {
    const n  = s.nodes[id];
    const el = document.getElementById(`cn-node-${id}`);
    const nh = el ? el.offsetHeight : 370;
    maxBottom = Math.max(maxBottom, n.y + nh);
  }

  // ── Clone to every other resolution ───────────────────────────────
  const targets = RES_ORDER.filter(r => r !== srcRes);
  const ROW_GAP = 24;
  let   created = 0;
  const createdLabels = [];

  for (const targetRes of targets) {
    // Skip if this resolution already has enough nodes with files
    const existing = Object.values(s.nodes).filter(
      n => n.resolution === targetRes && n.file
    );
    if (existing.length >= srcSnap.length) continue;

    const rowY  = maxBottom + ROW_GAP;
    maxBottom   = rowY + 390;          // reserve height for next row

    // Build new nodes from snapshot (not from live s.nodes)
    const newIds = [];
    for (const snap of srcSnap) {
      const newId = s.nextId++;
      s.nodes[newId] = {
        file:       snap.file,
        video_url:  snap.video_url,
        trim_dur:   snap.trim_dur,
        trim_start: snap.trim_start,
        trim_end:   snap.trim_end,     // always a number now
        x:          snap.x,
        y:          rowY,
        resolution: targetRes,
      };
      newIds.push(newId);
    }

    // Wire edges in the same order as source row
    for (let i = 0; i < newIds.length - 1; i++) {
      s.edges.push({ from: newIds[i], to: newIds[i + 1] });
    }

    created++;
    createdLabels.push(RES_LABELS[targetRes] || targetRes);
  }

  if (created === 0) {
    alert("Все форматы уже присутствуют в нод-графе.");
    return;
  }

  renderActiveTab();
  appendLog(`Клонировано на ${created} форматов: ${createdLabels.join(", ")}`, "success");
}

// Init on pywebview ready
window.addEventListener("pywebviewready", init);

// =====================================================================
// Sound Tab
// =====================================================================
const SND_COLORS = ["#4f8cf7","#a78bfa","#44d39a","#f97316","#f87171","#fbbf24","#34d399"];

function sndFmtTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return m > 0 ? `${m}:${String(s).padStart(2,"0")}` : `${s}s`;
}

function renderSoundTimeline() {
  const s   = state.sound;
  const dur  = s.duration;
  if (!dur) return "";

  // ruler step
  const step = dur > 300 ? 60 : dur > 120 ? 30 : dur > 60 ? 15 : dur > 30 ? 10 : dur > 10 ? 5 : 2;
  let markers = "";
  for (let t = 0; t <= dur; t += step) {
    const pct = (t / dur * 100).toFixed(2);
    markers += `<div class="snd-marker" style="left:${pct}%">
      <div class="snd-marker-tick"></div>
      <div class="snd-marker-label">${sndFmtTime(t)}</div>
    </div>`;
  }

  // thumbnail strip inside video bar
  const thumbStrip = s.thumbs && s.thumbs.length
    ? `<div style="display:flex;height:100%;position:absolute;inset:0;border-radius:6px;overflow:hidden;opacity:0.75;">
        ${s.thumbs.map(th => th
          ? `<img src="${th}" style="flex:1;object-fit:cover;min-width:0;height:100%;">`
          : `<div style="flex:1;background:var(--bg-elev);"></div>`
        ).join("")}
       </div>`
    : "";

  // track blocks
  const blocks = s.tracks.map((tr, i) => {
    const left  = Math.max(0, Math.min(100, tr.start / dur * 100)).toFixed(2);
    const width = Math.max(0.5, Math.min(100 - parseFloat(left), tr.duration / dur * 100)).toFixed(2);
    const color = SND_COLORS[i % SND_COLORS.length];
    const nm    = tr.name.length > 16 ? tr.name.substring(0, 16) + "…" : tr.name;
    return `<div class="snd-track-block"
       style="left:${left}%;width:${width}%;background:${color}28;border-color:${color}"
       title="${escapeHtml(tr.name)} | старт ${tr.start.toFixed(2)}s, длина ${tr.duration.toFixed(2)}s">
       <span class="snd-block-label" style="color:${color}">${escapeHtml(nm)}</span>
     </div>`;
  }).join("");

  const curPct = Math.max(0, Math.min(100, ((s.cursor || 0) / dur * 100))).toFixed(2);

  return `
    <div id="snd-timeline" class="snd-timeline" onclick="setSoundCursor(event)">
      <div class="snd-ruler">${markers}</div>
      <div class="snd-video-bar" style="position:relative;">
        ${thumbStrip}
        <span style="position:relative;z-index:2;font-size:9px;padding:0 8px;
                     color:#fff;text-shadow:0 1px 3px #0008;">
          ${escapeHtml(s.file.name.length > 40 ? s.file.name.substring(0,40)+"…" : s.file.name)}
          &nbsp;·&nbsp; ${sndFmtTime(dur)}
        </span>
      </div>
      <div class="snd-tracks-bar">${blocks}</div>
      <div class="snd-cursor" id="snd-cursor" style="left:${curPct}%"></div>
    </div>
    <div style="font-size:11px;color:var(--text-dim);margin-top:4px;" id="snd-cursor-label">
      Курсор: <b style="color:#ffb02e">${(s.cursor||0).toFixed(2)}s</b>
      &nbsp;·&nbsp; кликните по таймлайну для позиции
    </div>`;
}

function renderSoundTab() {
  const s = state.sound;

  // ── File picker ──────────────────────────────────────────────────
  const fileRow = s.file
    ? `<div class="file-row slot-loaded">
         <div class="file-row-num">&#10003;</div>
         <div class="file-row-name">${escapeHtml(s.file.name)}</div>
         <span style="font-size:11px;color:var(--text-dim);margin:0 8px;">${sndFmtTime(s.duration)}</span>
         <button class="btn btn-icon btn-ghost btn-danger" onclick="removeSoundVideo()">&#10005;</button>
       </div>`
    : `<button class="slot-pick-btn" onclick="pickSoundVideo()">+ Выбрать видео&hellip;</button>`;

  // ── Track list ───────────────────────────────────────────────────
  const trackRows = s.tracks.length === 0
    ? `<div style="font-size:12px;color:var(--text-dim);padding:4px 0;">
         Нет треков. Добавьте файл или сгенерируйте голос ниже.
       </div>`
    : s.tracks.map((tr, i) => {
        const color = SND_COLORS[i % SND_COLORS.length];
        return `<div class="track-row">
          <div class="track-dot" style="background:${color}"></div>
          <input class="input" type="number" min="0" step="0.1"
                 value="${tr.start.toFixed(1)}"
                 oninput="updateTrackStart(${tr.id},+this.value)"
                 style="width:62px;padding:3px 6px;font-size:12px;flex-shrink:0;">
          <div class="track-name" title="${escapeHtml(tr.file)}">${escapeHtml(tr.name)}</div>
          <div class="track-dur">${tr.duration.toFixed(1)}s</div>
          <button class="btn btn-icon btn-ghost" onclick="sndPlayAudio('${tr.file.replace(/'/g,"\\'")}',${i})" title="Прослушать">&#9654;</button>
          <button class="btn btn-icon btn-ghost btn-danger" onclick="removeTrack(${tr.id})">&#10005;</button>
        </div>`;
      }).join("");

  // ── TTS generator ────────────────────────────────────────────────
  const langBtns = ["ru","en"].map(l =>
    `<button class="mode-tab${s.tts_lang===l?' active':''}" onclick="setSoundTTSLang('${l}')">${l==='ru'?'Русский':'English'}</button>`
  ).join("");
  const genderBtns = ["female","male"].map(g => {
    const ico = g==="female" ? "&#9792; Женский" : "&#9794; Мужской";
    return `<button class="voice-gender-btn${s.tts_gender===g?' active':''}" onclick="setSoundTTSGender('${g}')">${ico}</button>`;
  }).join("");
  const ttsProgressHTML = s.tts_generating ? `
    <div id="tts-prog-msg" style="font-size:11px;color:var(--text-dim);margin-top:6px;">${escapeHtml(s.tts_progress||"…")}</div>
    <div class="tts-progress"><div class="tts-progress-fill" id="tts-prog-fill" style="width:${s.tts_pct}%"></div></div>
  ` : "";

  // ── TTS Library ──────────────────────────────────────────────────
  const libRows = s.tts_library.length === 0
    ? `<div style="font-size:12px;color:var(--text-dim);padding:4px 0;">Голоса появятся здесь после генерации.</div>`
    : [...s.tts_library].reverse().map((item, ri) => {
        const langFlag = item.lang === "ru" ? "RU" : "EN";
        const genderIco = item.gender === "female" ? "♀" : "♂";
        const idx = s.tts_library.length - 1 - ri;
        return `<div class="track-row" style="gap:6px;">
          <div style="font-size:10px;color:var(--text-dim);min-width:28px;text-align:center;
                      background:var(--bg-elev);border-radius:4px;padding:2px 4px;
                      border:1px solid var(--border-subtle);">${langFlag}<br>${genderIco}</div>
          <div class="track-name" style="flex:1;" title="${escapeHtml(item.file)}">${escapeHtml(item.label)}</div>
          <div class="track-dur">${item.dur.toFixed(1)}s</div>
          <button class="btn btn-icon btn-ghost" onclick="sndPlayLibItem(${idx})" title="Прослушать">&#9654;</button>
          ${s.file
            ? `<button class="btn btn-icon btn-ghost" style="color:var(--accent)"
                       onclick="addLibItemToTimeline(${idx})" title="На ${(s.cursor||0).toFixed(1)}s">+TL</button>`
            : ""}
          <button class="btn btn-icon btn-ghost btn-danger" onclick="removeLibItem(${idx})">&#10005;</button>
        </div>`;
      }).join("");

  // ── Mix progress (shown while mixing) ───────────────────────────
  const mixBlock = s.mixing ? `
    <div class="card" style="border-color:var(--violet)30;">
      <div class="card-head"><span class="card-title" style="color:var(--violet);">&#9654; Микширование…</span></div>
      <div class="card-body">
        <div id="mix-status-txt" style="font-size:11px;color:var(--text-dim);margin-bottom:6px;">${escapeHtml(s.mix_status)}</div>
        <div class="tts-progress" style="background:var(--bg);">
          <div class="tts-progress-fill" id="mix-prog-fill" style="width:${s.mix_progress}%;background:var(--violet);"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;">
          <span id="mix-prog-pct" style="font-size:11px;color:var(--text-dim);">${s.mix_progress}%</span>
          <button class="btn btn-danger" style="padding:2px 10px;font-size:11px;"
                  onclick="pywebview.api.cancel_sound_mix()">Стоп</button>
        </div>
      </div>
    </div>` : "";

  const mixDoneBlock = (!s.mixing && s.mix_last_dst) ? `
    <div style="font-size:11px;color:var(--success);padding:6px 0;">
      &#10003; Сохранено: ${escapeHtml(s.mix_last_dst)}
    </div>` : "";

  // ── Audio mode ───────────────────────────────────────────────────
  const audioModeBtns = [
    {id:"mix",     label:"&#127925; Смикшировать"},
    {id:"replace", label:"&#128263; Заменить звук"},
  ].map(m =>
    `<button class="audio-mode-btn${s.original_audio===m.id?' active':''}" onclick="setSoundAudioMode('${m.id}')">${m.label}</button>`
  ).join("");

  const canMix = !!(s.file && s.tracks.length > 0 && !s.mixing);

  return `
    <div class="card">
      <div class="card-head">
        <span class="card-title">Исходное видео</span>
        <span class="card-sub">к нему будет добавлен звук</span>
      </div>
      <div class="card-body">${fileRow}</div>
    </div>

    ${s.file && s.duration > 0 ? `
    <div class="card">
      <div class="card-head">
        <span class="card-title">Таймлайн</span>
        <span class="card-sub">кликните для позиции курсора</span>
      </div>
      <div class="card-body" style="padding:8px 12px 10px;">${renderSoundTimeline()}</div>
    </div>` : ""}

    <div class="card">
      <div class="card-head">
        <span class="card-title">Войсоверы</span>
        ${s.tracks.length ? `<span class="card-sub">${s.tracks.length} трек(ов)</span>` : ""}
      </div>
      <div class="card-body">
        ${trackRows}
        <button class="btn" style="margin-top:8px;" onclick="addVoiceFromFile()">+ Добавить из файла&hellip;</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Генератор голоса</span>
        <span class="card-sub">Edge TTS (онлайн) &rarr; macOS say (офлайн)</span>
      </div>
      <div class="card-body">
        <textarea class="input" id="tts-text-area"
                  style="width:100%;height:80px;resize:vertical;font-size:13px;padding:8px;"
                  placeholder="Введите текст для озвучки…"
                  oninput="state.sound.tts_text=this.value">${escapeHtml(s.tts_text)}</textarea>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap;">
          <div class="mode-tabs" style="margin:0;">${langBtns}</div>
          <div style="flex:1;"></div>
          <span style="font-size:11px;color:var(--text-dim);">Скорость</span>
          <input type="range" min="0.5" max="2" step="0.1" value="${s.tts_speed}"
                 style="width:72px;"
                 oninput="state.sound.tts_speed=+this.value;document.getElementById('tts-spd-lbl').textContent=this.value+'x'">
          <span id="tts-spd-lbl" style="font-size:11px;min-width:32px;">${s.tts_speed}x</span>
        </div>
        <div class="voice-gender-row" style="margin-top:8px;">${genderBtns}</div>
        <div style="margin-top:10px;">
          <button class="btn btn-accent" style="width:100%;"
                  onclick="startTTSGenerate()"
                  ${s.tts_generating ? "disabled" : ""}>
            ${s.tts_generating ? "&#9203; Генерация…" : "&#127908; Сгенерировать голос"}
          </button>
        </div>
        ${ttsProgressHTML}
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Библиотека голосов</span>
        ${s.tts_library.length ? `<span class="card-sub">${s.tts_library.length} файл(ов)</span>` : ""}
      </div>
      <div class="card-body">${libRows}</div>
    </div>

    ${s.file ? `
    <div class="card">
      <div class="card-head"><span class="card-title">Исходный звук видео</span></div>
      <div class="card-body">
        <div class="audio-mode-row">${audioModeBtns}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">Куда сохранять</span></div>
      <div class="card-body">
        <label class="check ${s.outdir_same?'on':''}"
               onclick="state.sound.outdir_same=!state.sound.outdir_same;renderSoundTabContent()">
          <span class="check-box"></span> Рядом с исходником
        </label>
        <div class="field-row" style="grid-template-columns:1fr auto;margin-top:6px;">
          <input class="input mono" placeholder="…или папка" value="${escapeHtml(s.outdir)}"
                 ${s.outdir_same?"disabled":""} oninput="state.sound.outdir=this.value">
          <button class="btn" onclick="pickSoundOutdir()">Обзор&hellip;</button>
        </div>
      </div>
    </div>

    ${mixBlock}

    <div style="padding:0 0 16px;">
      ${mixDoneBlock}
      <button class="btn${canMix?' btn-accent':''}"
              style="width:100%;padding:12px;font-size:14px;font-weight:700;
                     ${canMix?'background:var(--violet);box-shadow:0 4px 12px rgba(167,139,250,0.3);':'opacity:0.4;'}"
              onclick="startSoundMix()" ${canMix?'':'disabled'}>
        &#9654; СМИКШИРОВАТЬ И СОХРАНИТЬ
      </button>
    </div>` : ""}
  `;
}

function renderSoundTabContent() {
  if (state.active_tab !== "sound") return;
  document.getElementById("content").innerHTML = renderSoundTab();
}

// =====================================================================
// ClipOpus tab
// =====================================================================
function renderClipOpusTab() {
  const co   = state.clipopus;
  const busy = co.processing;

  // Слоты — 3 карточки с URL + разрешениями
  const slotCards = co.slots.map((slot, i) => {
    const hasUrl   = slot.url.trim().length > 0;
    const resChips = ["1080x1080","1080x1350","1080x1920","1920x1080"].map(r => {
      const on = slot.resolutions[r] ? " on" : "";
      return `<span class="co-res-chip${on}" onclick="coToggleRes(${i},'${r}')">${r}</span>`;
    }).join("");

    // Если есть загруженные клипы — показываем select
    let clipSelect = "";
    if (co.clips.length > 0) {
      const opts = co.clips.map((c, ci) => {
        const sel = slot.clip_idx === ci ? " selected" : "";
        const lbl = escapeHtml(c.title || c.id || `Клип ${ci+1}`);
        return `<option value="${ci}"${sel}>${lbl}</option>`;
      }).join("");
      clipSelect = `
        <select class="co-select" onchange="coSelectClip(${i},this.value)">
          <option value="">— выбрать клип из загруженных —</option>
          ${opts}
        </select>`;
    }

    return `
      <div class="co-slot${hasUrl ? ' loaded' : ''}">
        <div class="co-slot-head">
          <div class="co-slot-num">${i + 1}</div>
          <div class="co-slot-title">${hasUrl ? escapeHtml(slot.title || slot.url.split("/").pop() || "Клип") : "Пустой слот"}</div>
        </div>
        ${clipSelect}
        <input class="input co-url-input" type="text" placeholder="URL клипа (прямая ссылка на .mp4)"
               value="${escapeHtml(slot.url)}"
               onchange="coSetUrl(${i},this.value)" ${busy ? "disabled" : ""}/>
        <div class="co-res-row">${resChips}</div>
      </div>`;
  }).join("");

  // Статус загрузки
  const statusDisplay = co.status_msg ? "visible" : "";
  const statusText    = escapeHtml(co.status_msg);

  // Папка вывода
  const outRow = `
    <div class="co-outdir-row">
      <label class="toggle-wrap" title="Сохранять рядом с исходником">
        <input type="checkbox" ${co.outdir_same ? "checked" : ""}
               onchange="state.clipopus.outdir_same=this.checked">
        <span class="toggle-track"></span>
      </label>
      <span class="field-label" style="font-size:.8rem;margin:0 6px">Рядом с источником</span>
      <input class="input co-outdir-row" type="text"
             style="flex:1" placeholder="Папка вывода…"
             value="${escapeHtml(co.outdir)}"
             onchange="state.clipopus.outdir=this.value"
             ${co.outdir_same ? "disabled" : ""} />
      <button class="btn btn-ghost" onclick="coPickOutdir()" ${busy ? "disabled" : ""}>📂</button>
    </div>`;

  // Кнопки действий
  const fetchDisabled = busy || co.loading || !co.api_key.trim() || !co.project_id.trim() ? "disabled" : "";
  const startDisabled = busy ? "disabled" : "";
  const startLabel    = busy ? "⏳ Обработка…" : "▶ Скачать и ресайзнуть";

  return `
    <div style="display:flex;flex-direction:column;gap:0">

      <!-- API Key -->
      <div class="card" style="margin-bottom:12px">
        <div class="card-head"><span class="card-title">OpusClip API</span></div>
        <div class="co-key-row">
          <input class="input" type="password" placeholder="API ключ из clip.opus.pro → Settings → API"
                 value="${escapeHtml(co.api_key)}"
                 onchange="state.clipopus.api_key=this.value" ${busy ? "disabled" : ""}/>
          <button class="co-btn-fetch" onclick="coSaveKey()" ${busy ? "disabled" : ""}>Сохранить</button>
        </div>
        <div class="co-fetch-row">
          <input class="input" type="text" placeholder="Project ID"
                 value="${escapeHtml(co.project_id)}"
                 onchange="state.clipopus.project_id=this.value" ${busy ? "disabled" : ""}/>
          <button class="co-btn-fetch" onclick="coFetchClips()" ${fetchDisabled}>
            ${co.loading ? "⏳ Загрузка…" : "🔍 Загрузить клипы"}
          </button>
        </div>
        ${co.clips.length > 0
          ? `<div style="font-size:.78rem;color:#a855f7;margin-top:-6px">
               ✓ Загружено клипов: ${co.clips.length} — выберите нужные в слотах
             </div>`
          : ""}
      </div>

      <!-- Статус -->
      <div id="co-status-bar" class="co-status-bar ${statusDisplay}">${statusText}</div>

      <!-- Слоты -->
      <div class="co-clips-grid">${slotCards}</div>

      <!-- Папка вывода -->
      <div class="card" style="margin-bottom:12px">
        <div class="card-head"><span class="card-title">Папка вывода</span></div>
        ${outRow}
      </div>

      <!-- Кнопки старт/стоп -->
      <div style="display:flex;gap:10px">
        <button class="btn-start" style="background:linear-gradient(135deg,#a855f7,#7c3aed);flex:1"
                onclick="coStartResize()" ${startDisabled}>${startLabel}</button>
        ${busy
          ? `<button class="btn-stop" onclick="coStop()" style="flex:0 0 auto">■ Стоп</button>`
          : ""}
      </div>
    </div>`;
}

async function coSaveKey() {
  const co = state.clipopus;
  await pywebview.api.save_clipopus_key(co.api_key);
  appendLog("ClipOpus: API ключ сохранён", "success");
}

async function coFetchClips() {
  const co = state.clipopus;
  if (!co.api_key.trim()) { appendLog("ClipOpus: введите API ключ", "error"); return; }
  if (!co.project_id.trim()) { appendLog("ClipOpus: введите Project ID", "error"); return; }
  co.loading = true;
  renderActiveTab();
  const r = await pywebview.api.fetch_clipopus_clips(co.api_key, co.project_id);
  co.loading = false;
  if (!r.ok) {
    appendLog("ClipOpus: " + r.error, "error");
    renderActiveTab();
    return;
  }
  co.clips = r.clips || [];
  appendLog(`ClipOpus: загружено ${co.clips.length} клипов`, "success");
  // Автозаполняем слоты первыми 3 клипами
  co.clips.slice(0, 3).forEach((clip, i) => {
    const url   = clip.stream_url || clip.download_url || clip.url || "";
    const title = clip.title || clip.name || `Клип ${i + 1}`;
    co.slots[i].url       = url;
    co.slots[i].title     = title;
    co.slots[i].clip_idx  = i;
  });
  renderActiveTab();
}

function coSelectClip(slotIdx, clipIdxStr) {
  const co      = state.clipopus;
  const clipIdx = parseInt(clipIdxStr, 10);
  if (isNaN(clipIdx) || clipIdx < 0 || clipIdx >= co.clips.length) {
    co.slots[slotIdx].url      = "";
    co.slots[slotIdx].title    = "";
    co.slots[slotIdx].clip_idx = null;
  } else {
    const clip = co.clips[clipIdx];
    co.slots[slotIdx].url      = clip.stream_url || clip.download_url || clip.url || "";
    co.slots[slotIdx].title    = clip.title || clip.name || `Клип ${clipIdx + 1}`;
    co.slots[slotIdx].clip_idx = clipIdx;
  }
  renderActiveTab();
}

function coSetUrl(slotIdx, url) {
  state.clipopus.slots[slotIdx].url   = url.trim();
  state.clipopus.slots[slotIdx].title = "";
}

function coToggleRes(slotIdx, res) {
  const slot = state.clipopus.slots[slotIdx];
  slot.resolutions[res] = !slot.resolutions[res];
  // Перерисовываем только чипсы
  const el = document.querySelectorAll(".co-res-row")[slotIdx];
  if (el) {
    el.querySelectorAll(".co-res-chip").forEach(chip => {
      const r = chip.textContent.trim();
      chip.classList.toggle("on", !!slot.resolutions[r]);
    });
  }
}

async function coPickOutdir() {
  const d = await pywebview.api.pick_folder();
  if (d) { state.clipopus.outdir = d; renderActiveTab(); }
}

async function coStartResize() {
  const co = state.clipopus;
  if (co.processing) return;

  const clips = co.slots
    .filter(s => s.url.trim())
    .map(s => ({
      url:         s.url.trim(),
      title:       s.title || s.url.split("/").pop() || "clip",
      resolutions: Object.entries(s.resolutions).filter(([,v])=>v).map(([k])=>k),
    }))
    .filter(c => c.resolutions.length > 0);

  if (!clips.length) {
    appendLog("ClipOpus: введите хотя бы один URL и выберите разрешения", "error");
    return;
  }

  setBar("bar-overall", 0); setBar("bar-current", 0);
  document.getElementById("progress-pct").textContent  = "0 %";
  document.getElementById("progress-title").textContent = "ClipOpus: скачиваем…";
  document.getElementById("current-name").textContent   = "—";
  const progressWrap = document.querySelector(".progress-wrap");
  if (progressWrap) progressWrap.style.display = "";

  const r = await pywebview.api.start_clipopus_resize({
    clips,
    outdir:      co.outdir_same ? "" : co.outdir,
    outdir_same: co.outdir_same,
  });
  if (!r.ok) { appendLog("ClipOpus: " + (r.error || "Ошибка запуска"), "error"); return; }
  co.processing = true;
  co.status_msg = "Запускаем…";
  renderActiveTab();
}

async function coStop() {
  await pywebview.api.stop_clipopus();
  appendLog("ClipOpus: остановка…", "warn");
}

function setSoundCursor(e) {
  const tl = document.getElementById("snd-timeline");
  if (!tl || !state.sound.duration) return;
  const rect = tl.getBoundingClientRect();
  const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  state.sound.cursor = parseFloat((pct * state.sound.duration).toFixed(2));
  const cur = document.getElementById("snd-cursor");
  if (cur) cur.style.left = (pct * 100).toFixed(2) + "%";
  const lbl = document.getElementById("snd-cursor-label");
  if (lbl) lbl.innerHTML =
    `Курсор: <b style="color:#ffb02e">${state.sound.cursor.toFixed(2)}s</b>
     &nbsp;·&nbsp; кликните по таймлайну для позиции`;
}

async function pickSoundVideo() {
  const r = await pywebview.api.pick_sound_video();
  if (!r) return;
  state.sound.file     = r;
  state.sound.duration = r.duration || 0;
  state.sound.cursor   = 0;
  state.sound.thumbs   = [];
  renderSoundTabContent();
  // Load thumbnails in background
  const tr = await pywebview.api.get_video_thumbnails(r.path, 10);
  if (tr && tr.ok && tr.thumbs) {
    state.sound.thumbs = tr.thumbs;
    // Only update timeline strip if we're still on the sound tab
    const tl = document.getElementById("snd-timeline");
    if (tl) {
      const bar = tl.querySelector(".snd-video-bar");
      if (bar) {
        const thumbStrip = tr.thumbs.length
          ? `<div style="display:flex;height:100%;position:absolute;inset:0;border-radius:6px;overflow:hidden;opacity:0.75;">
              ${tr.thumbs.map(th => th
                ? `<img src="${th}" style="flex:1;object-fit:cover;min-width:0;height:100%;">`
                : `<div style="flex:1;background:var(--bg-elev);"></div>`
              ).join("")}
             </div>`
          : "";
        const label = bar.querySelector("span");
        bar.style.position = "relative";
        const existing = bar.querySelector("div");
        if (existing) existing.remove();
        bar.insertAdjacentHTML("afterbegin", thumbStrip);
      }
    }
  }
}

function removeSoundVideo() {
  state.sound.file = null; state.sound.duration = 0;
  state.sound.thumbs = []; state.sound.tracks = []; state.sound.cursor = 0;
  renderSoundTabContent();
}

async function addVoiceFromFile() {
  const files = await pywebview.api.pick_audio_files();
  if (!files || !files.length) return;
  files.forEach(f => {
    state.sound.tracks.push({
      id: Date.now() + Math.random(),
      start: state.sound.cursor || 0,
      duration: f.duration || 0,
      file: f.path, name: f.name,
    });
  });
  renderSoundTabContent();
}

function updateTrackStart(id, val) {
  const tr = state.sound.tracks.find(t => t.id === id);
  if (tr) tr.start = Math.max(0, val);
  const bar = document.querySelector(".snd-tracks-bar");
  if (!bar || !state.sound.duration) return;
  const dur = state.sound.duration;
  bar.innerHTML = state.sound.tracks.map((tr2, i) => {
    const left  = Math.max(0, Math.min(100, tr2.start / dur * 100)).toFixed(2);
    const width = Math.max(0.5, Math.min(100-parseFloat(left), tr2.duration/dur*100)).toFixed(2);
    const color = SND_COLORS[i % SND_COLORS.length];
    const nm = tr2.name.length > 16 ? tr2.name.substring(0,16)+"…" : tr2.name;
    return `<div class="snd-track-block"
       style="left:${left}%;width:${width}%;background:${color}28;border-color:${color}">
       <span class="snd-block-label" style="color:${color}">${escapeHtml(nm)}</span></div>`;
  }).join("");
}

function removeTrack(id) {
  state.sound.tracks = state.sound.tracks.filter(t => t.id !== id);
  renderSoundTabContent();
}

// Play audio via afplay (macOS native, bypasses WKWebView sandbox)
async function sndPlayAudio(path, _idx) {
  try { await pywebview.api.play_audio(path); } catch(e) { appendLog("Ошибка воспроизведения", "error"); }
}

// Play a track from the timeline list
function previewTrack(id) {
  const tr = state.sound.tracks.find(t => t.id === id);
  if (tr) sndPlayAudio(tr.file);
}

// Play item from TTS library
function sndPlayLibItem(idx) {
  const item = state.sound.tts_library[idx];
  if (item) sndPlayAudio(item.file);
}

// Add library item to timeline at cursor
function addLibItemToTimeline(idx) {
  const s    = state.sound;
  const item = s.tts_library[idx];
  if (!item) return;
  s.tracks.push({
    id: Date.now() + Math.random(),
    start:    s.cursor || 0,
    duration: item.dur || 0,
    file:     item.file,
    name:     "TTS: " + (item.label.length > 22 ? item.label.substring(0,22)+"…" : item.label),
  });
  renderSoundTabContent();
}

function removeLibItem(idx) {
  state.sound.tts_library.splice(idx, 1);
  renderSoundTabContent();
}

// Legacy: add last TTS result to timeline (kept for compat)
function addTTSToTimeline() {
  const s = state.sound;
  if (!s.tts_last_file) return;
  const text = s.tts_text.trim();
  const name = "TTS: " + (text.length > 22 ? text.substring(0,22)+"…" : text);
  s.tracks.push({
    id: Date.now() + Math.random(),
    start: s.cursor || 0,
    duration: s.tts_last_dur || 0,
    file: s.tts_last_file, name,
  });
  renderSoundTabContent();
}

async function startTTSGenerate() {
  const s = state.sound;
  if (!(s.tts_text || "").trim()) { appendLog("TTS: введите текст", "warn"); return; }
  s.tts_generating = true; s.tts_last_file = null; s.tts_pct = 0;
  renderSoundTabContent();
  const r = await pywebview.api.generate_tts({
    text: s.tts_text, lang: s.tts_lang,
    gender: s.tts_gender, speed: s.tts_speed,
  });
  if (!r.ok) {
    s.tts_generating = false;
    renderSoundTabContent();
    appendLog("TTS: " + (r.error || "Ошибка"), "error");
  }
}

function setSoundTTSLang(l)   { state.sound.tts_lang   = l; renderSoundTabContent(); }
function setSoundTTSGender(g) { state.sound.tts_gender = g; renderSoundTabContent(); }
function setSoundAudioMode(m) { state.sound.original_audio = m; renderSoundTabContent(); }

async function pickSoundOutdir() {
  const d = await pywebview.api.pick_folder();
  if (d) { state.sound.outdir = d; renderSoundTabContent(); }
}

async function startSoundMix() {
  const s = state.sound;
  if (!s.file || !s.tracks.length || s.mixing) return;
  s.mixing       = true;
  s.mix_progress = 0;
  s.mix_status   = "Подготовка…";
  s.mix_last_dst = "";
  renderSoundTabContent();
  const r = await pywebview.api.start_sound_mix({
    src_path: s.file.path,
    tracks: s.tracks.map(t => ({start:t.start, file:t.file, duration:t.duration, name:t.name})),
    original_audio: s.original_audio,
    outdir: s.outdir, outdir_same: s.outdir_same,
  });
  if (!r.ok) {
    s.mixing = false;
    renderSoundTabContent();
    appendLog("Звук: " + (r.error || "Ошибка"), "error");
  }
  // On success: SoundMixWorker fires onMixProgress / onMixDone via soundApi
}
</script>
</body>
</html>
"""


# =============================================================================
# Запуск
# =============================================================================
def main():
    api = API()
    window = webview.create_window(
        APP_TITLE,
        html=HTML,
        js_api=api,
        width=1000,
        height=900,
        min_size=(880, 780),
        background_color="#0a0d14",
    )
    api.window = window
    webview.start()


if __name__ == "__main__":
    main()
