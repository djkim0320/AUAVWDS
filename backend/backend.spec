# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

block_cipher = None

casadi_datas, casadi_binaries, casadi_hiddenimports = collect_all('casadi')
neuralfoil_datas, neuralfoil_binaries, neuralfoil_hiddenimports = collect_all('neuralfoil')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=casadi_binaries + neuralfoil_binaries,
    datas=casadi_datas + neuralfoil_datas,
    hiddenimports=[
        'numpy',
        'uvicorn',
        'fastapi',
        'pydantic',
        'requests',
        'neuralfoil',
        *casadi_hiddenimports,
        *neuralfoil_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='backend',
)


