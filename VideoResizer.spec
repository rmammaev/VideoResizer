# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec для VideoResizer.app v4 (pywebview, нативный WebKit)

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files("webview")

hiddenimports = []
hiddenimports += collect_submodules("webview")
hiddenimports += [
    "AppKit", "Foundation", "WebKit", "CoreFoundation",
    "objc", "PyObjCTools",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "customtkinter", "tkinterdnd2"],
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VideoResizer",
    debug=False,
    strip=False, upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name="VideoResizer",
)

app = BUNDLE(
    coll,
    name="VideoResizer.app",
    icon="icon.icns" if os.path.exists("icon.icns") else None,
    bundle_identifier="com.video.resizer",
    info_plist={
        "CFBundleName": "Video Resizer",
        "CFBundleDisplayName": "Video Resizer",
        "CFBundleVersion": "4.0.0",
        "CFBundleShortVersionString": "4.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSRequiresAquaSystemAppearance": False,
    },
)
