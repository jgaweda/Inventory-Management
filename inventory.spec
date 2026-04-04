# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for HP Connectivity Team Inventory Management System.

Usage:
    pyinstaller inventory.spec

Produces a single-directory build in dist/InventorySystem/
"""

import os
import sys

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPEC))

# Ensure static/ exists (may be empty in CI)
os.makedirs(os.path.join(ROOT, 'static'), exist_ok=True)

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'templates'), 'templates'),
        (os.path.join(ROOT, 'static'), 'static'),
    ],
    hiddenimports=[
        'waitress',
        'PIL',
        'qrcode',
        'barcode',
        'barcode.codex',
        'openpyxl',
        'sqlite3',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test'],
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
    name='InventorySystem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,      # console app — shows the startup banner / URL
    icon=None,          # add icon=os.path.join(ROOT, 'static', 'icon.ico') if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='InventorySystem',
)
