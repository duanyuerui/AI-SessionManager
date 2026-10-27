# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、带控制台窗口）。

    python -m PyInstaller --noconfirm ClaudeSessionManager.spec
    # 产物：dist/ClaudeSessionManager.exe
"""
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(ROOT / "src" / "session_manager" / "webui"), "webui")],
    hiddenimports=["session_manager.core", "session_manager.server"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClaudeSessionManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
