# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['receiver.py'],
    pathex=[],
    binaries=[('C:\\Python38\\lib\\site-packages\\pyzbar\\libzbar-64.dll', '.'), ('C:\\Python38\\lib\\site-packages\\pyzbar\\libiconv.dll', '.')],
    datas=[],
    hiddenimports=['pyzbar.pyzbar', 'minio', 'pymysql'],
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
    name='QR_Receiver',
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
