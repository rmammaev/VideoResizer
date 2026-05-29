# ClipOpus → Resizer (веб)

Веб-сервис: кидаешь ссылку на длинное видео → OpusClip нарезает короткие клипы →
сервис скачивает их и подгоняет под нужные вертикальные/квадратные форматы тем же
blur-fit ресайзером, что и десктоп-приложение `../app.py`.

## Поток
1. `POST /api/jobs {video_url, resolutions}` → создаётся проект в OpusClip
   (`POST /clip-projects`).
2. Сервис ждёт нарезку (поллинг `GET /exportable-clips`; вебхук ускоряет).
3. Скачивает каждый клип (`uriForExport`), ресайзит в выбранные разрешения (ffmpeg).
4. Пакует zip. Фронт поллит `GET /api/jobs/{id}` и показывает ссылки.

Разрешения: `1080x1080` (1:1), `1080x1350` (4:5), `1080x1920` (9:16), `1920x1080` (16:9).

## Переменные окружения
| Переменная | Назначение |
|---|---|
| `OPUS_API_KEY` | ключ OpusClip (дашборд → API key; план Pro Beta / Business) |
| `OPUS_ORG_ID` | Org ID (заголовок `x-opus-org-id`) |
| `PUBLIC_BASE_URL` | публичный URL для вебхука (на Railway берётся из `RAILWAY_PUBLIC_DOMAIN`) |
| `DATA_DIR` | папка под файлы (по умолч. `data`) |
| `OPUS_POLL_INTERVAL` / `OPUS_POLL_TIMEOUT` | настройка поллинга (сек) |

## Локальный запуск
```bash
cd clipopus_web
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # вписать OPUS_API_KEY и OPUS_ORG_ID
uvicorn main:app --reload
# http://localhost:8000
```
Нужен установленный `ffmpeg` в PATH (`brew install ffmpeg`).

## Docker (как на Railway)
```bash
docker build -t clipopus-web .
docker run -p 8000:8000 --env-file .env clipopus-web
```

## Деплой на Railway
1. Запушить репозиторий на GitHub.
2. Railway → **New Project → Deploy from GitHub repo**.
3. Settings → **Root Directory** = `clipopus_web` (репо общий с десктоп-приложением).
   Билд по `Dockerfile` подхватится автоматически (есть `railway.json`).
4. **Variables**: `OPUS_API_KEY`, `OPUS_ORG_ID`.
5. Settings → Networking → **Generate Domain** → `RAILWAY_PUBLIC_DOMAIN` появится сам,
   вебхук включится автоматически.
6. Открыть выданный URL.

> Диск на Railway эфемерный — готовые файлы живут до рестарта контейнера. Для постоянного
> хранения подключить Volume и указать на него `DATA_DIR`.
