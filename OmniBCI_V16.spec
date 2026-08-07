# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

hiddenimports = []
hiddenimports += collect_submodules('bleak')
hiddenimports += collect_submodules('serial')
hiddenimports += collect_submodules('pyqtgraph')

# pyqtgraph ships a few runtime resources which some code paths discover
# dynamically. PySide6 and scipy have official PyInstaller hooks.
datas = [('assets', 'assets')]
try:
    datas += collect_data_files('pyqtgraph')
except Exception:
    pass

a = Analysis(
    ['ads1299_eeg_gui_native.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Export-only dependencies are deliberately not bundled into the live
        # acquisition executable. This keeps the Windows build smaller and
        # avoids pyEDFlib wheel/compiler differences across Python versions.
        'mne', 'pyedflib',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OmniBCI_V16',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/omni_logo_mark.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OmniBCI_V16',
)
