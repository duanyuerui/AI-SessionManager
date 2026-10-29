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
    hiddenimports=[
        "session_manager.core", "session_manager.server",
        "session_manager.usage", "session_manager.zstd",
        "session_manager.providers",
        "session_manager.providers.claude",
        "session_manager.providers.codex",
        "session_manager.providers.dsh",
        "session_manager.providers.generic",
        "session_manager.providers.atomcode",
        "session_manager.providers.opensquilla",
        "session_manager.providers.catalog",
        "session_manager.providers.discovery",
        "session_manager.providers.workbuddy",
        "session_manager.providers.zcode",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 1) 控制台版：保留黑色窗口，启动后开默认浏览器（老用法）
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

# 2) 客户端版：没有控制台窗口，默认用 Chrome/Edge 的 --app 模式开独立窗口
app_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AISessionManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
