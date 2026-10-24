# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = ['PIL', 'psutil']
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# The standalone build imports the shared package dynamically (see the
# crashlog/metrics shims in ewarecare.py: sys.path insert + `from scanner
# import ...`), so PyInstaller's static analysis cannot see those imports
# from this source layout -- crashlog and metrics_db would silently stay
# missing from the exe. Make the package importable at spec-execution time
# and pull its submodules in explicitly.
_PKG = str(Path('smart_hardware_diagnostic_scanner'))  # repo-relative at spec exec
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)
hiddenimports += collect_submodules('scanner')


a = Analysis(
    ['ewarecare.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ewarecare',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
