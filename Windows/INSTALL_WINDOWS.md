# Video Resizer v4 — Инструкция по сборке и установке на Windows 10/11

## Что входит в папку Windows/

| Файл | Описание |
|------|----------|
| `app.py` | Основное приложение (Windows-версия) |
| `VideoResizer.spec` | Конфиг PyInstaller для сборки .exe |
| `setup_and_build.bat` | Скрипт автоматической установки и сборки |
| `INSTALL_WINDOWS.md` | Этот файл |

---

## Требования

- Windows 10 (версия 1809+) или Windows 11
- Python 3.10, 3.11, 3.12 или 3.13
- FFmpeg (с libass и libfreetype)
- Интернет для скачивания зависимостей

---

## Способ 1 — Автоматическая сборка (рекомендуется)

### Шаг 1. Установите Python

Скачайте с **https://www.python.org/downloads/**

> ⚠️ При установке **обязательно** отметьте галочку **"Add Python to PATH"**

### Шаг 2. Установите FFmpeg

**Вариант A — через winget** (Windows 10 1809+ / Windows 11):
```
winget install --id Gyan.FFmpeg
```
После установки закройте и откройте командную строку заново.

**Вариант B — вручную:**
1. Скачайте с https://ffmpeg.org/download.html → Windows Builds (от Gyan или BtbN)
2. Распакуйте в `C:\ffmpeg\`
3. Добавьте `C:\ffmpeg\bin` в системный PATH:
   - Пуск → "Переменные среды" → Переменные среды → Path → Изменить → Создать

### Шаг 3. Скопируйте папку Windows на ПК

Скопируйте папку `Windows/` на Windows-компьютер (например в `C:\VideoResizerBuild\`).

Также скопируйте **icon.png** из папки `MAC/` в папку `Windows/` если хотите кастомную иконку.

### Шаг 4. Запустите setup_and_build.bat

Дважды кликните **`setup_and_build.bat`**. Скрипт автоматически:

1. Проверит Python и FFmpeg
2. Установит/скачает Microsoft WebView2 Runtime (если нужно)
3. Установит все Python-пакеты: `pywebview`, `pyinstaller`, `Pillow`, `pyttsx3`, `pywin32`, `edge-tts`, `openai-whisper`
4. Создаст `icon.ico`
5. Соберёт `dist\VideoResizer\VideoResizer.exe` через PyInstaller

**Время:** 5–15 минут при первом запуске.

### Шаг 5. Запустите приложение

Готово! Откройте `dist\VideoResizer\VideoResizer.exe`.

Для удобства создайте ярлык на рабочем столе: ПКМ на `VideoResizer.exe` → Отправить → Рабочий стол.

---

## Способ 2 — Ручная сборка

Если автоматический скрипт не подходит, выполните в командной строке (в папке Windows/):

```bat
:: Установка зависимостей
pip install pywebview[winforms] pyinstaller Pillow pyttsx3 pywin32 comtypes edge-tts openai-whisper

:: Конвертация иконки (если есть icon.png)
python -c "from PIL import Image; Image.open('icon.png').save('icon.ico', format='ICO')"

:: Сборка
pyinstaller VideoResizer.spec --noconfirm --clean
```

Готовый .exe будет в `dist\VideoResizer\VideoResizer.exe`.

---

## Дистрибуция (передать другому пользователю)

После сборки передайте **всю папку** `dist\VideoResizer\` — это portable-сборка, не требующая установки Python.

Пользователю нужно:
1. Установить **FFmpeg** (или положить `ffmpeg.exe` рядом с `VideoResizer.exe`)
2. Установить **Microsoft WebView2 Runtime** (на Windows 11 уже встроен):
   https://developer.microsoft.com/microsoft-edge/webview2/
3. Запустить `VideoResizer.exe`

---

## Возможные проблемы

### "Приложение не запускается / чёрный экран"
- Убедитесь, что установлен **WebView2 Runtime**
- Попробуйте запустить через командную строку для просмотра ошибок:
  ```
  VideoResizer.exe
  ```

### "FFmpeg не найден"
- Проверьте, что ffmpeg.exe в PATH: откройте cmd, введите `ffmpeg -version`
- Или положите `ffmpeg.exe` в папку `dist\VideoResizer\`

### "Нет звука при воспроизведении TTS"
- Требуется доступ к интернету для Edge TTS
- Для офлайн-голоса Windows нужен установленный TTS-голос: Пуск → Параметры → Время и язык → Речь → Добавить голоса

### "Субтитры не работают (no such filter: ass)"
- Установите FFmpeg от **Gyan** (полная сборка с libass): https://www.gyan.dev/ffmpeg/builds/
- Скачайте `ffmpeg-release-full.7z`

### "Антивирус блокирует .exe"
- PyInstaller-сборки иногда ложно срабатывают. Добавьте папку `dist\VideoResizer\` в исключения.

### Ошибка при сборке: "icon.ico: No such file"
- Создайте пустой файл: в папке Windows/ введите в cmd: `copy nul icon.ico`
- Или добавьте свой icon.ico

---

## Отличия Windows-версии от macOS

| Функция | macOS | Windows |
|---------|-------|---------|
| UI движок | WKWebView (Safari) | EdgeChromium (WebView2) |
| Воспроизведение аудио | `afplay` | PowerShell MediaPlayer |
| Офлайн TTS | macOS `say` (Milena/Samantha) | Windows SAPI (`pyttsx3`) |
| Онлайн TTS | Edge TTS | Edge TTS (то же) |
| Сборка | `.app` (PyInstaller) | `.exe` (PyInstaller) |
| Шрифты для субтитров | `/Library/Fonts/Arial.ttf` | `C:\Windows\Fonts\arial.ttf` |

---

## Структура собранного приложения

```
dist\VideoResizer\
├── VideoResizer.exe          ← главный исполняемый файл
├── _internal\                ← Python-библиотеки (не трогать)
│   ├── webview\
│   ├── PIL\
│   └── ...
```

> **Важно:** VideoResizer.exe работает только вместе с папкой `_internal\`. Нельзя скопировать только .exe отдельно.
