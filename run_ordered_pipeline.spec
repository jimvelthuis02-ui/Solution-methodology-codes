# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['Scripts/Pipeline/run_ordered_pipeline.py'],
    pathex=[],
    binaries=[],
    datas=[('Input files', 'Input files'), ('Output', 'Output'), ('Scripts', 'Scripts')],
    hiddenimports=['openpyxl', 'matplotlib', 'numpy', 'pandas', 'scipy', 'sklearn', 'sklearn.cluster', 'sklearn.metrics', 'sklearn.preprocessing', 'scipy.sparse'],
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
    [],
    exclude_binaries=True,
    name='run_ordered_pipeline',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='run_ordered_pipeline',
)
