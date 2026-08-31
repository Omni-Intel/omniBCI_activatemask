# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

bundle_exports = os.environ.get('OMNIBCI_BUNDLE_EXPORTS', '1') == '1'

hiddenimports = []
hiddenimports += collect_submodules('bleak')
hiddenimports += collect_submodules('serial')

datas = [('assets', 'assets')]
if bundle_exports:
    # MNE resolves most APIs lazily through .pyi files; static imports miss them.
    hiddenimports += collect_submodules('mne')
    datas += collect_data_files('mne')
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
    excludes=[] if bundle_exports else ['mne', 'pyedflib'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OmniBCI_V19',
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
    name='OmniBCI_V19',
)
