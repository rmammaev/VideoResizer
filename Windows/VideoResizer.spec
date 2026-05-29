# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Video Resizer v4 — Windows 10/11
# Run from Windows: pyinstaller VideoResizer.spec --noconfirm --clean

import sys
from pathlib import Path

block_cipher = None

# Collect pywebview data files (WebView2 loader, JS bridges, etc.)
try:
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
    webview_datas   = collect_data_files('webview')
    webview_dynlibs = collect_dynamic_libs('webview')
except Exception:
    webview_datas   = []
    webview_dynlibs = []

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=webview_dynlibs,
    datas=webview_datas,
    hiddenimports=[
        # pywebview Windows backends
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'clr',
        # edge-tts
        'edge_tts',
        'aiohttp',
        'aiofiles',
        # pyttsx3 Windows
        'pyttsx3',
        'pyttsx3.drivers',
        'pyttsx3.drivers.sapi5',
        'win32com',
        'win32com.client',
        'pythoncom',
        'comtypes',
        # whisper (if bundled)
        'whisper',
        'torch',
        # Pillow
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        # standard
        'http.server',
        'queue',
        'threading',
        'subprocess',
        'json',
        'tempfile',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy.testing',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoResizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,       # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',     # Windows .ico (see setup script)
    version='version_info.txt',  # optional version resource
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoResizer',
)
