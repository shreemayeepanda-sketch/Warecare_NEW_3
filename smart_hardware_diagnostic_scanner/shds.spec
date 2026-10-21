# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the single-file GUI (onefile, console kept).

The console stays on purpose: the same binary drives the documented CLI
(`--scan --quiet ...`) that the scheduled-scan feature registers in Task
Scheduler / cron. For a no-console desktop run, use `Open Smart Hardware
Scanner.bat` with a normal Python install, or build a second spec with
console=False (the CLI would then have no visible output).

Optional heavyweight deps are excluded on purpose: without matplotlib the
chart area degrades to its documented placeholder, keeping the binary lean.
"""
from __future__ import annotations

a = Analysis(
    ["shds_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "PIL", "tkinter.test", "plyer"],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="SHDS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
