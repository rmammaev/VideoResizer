# ClipOpus интеграция — передача в новый чат

Этот файл описывает работу, сделанную в `/Users/macbook/Desktop/FFMpegTutor/MAC/app.py`.
Прочитай его в новой сессии Claude Code, открытой в папке `FFMpegTutor/MAC`, чтобы продолжить.

## Что за проект
`app.py` — десктопное приложение **Video Resizer v4** (macOS) на pywebview (нативное
WebKit-окно, UI = HTML/CSS/JS внутри одной большой Python-строки `HTML = r"""..."""`).
Связь JS ⇄ Python: `pywebview.api.<метод>` (JS → Python) и
`window.evaluate_js("window.<api>.<fn>(...)")` (Python → JS).

Существующие вкладки: **Ресайз**, **Склейка**, **Субтитры**, **Звук**.

## Что было добавлено: вкладка «ClipOpus»
Новая вкладка, которая берёт 3 ролика из сервиса OpusClip (https://clip.opus.pro)
по их API и автоматически прогоняет через существующий ресайзер.

### API OpusClip (выяснено из https://help.opus.pro/llms.txt)
- Base URL: `https://api.opus.pro/api`
- Авторизация: заголовок `Authorization: Bearer <API_KEY>`
- `GET /projects` — список проектов
- `GET /clips?projectId=<id>` — список клипов проекта (в ответе у клипов берём
  поля `stream_url` / `download_url` / `url` для скачивания)
- Доступ к API в закрытой бете (платные годовые Pro-планы). Ключ берётся в
  clip.opus.pro → Settings → API.

### Изменения в app.py (всё уже внесено и проходит `python3 -c "import ast..."`)

**Python:**
1. Класс `ClipOpusWorker(threading.Thread)` (рядом с блоком `HTML = r"""`):
   скачивает клипы по URL в `~/VideoResizer/ClipOpus/`, затем переиспользует
   `ResizeWorker` для ресайза. Шлёт прогресс в JS через `window.clipOpusApi.*`.
2. В `API.__init__` добавлено поле `self.clipopus_worker = None`.
3. Методы класса `API`:
   - `load_clipopus_key()` / `save_clipopus_key(api_key)` — ключ в
     `~/VideoResizer/clipopus_key.txt`
   - `fetch_clipopus_projects(api_key)`
   - `fetch_clipopus_clips(api_key, project_id)`
   - `start_clipopus_resize(params)` — params = {clips:[{url,title,resolutions:[]}], outdir, outdir_same}
   - `stop_clipopus()`

**JS (внутри строки HTML):**
4. CSS-блок `.co-*` (рядом с `.nav-item.active[data-tab="clipopus"]`).
5. Кнопка в sidebar: `<button class="nav-item" data-tab="clipopus" ...>`.
6. `state.clipopus = { api_key, project_id, clips, slots[3], outdir, outdir_same,
   loading, processing, phase, status_msg }`. Каждый слот:
   `{ url, title, clip_idx, resolutions: {"1080x1080":bool, ...} }`.
7. `switchTab()` и `renderActiveTab()` обрабатывают `clipopus`; кнопки СТАРТ/СТОП
   скрыты для этой вкладки (своя кнопка в теле).
8. `window.clipOpusApi = { onDownload, onResizeStart, onAllDone, onStopped, onError }`.
9. `renderClipOpusTab()` + хелперы: `coSaveKey`, `coFetchClips`, `coSelectClip`,
   `coSetUrl`, `coToggleRes`, `coPickOutdir`, `coStartResize`, `coStop`.
10. В `init()` подгружается сохранённый ключ через `load_clipopus_key()`.

### Доступные разрешения ресайза (из `RESOLUTIONS`)
`1080x1080` (1:1), `1080x1350` (4:5), `1080x1920` (9:16), `1920x1080` (16:9).

## Статус
- Синтаксис Python — OK.
- НЕ протестировано вживую (нет `webview` в окружении проверки + нет реального
  API-ключа OpusClip). Нужно: запустить приложение, ввести ключ + Project ID,
  загрузить клипы, проверить скачивание и ресайз.

## Возможные доработки / на что обратить внимание
- Поля ответа `/clips` могли угадать неточно — проверить реальную структуру JSON
  (имена `stream_url`/`download_url`/`url`, `title`/`name`).
- Прямые ссылки на .mp4 могут требовать заголовков/подписи — проверить, что
  `urllib` их тянет без авторизации; иначе добавить Bearer и к скачиванию.
- Эндпоинт `/clips` может требовать пагинацию — сейчас берём как есть.

## Важно про окружение
Папка `FFMpegTutor/MAC` сейчас не отдельный git-репо: `git init` случайно сделан
в домашней папке `/Users/macbook` (поэтому старый чат «видел» asana-ai-bot).
При желании — выделить MAC в отдельный репозиторий.
