"""
Tests for the HP Connectivity Team Inventory Management System.

Shared test infrastructure: BaseTestCase, temp directory setup, and common imports.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

# Set up temp data dir before importing app
_test_dir = tempfile.mkdtemp()
os.environ['INVENTORY_DATA_DIR'] = _test_dir

import database as db
import barcode_utils
from app import app, ROLE_PERMISSIONS, GUEST_ASSIGNABLE_PERMISSIONS, has_permission, get_user_permissions


class BaseTestCase(unittest.TestCase):
    """Base class with test client and fresh database for each test."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.client = self.app.test_client()

        # Fresh database for each test
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        # Clean up any leftover seed_data from prior tests
        _seed = os.path.join(_test_dir, 'seed_data')
        if os.path.isdir(_seed):
            shutil.rmtree(_seed)
        # Patch BUNDLE_DIR so init_db() doesn't pick up real seed_data/
        self._bundle_patcher = patch('database.BUNDLE_DIR', _test_dir)
        self._bundle_patcher.start()
        db.init_db()
        self._bundle_patcher.stop()
        # Disable upload bundling in backups — avoids zipping real upload dirs
        config = db._get_backup_config()
        config['include_uploads'] = False
        db.save_backup_config(config)

    def tearDown(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)

    def login_admin(self):
        """Log in as default admin user."""
        return self.client.post('/login', data={
            'username': 'admin',
            'password': 'admin',
        }, follow_redirects=True)
