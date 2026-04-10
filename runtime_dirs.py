"""
Resolve base directories for bundled (PyInstaller) vs. normal execution.

When frozen:
  BUNDLE_DIR  = sys._MEIPASS   (read-only resources: templates, static)
  DATA_DIR    = directory containing the .exe  (writable: db, logs, backups, labels)

When running from source:
  BUNDLE_DIR = DATA_DIR = directory containing this file (project root)
"""

import os
import shutil
import sys


def _is_frozen():
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


# Read-only bundled assets (templates, static css/js)
BUNDLE_DIR = sys._MEIPASS if _is_frozen() else os.path.dirname(os.path.abspath(__file__))

# Writable user data (database, logs, backups, generated labels)
DATA_DIR = os.path.dirname(sys.executable) if _is_frozen() else os.path.dirname(os.path.abspath(__file__))


def _resolve_git_executable():
    """Return path to git executable. Prefers bundled MinGit (shipped
    alongside the .exe in dist/InventorySystem/git/cmd/git.exe), falls
    back to system git on PATH, or None if neither is available."""
    # When frozen, git is shipped next to the .exe in a 'git' subfolder
    if _is_frozen():
        exe_dir = os.path.dirname(sys.executable)
        # Walk up from the exe to the COLLECT root (InventorySystem/)
        candidates = [
            os.path.join(exe_dir, 'git', 'cmd', 'git.exe'),
            os.path.join(exe_dir, 'git', 'bin', 'git.exe'),
            os.path.join(exe_dir, '_internal', 'git', 'cmd', 'git.exe'),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
    # Fall back to system git
    return shutil.which('git')


GIT_EXECUTABLE = _resolve_git_executable() or 'git'
