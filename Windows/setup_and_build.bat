@echo off
chcp 65001 > nul
cls

:: =============================================================================
::  Video Resizer v4  ·  Windows 10/11  ·  Установка и сборка
::  Запустите ОДИН РАЗ двойным кликом — скрипт всё установит и соберёт .exe
:: =============================================================================

echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║           Video Resizer v4  ·  Windows Setup                ║
echo  ╠══════════════════════════════════════════════════════════════╣
echo  ║  • Ресайз видео под 4 формата                               ║
echo  ║  • Склейка с переходами                                     ║
echo  ║  • Авто-субтитры Whisper + анимации                         ║
echo  ║  • TTS генератор голоса (Edge TTS / Windows SAPI)           ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.
echo  Этот скрипт установит все зависимости и соберёт VideoResizer.exe
echo  Примерное время: 5–15 минут при первом запуске.
echo.
pause

cd /d "%~dp0"

:: =============================================================================
:: ШАГ 1 — Проверка Python 3.10+
:: =============================================================================
echo.
echo [1/7] Проверка Python 3.10+...

python --version 2>nul
if %ERRORLEVEL% neq 0 (
    echo   ОШИБКА: Python не найден!
    echo   Скачайте с https://www.python.org/downloads/
    echo   При установке обязательно отметьте "Add Python to PATH"
    pause
    exit /b 1
)

:: Check version >= 3.10
python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>nul
if %ERRORLEVEL% neq 0 (
    echo   ОШИБКА: Нужен Python 3.10 или новее.
    echo   Скачайте с https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo   OK: %%i

:: =============================================================================
:: ШАГ 2 — Проверка FFmpeg
:: =============================================================================
echo.
echo [2/7] Проверка FFmpeg...

ffmpeg -version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   FFmpeg не найден в PATH.
    echo.
    echo   Способы установки:
    echo     1. winget install --id Gyan.FFmpeg  (рекомендуется, Windows 10/11)
    echo     2. Или скачайте с https://ffmpeg.org/download.html
    echo        и добавьте папку bin в системный PATH.
    echo.
    echo   После установки FFmpeg запустите этот скрипт ещё раз.
    pause
    exit /b 1
)

for /f "tokens=1,3" %%a in ('ffmpeg -version 2^>^&1 ^| findstr "ffmpeg version"') do (
    echo   OK: %%a %%b
    goto :ffmpeg_ok
)
:ffmpeg_ok

:: =============================================================================
:: ШАГ 3 — Microsoft WebView2 Runtime (нужен для pywebview)
:: =============================================================================
echo.
echo [3/7] Проверка WebView2 Runtime...

:: WebView2 уже встроен в Windows 11; на Win10 — проверяем реестр
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo   OK: WebView2 Runtime найден.
) else (
    reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        echo   OK: WebView2 Runtime найден.
    ) else (
        echo   WebView2 Runtime не найден. Устанавливаем...
        echo   Скачиваем с серверов Microsoft...
        powershell -NoProfile -Command ^
            "Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile '%TEMP%\webview2setup.exe'"
        if exist "%TEMP%\webview2setup.exe" (
            "%TEMP%\webview2setup.exe" /silent /install
            echo   OK: WebView2 установлен.
        ) else (
            echo   ПРЕДУПРЕЖДЕНИЕ: Не удалось скачать WebView2.
            echo   Скачайте вручную: https://developer.microsoft.com/microsoft-edge/webview2/
        )
    )
)

:: =============================================================================
:: ШАГ 4 — Обновление pip
:: =============================================================================
echo.
echo [4/7] Обновление pip...
python -m pip install --upgrade pip --quiet
echo   OK: pip обновлён.

:: =============================================================================
:: ШАГ 5 — Python-зависимости
:: =============================================================================
echo.
echo [5/7] Установка Python-зависимостей...
echo   (первый раз ~3–5 мин)
echo.

python -m pip install --upgrade --quiet ^
    pywebview[winforms] ^
    pyinstaller ^
    Pillow ^
    pyttsx3 ^
    pywin32 ^
    comtypes

