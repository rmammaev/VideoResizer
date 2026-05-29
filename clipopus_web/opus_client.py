"""
Обёртка над OpusClip REST API (https://help.opus.pro/api-reference).

Base URL : https://api.opus.pro/api
Auth     : заголовок Authorization: Bearer <API_KEY>  + x-opus-org-id: <ORG_ID>
Поток    : POST /clip-projects {videoUrl,...} -> projectId
           GET  /exportable-clips?q=findByProjectId&projectId=<id> -> [ {uriForExport,...} ]
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

BASE_URL = os.environ.get("OPUS_BASE_URL", "https://api.opus.pro/api").rstrip("/")


class OpusError(RuntimeError):
    """Ошибка обращения к OpusClip API (несёт http-статус и тело ответа)."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class OpusClient:
    def __init__(self, api_key: Optional[str] = None, org_id: Optional[str] = None,
                 *, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("OPUS_API_KEY", "")
        self.org_id = org_id or os.environ.get("OPUS_ORG_ID", "")
        self.timeout = timeout

    # --- внутреннее -------------------------------------------------------
    def _headers(self, json_body: bool = False) -> dict:
        if not self.api_key:
            raise OpusError("Не задан OPUS_API_KEY")
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if self.org_id:
            h["x-opus-org-id"] = self.org_id
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def _request(self, method: str, path: str, *, params=None, json=None) -> Any:
        url = f"{BASE_URL}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(
                method, url, params=params, json=json,
                headers=self._headers(json_body=json is not None))
        if resp.status_code >= 400:
            raise OpusError(
                f"{method} {path} -> HTTP {resp.status_code}",
                status=resp.status_code, body=resp.text[:1000])
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # --- публичные методы -------------------------------------------------
    async def create_project(
        self,
        video_url: str,
        *,
        curation_pref: Optional[dict] = None,
        import_pref: Optional[dict] = None,
        brand_template_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """POST /clip-projects. Возвращает сырой ответ (минимум содержит id проекта)."""
        body: dict[str, Any] = {"videoUrl": video_url}
        if curation_pref:
            body["curationPref"] = curation_pref
        if import_pref:
            body["importPref"] = import_pref
        if brand_template_id:
            body["brandTemplateId"] = brand_template_id
        if webhook_url:
            body["conclusionActions"] = [
                {"type": "WEBHOOK", "url": webhook_url, "notifyFailure": True}
            ]
        data = await self._request("POST", "/clip-projects", json=body)
        return data if isinstance(data, dict) else {"raw": data}

    async def list_exportable_clips(self, project_id: str) -> list[dict]:
        """GET /exportable-clips?q=findByProjectId&projectId=... -> массив клипов."""
        data = await self._request(
            "GET", "/exportable-clips",
            params={"q": "findByProjectId", "projectId": project_id})
        return _as_list(data)


def project_id_from_response(data: dict) -> Optional[str]:
    """OpusClip может вернуть id под разными ключами — пробуем по очереди."""
    if not isinstance(data, dict):
        return None
    for key in ("projectId", "id", "_id"):
        v = data.get(key)
        if v:
            return str(v)
    # иногда оборачивают в data/project
    for wrap in ("data", "project"):
        inner = data.get(wrap)
        if isinstance(inner, dict):
            got = project_id_from_response(inner)
            if got:
                return got
    return None


def clip_download_url(clip: dict) -> Optional[str]:
    """Прямая ссылка на mp4 (uriForExport), с фолбэками на возможные имена."""
    for key in ("uriForExport", "downloadUrl", "download_url", "url", "uriForPreview"):
        v = clip.get(key)
        if v:
            return str(v)
    return None


def clip_title(clip: dict, fallback: str = "clip") -> str:
    for key in ("title", "name"):
        v = clip.get(key)
        if v:
            return str(v)
    return fallback


def _as_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # OpusClip оборачивает массив в {"list":[...]} (подтверждено вживую),
        # остальные ключи — на случай иных эндпоинтов
        for key in ("list", "data", "clips", "exportableClips", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []
