"""
Resolve base directories for bundled (PyInstaller) vs. normal execution.

When frozen:
  BUNDLE_DIR  = sys._MEIPASS   (read-only resources: templates, static)
  DATA_DIR    = directory containing the .exe  (writable: db, logs, backups, labels)

When running from source:
  BUNDLE_DIR = DATA_DIR = directory containing this file (project root)
"""

import os
import sys


def _is_frozen():
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


# Read-only bundled assets (templates, static css/js)
BUNDLE_DIR = sys._MEIPASS if _is_frozen() else os.path.dirname(os.path.abspath(__file__))

# Writable user data (database, logs, backups, generated labels)
DATA_DIR = os.path.dirname(sys.executable) if _is_frozen() else os.path.dirname(os.path.abspath(__file__))
