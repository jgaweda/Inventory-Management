# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for HP Connectivity Team Inventory Management System.

Usage:
    pyinstaller scripts/inventory.spec

Produces a single-directory build in dist/InventorySystem/
"""

import os
import sys

block_cipher = None
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

# Ensure static/ exists (may be empty in CI)
os.makedirs(os.path.join(ROOT, 'static'), exist_ok=True)

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'templates'), 'templates'),
        (os.path.join(ROOT, 'static'), 'static'),
        (os.path.join(ROOT, 'seed_data'), 'seed_data'),
        (os.path.join(ROOT, 'fonts'), 'fonts'),
        (os.path.join(ROOT, 'VERSION'), '.'),
        (os.path.join(ROOT, 'README.md'), '.'),
    ],
    hiddenimports=[
        'flask',
        'jinja2',
        'werkzeug',
        'PIL',
        'qrcode',
        'barcode',
        'barcode.codex',
        'openpyxl',
        'sqlite3',
        'pyzipper',
    ] + collect_submodules('waitress'),
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
    console=False,     # windowed mode — no console window on launch
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
