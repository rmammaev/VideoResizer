#!/bin/bash
# =============================================================================
#  Video Resizer v4  ·  Полная установка и сборка
#  Запустите ОДИН РАЗ двойным кликом — скрипт всё установит и соберёт .app
# =============================================================================

set -e
cd "$(dirname "$0")"
chmod +x ./*.command 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"
RED="\033[31m"; CYAN="\033[36m"; RESET="\033[0m"
OK="${GREEN}✓${RESET}"; WARN="${YELLOW}!${RESET}"; ERR="${RED}✕${RESET}"

clear
echo -e "${BOLD}"
cat <<'BANNER'
 ╔══════════════════════════════════════════════════════════════╗
 ║           Video Resizer v4  ·  macOS Setup                  ║
 ╠══════════════════════════════════════════════════════════════╣
 ║  • Ресайз видео под 4 формата                               ║
 ║  • Склейка с переходами                                     ║
 ║  • Авто-субтитры Whisper + анимации                         ║
 ╚══════════════════════════════════════════════════════════════╝
BANNER
echo -e "${RESET}"

echo "  Этот скрипт установит все зависимости и соберёт приложение."
echo "  Примерное время: 5–10 минут (при первом запуске)."
echo
read -rp "  Нажмите Enter, чтобы начать…"
echo

# =============================================================================
# ШАГ 1 — Homebrew
# =============================================================================
echo -e "${BOLD}[1/6] Homebrew${RESET}"

if ! command -v brew >/dev/null 2>&1; then
  echo -e "  ${WARN}  Homebrew не найден, устанавливаем…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Добавляем Homebrew в PATH для текущего сеанса
for _brewpath in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [ -x "$_brewpath" ]; then
    eval "$("$_brewpath" shellenv)"
    break
  fi
done

if ! command -v brew >/dev/null 2>&1; then
  echo -e "  ${ERR}  Homebrew не удалось установить. Установите вручную:"
  echo "       https://brew.sh"
  read -rp "  Enter для выхода."; exit 1
fi
echo -e "  ${OK}  Homebrew $(brew --version | head -1)"

# =============================================================================
# ШАГ 2 — Python 3.10+
# =============================================================================
echo
echo -e "${BOLD}[2/6] Python 3.10+${RESET}"

HBPY=""
for _cand in \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
    /usr/local/bin/python3; do
  if [ -x "$_cand" ]; then
    _ver=$("$_cand" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
    if [ "$_ver" = "True" ]; then HBPY="$_cand"; break; fi
  fi
done

if [ -z "$HBPY" ]; then
  echo "  Устанавливаем Python через Homebrew…"
  brew install python
  for _cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -x "$_cand" ] && { HBPY="$_cand"; break; }
  done
fi

if [ -z "$HBPY" ]; then
  echo -e "  ${ERR}  Python не найден. Установите вручную: brew install python"
  read -rp "  Enter для выхода."; exit 1
fi
echo -e "  ${OK}  $("$HBPY" --version)  ($HBPY)"

# =============================================================================
# ШАГ 3 — FFmpeg
# =============================================================================
echo
echo -e "${BOLD}[3/6] FFmpeg${RESET}"

if command -v ffmpeg >/dev/null 2>&1; then
  echo -e "  ${OK}  $(ffmpeg -version 2>&1 | head -1 | awk '{print $1,$3}')"
  # Проверяем поддержку libass (нужна для субтитров)
  if ! ffmpeg -filters 2>/dev/null | grep -q "^ *ass "; then
    echo -e "  ${WARN}  FFmpeg без libass — переустанавливаем с полным набором…"
    brew reinstall ffmpeg
    echo -e "  ${OK}  FFmpeg переустановлен с поддержкой субтитров"
  fi
else
  echo "  Устанавливаем FFmpeg…"
  brew install ffmpeg
  echo -e "  ${OK}  FFmpeg установлен"
fi

# =============================================================================
# ШАГ 4 — Python-зависимости
# =============================================================================
echo
echo -e "${BOLD}[4/6] Python-зависимости${RESET}"
echo "  Устанавливаем: pywebview · pyobjc · pyinstaller · Pillow · openai-whisper · edge-tts"
echo "  (первый раз ~3–5 мин, Whisper ~500 МБ)"
echo

# Пробуем с флагом --break-system-packages (Python 3.12+), затем без него
_pip() {
  "$HBPY" -m pip install --upgrade --quiet "$@" --break-system-packages 2>/dev/null \
    || "$HBPY" -m pip install --upgrade --quiet "$@"
}

_pip pip
_pip \
  pywebview \
  pyobjc-framework-WebKit \
  pyobjc-framework-Cocoa \
  pyinstaller \
  Pillow

echo -e "  ${OK}  Базовые зависимости установлены"

# Whisper — ставится отдельно, может быть долго
echo "  Устанавливаем openai-whisper (может занять несколько минут)…"
_pip openai-whisper
echo -e "  ${OK}  openai-whisper установлен"

# edge-tts — онлайн TTS (Microsoft Edge)
echo "  Устанавливаем edge-tts…"
_pip edge-tts
echo -e "  ${OK}  edge-tts установлен"

# =============================================================================
# ШАГ 5 — Сборка .app
# =============================================================================
echo
echo -e "${BOLD}[5/6] Сборка VideoResizer.app${RESET}"

# Иконка
if [ -f icon.png ] && command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  echo "  Генерируем icon.icns…"
  rm -rf icon.iconset; mkdir -p icon.iconset
  for _s in 16 32 128 256 512; do
    sips -z $_s $_s         icon.png --out "icon.iconset/icon_${_s}x${_s}.png"      >/dev/null
    sips -z $((_s*2)) $((_s*2)) icon.png --out "icon.iconset/icon_${_s}x${_s}@2x.png"   >/dev/null
  done 2>/dev/null
  iconutil -c icns icon.iconset 2>/dev/null
  rm -rf icon.iconset
  echo -e "  ${OK}  icon.icns создан"
fi

# Чистим кеш и собираем
echo "  Запускаем PyInstaller (1–3 мин)…"
rm -rf __pycache__ build/ 2>/dev/null || true
"$HBPY" -m PyInstaller VideoResizer.spec \
  --noconfirm --clean --log-level WARN \
  --distpath "$(pwd)/dist"
rm -rf build/

APP="$(pwd)/dist/VideoResizer.app"
if [ ! -d "$APP" ]; then
  echo -e "  ${ERR}  .app не создан — проверьте ошибки выше."
  read -rp "  Enter для выхода."; exit 1
fi
echo -e "  ${OK}  Готово: $APP  ($(du -sh "$APP" | cut -f1))"

# =============================================================================
# ШАГ 6 — Установка в ~/Applications
# =============================================================================
echo
echo -e "${BOLD}[6/6] Установка${RESET}"

mkdir -p "$HOME/Applications"
DEST="$HOME/Applications/VideoResizer.app"

rm -rf "$DEST"
cp -R "$APP" "$DEST"
# Снимаем карантин — чтобы не нужен был правый клик → «Открыть»
xattr -rd com.apple.quarantine "$DEST" 2>/dev/null || true

echo -e "  ${OK}  Установлено: $DEST"

# =============================================================================
# Итог
# =============================================================================
echo
echo -e "${BOLD}${GREEN}"
cat <<'DONE'
 ╔══════════════════════════════════════════════════════════════╗
 ║                     ВСЁ ГОТОВО!                             ║
 ╚══════════════════════════════════════════════════════════════╝
DONE
echo -e "${RESET}"
echo "  Приложение установлено в:  ~/Applications/VideoResizer.app"
echo
echo "  Как открыть:"
echo "    • Finder → ~/Applications → VideoResizer"
echo "    • Или из Spotlight (Cmd+Space): «VideoResizer»"
echo
echo -e "  ${CYAN}Первый запуск Субтитров:${RESET}"
echo "    Whisper скачает языковую модель (~150 МБ) при первом использовании."
echo "    Это происходит автоматически — просто нажмите «Распознать субтитры»."
echo
echo "  Если нужны обновления — запустите этот скрипт ещё раз."
echo

# Открываем ~/Applications в Finder
open "$HOME/Applications"

read -rp "  Нажмите Enter, чтобы закрыть."
