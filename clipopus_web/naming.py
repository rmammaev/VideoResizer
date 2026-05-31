"""
Именование выходных файлов — перенос из app.py (NamingConfig + хелперы).

Режимы:
  keep   — оставить имя исходника, заменить/добавить суффикс _<разрешение>
  custom — шаблон с токенами {name} {tag} {resolution} {date} {index}
  os     — конвенция OS_{team}_{type}_{name}_{date}_EN_{dur}_{res}
"""
from __future__ import annotations

import datetime as _dt
import re

KNOWN_TEAMS = ["In-House", "Freelance"]
KNOWN_TYPES = ["Gameplay", "Unreal", "Cinematic", "Combo", "UGC", "AI", "AI-Hook"]
_PARSE_TYPES = set(KNOWN_TYPES) | {"Сinematic", "Hook"}
_PARSE_TEAMS = set(KNOWN_TEAMS)
_RX_SPECIAL = re.compile(r"^(EN|\d{2}-\d{2}|\d+s|\d+x\d+)$")
_RX_RES_SUFFIX = re.compile(r"_\d+x\d+$")

DEFAULT_TEMPLATE_RESIZE = "{name}_{resolution}"
DEFAULT_TEMPLATE_CONCAT = "concat_{date}_{resolution}"


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]", "_", (name or "").strip())
    return name.strip("_- ") or "output"


def extract_name_from_filename(stem: str):
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
    inits = []
    for stem in stems:
        name = extract_name_from_filename(stem) or stem
        words = [w for w in name.split("-") if w]
        ini = "".join(w[0].upper() for w in words) if words else "X"
        inits.append(ini)
    return "-".join(inits)


def keep_original_naming(src_stem: str, res_key: str) -> str:
    new = _RX_RES_SUFFIX.sub(f"_{res_key}", src_stem)
    if new == src_stem:
        new = f"{src_stem}_{res_key}"
    return new


def format_date_short(now=None) -> str:
    return (now or _dt.datetime.now()).strftime("%m-%y")


def format_duration(seconds) -> str:
    return f"{int(round(max(seconds, 0)))}s"


def render_template(template, *, name, tag, resolution, index=1) -> str:
    date_str = _dt.datetime.now().strftime("%Y-%m-%d")
    vars_ = {"name": name, "tag": tag or "", "resolution": resolution,
             "date": date_str, "index": f"{index:02d}"}
    result = template or DEFAULT_TEMPLATE_RESIZE
    if not tag:
        result = re.sub(r"_\{tag\}", "", result, count=1)
        result = re.sub(r"\{tag\}_", "", result, count=1)
        result = result.replace("{tag}", "")
    for k, v in vars_.items():
        result = result.replace("{" + k + "}", str(v))
    result = re.sub(r"\{[^}]*\}", "", result)
    result = re.sub(r"__+", "_", result)
    result = result.strip("_- ")
    return safe_filename(result)


class NamingConfig:
    MODE_OS = "os"
    MODE_KEEP = "keep"
    MODE_CUSTOM = "custom"

    def __init__(self, *, mode="keep", team="In-House", type_="Unreal",
                 name_source="auto", name_manual="",
                 template=DEFAULT_TEMPLATE_RESIZE, tag=""):
        self.mode = mode
        self.team = team
        self.type = type_
        self.name_source = name_source
        self.name_manual = (name_manual or "").strip()
        self.template = template or DEFAULT_TEMPLATE_RESIZE
        self.tag = (tag or "").strip()

    @classmethod
    def from_dict(cls, d, default_template=DEFAULT_TEMPLATE_RESIZE):
        d = d or {}
        return cls(
            mode=d.get("mode", "keep"),
            team=d.get("team", "In-House"),
            type_=d.get("type", "Unreal"),
            name_source=d.get("name_source", "auto"),
            name_manual=d.get("name_manual", ""),
            template=d.get("template") or default_template,
            tag=d.get("tag", ""),
        )

    def make_name(self, *, src_stem, res_key, output_duration_sec, all_src_stems=None):
        if self.mode == self.MODE_KEEP:
            return keep_original_naming(src_stem, res_key)
        if self.mode == self.MODE_CUSTOM:
            return render_template(self.template, name=src_stem,
                                   tag=self.tag, resolution=res_key)
        # OS
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
        return safe_filename(
            f"OS_{self.team}_{self.type}_{name_part}_{date_s}_EN_{dur_s}_{res_key}")