if %ERRORLEVEL% neq 0 (
    echo   ОШИБКА при установке базовых зависимостей.
    pause
    exit /b 1
)
echo   OK: базовые зависимости установлены.

echo   Устанавливаем edge-tts...
python -m pip install --upgrade --quiet edge-tts
echo   OK: edge-tts установлен.

echo   Устанавливаем openai-whisper (может занять несколько минут, ~500 МБ)...
python -m pip install --upgrade --quiet openai-whisper
echo   OK: openai-whisper установлен.

:: =============================================================================
:: ШАГ 6 — Генерация иконки .ico
:: =============================================================================
echo.
echo [6/7] Подготовка иконки...

if exist "icon.png" (
    python -c ^
        "from PIL import Image; img=Image.open('icon.png'); " ^
        "img.save('icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])" ^
        2>nul
    if exist "icon.ico" (
        echo   OK: icon.ico создан.
    ) else (
        echo   ПРЕДУПРЕЖДЕНИЕ: Не удалось создать icon.ico — будет использована иконка по умолчанию.
        :: Create empty icon reference to avoid spec error
        copy nul icon.ico >nul 2>&1
    )
) else (
    echo   icon.png не найден — будет использована иконка по умолчанию.
    copy nul icon.ico >nul 2>&1
)

:: Create minimal version_info.txt if missing
if not exist "version_info.txt" (
    python -c ^
        "open('version_info.txt','w').write('VSVersionInfo(ffi=FixedFileInfo(filevers=(4,0,0,0),prodvers=(4,0,0,0),mask=0x3f,flags=0x0,OS=0x4,fileType=0x1,subtype=0x0,date=(0,0)),kids=[StringFileInfo([StringTable(u\"040904B0\",[StringStruct(u\"CompanyName\",u\"\"),StringStruct(u\"FileDescription\",u\"Video Resizer v4\"),StringStruct(u\"FileVersion\",u\"4.0.0.0\"),StringStruct(u\"InternalName\",u\"VideoResizer\"),StringStruct(u\"LegalCopyright\",u\"\"),StringStruct(u\"OriginalFilename\",u\"VideoResizer.exe\"),StringStruct(u\"ProductName\",u\"Video Resizer\"),StringStruct(u\"ProductVersion\",u\"4.0.0.0\")])]),VarFileInfo([VarStruct(u\"Translation\",[1033,1200])])])')"
)

:: =============================================================================
:: ШАГ 7 — Сборка .exe через PyInstaller
:: =============================================================================
echo.
echo [7/7] Сборка VideoResizer.exe (1–5 мин)...
echo   Пожалуйста, подождите...

if exist "build" rmdir /s /q "build"
python -m PyInstaller VideoResizer.spec --noconfirm --clean --log-level WARN

if %ERRORLEVEL% neq 0 (
    echo.
    echo   ОШИБКА: PyInstaller завершился с ошибкой.
    echo   Проверьте вывод выше.
    pause
    exit /b 1
)

if not exist "dist\VideoResizer\VideoResizer.exe" (
    echo   ОШИБКА: .exe не создан — проверьте ошибки выше.
    pause
    exit /b 1
)

:: Clean build artifacts
if exist "build" rmdir /s /q "build"
if exist "__pycache__" rmdir /s /q "__pycache__"

echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║                     ВСЁ ГОТОВО!                             ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.
echo   Приложение собрано в:  %~dp0dist\VideoResizer\
echo.
echo   Как запустить:
echo     • Дважды кликните VideoResizer.exe в папке dist\VideoResizer\
echo     • Или создайте ярлык на рабочем столе
echo.
echo   Первый запуск субтитров:
echo     Whisper скачает модель (~150 МБ) автоматически при первом использовании.
echo.
echo   Если нужны обновления — запустите этот скрипт ещё раз.
echo.

:: Open the output folder
explorer "%~dp0dist\VideoResizer"

pause
