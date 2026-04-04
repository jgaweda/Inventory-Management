"""
Tests for the HP Connectivity Team Inventory Management System.

Covers: barcode generation, device CRUD, serial duplicate detection,
label caching, backup system, auth/rate limiting, health endpoint,
product reference inline edit, and the API.
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
from app import app


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
        db.init_db()

    def tearDown(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)

    def login_admin(self):
        """Log in as default admin user."""
        return self.client.post('/login', data={
            'username': 'admin',
            'password': 'admin',
        }, follow_redirects=True)


class TestBarcodeGeneration(BaseTestCase):
    """Test base-36 barcode generation with CNX- prefix."""

    def test_int_to_base36_basic(self):
        self.assertEqual(db._int_to_base36(0), '0')
        self.assertEqual(db._int_to_base36(1), '1')
        self.assertEqual(db._int_to_base36(10), 'A')
        self.assertEqual(db._int_to_base36(35), 'Z')
        self.assertEqual(db._int_to_base36(36), '10')

    def test_base36_roundtrip(self):
        for n in [0, 1, 10, 35, 36, 100, 999, 1296, 46655]:
            encoded = db._int_to_base36(n)
            decoded = db._base36_to_int(encoded)
            self.assertEqual(decoded, n, f'Round-trip failed for {n}: encoded={encoded}')

    def test_barcode_has_cnx_prefix(self):
        device_id = db.add_device({'name': 'Test Device'})
        device = db.get_device(device_id)
        self.assertTrue(device['barcode_value'].startswith('CNX-'),
                        f'Expected CNX- prefix, got {device["barcode_value"]}')

    def test_barcodes_are_sequential(self):
        ids = []
        for i in range(5):
            device_id = db.add_device({'name': f'Device {i}'})
            device = db.get_device(device_id)
            ids.append(device['barcode_value'])

        # Extract numbers after prefix
        nums = [db._base36_to_int(v.replace('CNX-', '')) for v in ids]
        for i in range(1, len(nums)):
            self.assertEqual(nums[i], nums[i-1] + 1,
                             f'Barcodes not sequential: {ids}')

    def test_barcode_no_duplicates(self):
        barcodes = set()
        for i in range(20):
            device_id = db.add_device({'name': f'Device {i}'})
            device = db.get_device(device_id)
            self.assertNotIn(device['barcode_value'], barcodes,
                             f'Duplicate barcode: {device["barcode_value"]}')
            barcodes.add(device['barcode_value'])

    def test_sequence_table_created(self):
        db.add_device({'name': 'Test'})
        conn = sqlite3.connect(db.DB_PATH)
        row = conn.execute('SELECT next_val FROM barcode_seq WHERE id = 1').fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], 1)


class TestDeviceCRUD(BaseTestCase):
    """Test device create, read, update, and lookup."""

    def test_add_and_get_device(self):
        device_id = db.add_device({'name': 'HP LaserJet', 'category': 'Printer'})
        device = db.get_device(device_id)
        self.assertIsNotNone(device)
        self.assertEqual(device['name'], 'HP LaserJet')
        self.assertEqual(device['category'], 'Printer')

    def test_update_device(self):
        device_id = db.add_device({'name': 'Old Name'})
        db.update_device(device_id, {'name': 'New Name'})
        device = db.get_device(device_id)
        self.assertEqual(device['name'], 'New Name')

    def test_get_device_by_barcode(self):
        device_id = db.add_device({'name': 'Scanner Test'})
        device = db.get_device(device_id)
        found = db.get_device_by_barcode(device['barcode_value'])
        self.assertIsNotNone(found)
        self.assertEqual(found['device_id'], device_id)

    def test_get_device_by_barcode_case_insensitive(self):
        device_id = db.add_device({'name': 'Case Test'})
        device = db.get_device(device_id)
        found = db.get_device_by_barcode(device['barcode_value'].lower())
        self.assertIsNotNone(found)

    def test_get_device_not_found(self):
        self.assertIsNone(db.get_device('nonexistent'))

    def test_get_device_by_barcode_not_found(self):
        self.assertIsNone(db.get_device_by_barcode('DOESNOTEXIST'))


class TestDuplicateSerialDetection(BaseTestCase):
    """Test duplicate serial number detection."""

    def test_get_device_by_serial(self):
        db.add_device({'name': 'Printer A', 'serial_number': 'SN12345'})
        found = db.get_device_by_serial('SN12345')
        self.assertIsNotNone(found)
        self.assertEqual(found['name'], 'Printer A')

    def test_get_device_by_serial_case_insensitive(self):
        db.add_device({'name': 'Printer B', 'serial_number': 'ABC123'})
        found = db.get_device_by_serial('abc123')
        self.assertIsNotNone(found)

    def test_retired_device_serial_not_found(self):
        device_id = db.add_device({'name': 'Printer C', 'serial_number': 'RET001'})
        db.retire_device(device_id)
        found = db.get_device_by_serial('RET001')
        self.assertIsNone(found)

    def test_duplicate_serial_blocked_in_ui(self):
        self.login_admin()
        # Add first device
        self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'model_number': 'LJ100',
            'category': 'Router', 'serial_number': 'UNIQUE001',
        }, follow_redirects=True)
        # Try adding duplicate
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'model_number': 'LJ200',
            'category': 'Router', 'serial_number': 'UNIQUE001',
        }, follow_redirects=True)
        self.assertIn(b'already exists', resp.data)


class TestLabelGeneration(BaseTestCase):
    """Test label PNG and barcode rendering."""

    def test_label_creates_png(self):
        device_id = db.add_device({'name': 'Label Test'})
        device = db.get_device(device_id)
        path = barcode_utils.generate_label(device_id, device['barcode_value'], device['name'])
        self.assertTrue(os.path.isfile(path))

    def test_label_dimensions(self):
        device_id = db.add_device({'name': 'Dim Test'})
        device = db.get_device(device_id)
        img = barcode_utils.generate_label(device_id, device['barcode_value'], device['name'], save=False)
        self.assertEqual(img.size, (1050, 450))

    def test_barcode_image_crisp(self):
        """Barcode should have 0% gray pixels (crisp bars)."""
        img = barcode_utils.generate_barcode_image('CNX-1', width=350, height=80)
        pixels = list(img.getdata())
        gray_count = sum(1 for r, g, b in pixels if 30 < r < 220)
        gray_pct = gray_count / len(pixels) * 100
        self.assertLess(gray_pct, 1.0, f'Barcode has {gray_pct:.1f}% gray pixels')

    def test_qr_code_crisp(self):
        """QR code should have 0% gray pixels."""
        img = barcode_utils.generate_qr_code('CNX-1', size=200)
        pixels = list(img.getdata())
        gray_count = sum(1 for r, g, b in pixels if 30 < r < 220)
        gray_pct = gray_count / len(pixels) * 100
        self.assertLess(gray_pct, 1.0, f'QR has {gray_pct:.1f}% gray pixels')


class TestBackupSystem(BaseTestCase):
    """Test backup, restore, and skip-if-unchanged."""

    def test_manual_backup(self):
        result = db.backup_database(performed_by='test', manual=True)
        self.assertFalse(result['skipped'])
        self.assertTrue(os.path.isfile(result['path']))

    def test_skip_if_unchanged(self):
        db.backup_database(performed_by='test', manual=True)
        result = db.backup_database(performed_by='test', manual=False)
        self.assertTrue(result['skipped'])

    def test_restore_creates_safety_backup(self):
        result = db.backup_database(performed_by='test', manual=True)
        restore_result = db.restore_database(result['filename'])
        self.assertIn('safety_backup', restore_result)
        self.assertTrue(os.path.isfile(
            os.path.join(db._get_backup_dir(), restore_result['safety_backup'])))

    def test_integrity_check(self):
        result = db.check_database_integrity()
        self.assertTrue(result['ok'])

    def test_default_backup_config(self):
        defaults = db.get_default_backup_config()
        self.assertEqual(defaults['backup_interval_hours'], 4)
        self.assertEqual(defaults['max_backups'], 10)
        self.assertIn('last_backup_hash', defaults)


class TestAuthAndRateLimiting(BaseTestCase):
    """Test login, auth decorators, and rate limiting."""

    def test_login_success(self):
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'admin'
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_login_failure(self):
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'wrong'
        }, follow_redirects=True)
        self.assertIn(b'Invalid username or password', resp.data)

    def test_auth_required_redirect(self):
        resp = self.client.get('/devices/add')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_rate_limiting(self):
        from app import _login_attempts
        _login_attempts.clear()
        # Exhaust rate limit
        for _ in range(10):
            self.client.post('/login', data={
                'username': 'admin', 'password': 'wrong'
            })
        # 11th should be rate limited
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'wrong'
        }, follow_redirects=True)
        self.assertIn(b'Too many login attempts', resp.data)
        _login_attempts.clear()

    def test_open_redirect_blocked(self):
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'admin',
            'next': 'https://evil.com',
        }, follow_redirects=False)
        self.assertNotIn('evil.com', resp.headers.get('Location', ''))


class TestHealthEndpoint(BaseTestCase):
    """Test /health endpoint."""

    def test_health_ok(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['db'], 'ok')


class TestProductReferenceAPI(BaseTestCase):
    """Test inline edit API for product references."""

    def test_inline_edit_requires_admin(self):
        resp = self.client.patch('/api/reference/1',
                                 data=json.dumps({'codename': 'Test'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 302)  # redirect to login

    def test_inline_edit_updates_field(self):
        self.login_admin()
        # Add a product reference first
        db.add_product_reference(codename='TestProduct', model_name='Old Model')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']

        resp = self.client.patch(f'/api/reference/{ref_id}',
                                 data=json.dumps({'model_name': 'New Model'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['ok'])

        # Verify update
        ref = db.get_product_reference(ref_id)
        self.assertEqual(ref['model_name'], 'New Model')

    def test_inline_edit_rejects_empty_codename(self):
        self.login_admin()
        db.add_product_reference(codename='TestProd')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']

        resp = self.client.patch(f'/api/reference/{ref_id}',
                                 data=json.dumps({'codename': ''}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class TestPublicRoutes(BaseTestCase):
    """Test that public routes work without auth."""

    def test_dashboard(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)

    def test_device_list(self):
        resp = self.client.get('/devices')
        self.assertEqual(resp.status_code, 200)

    def test_scan_page(self):
        resp = self.client.get('/scan')
        self.assertEqual(resp.status_code, 200)

    def test_api_lookup_empty(self):
        resp = self.client.get('/api/lookup?barcode=')
        self.assertEqual(resp.status_code, 400)

    def test_api_lookup_not_found(self):
        resp = self.client.get('/api/lookup?barcode=NOPE')
        self.assertEqual(resp.status_code, 404)

    def test_api_lookup_found(self):
        device_id = db.add_device({'name': 'Lookup Test'})
        device = db.get_device(device_id)
        resp = self.client.get(f'/api/lookup?barcode={device["barcode_value"]}')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['found'])

    def test_product_reference_list(self):
        resp = self.client.get('/reference')
        self.assertEqual(resp.status_code, 200)

    def test_app_logs_requires_login(self):
        resp = self.client.get('/logs')
        self.assertEqual(resp.status_code, 302)


class TestLogPagination(BaseTestCase):
    """Test audit log pagination."""

    def test_log_page_parameter(self):
        self.login_admin()
        resp = self.client.get('/logs?page=1')
        self.assertEqual(resp.status_code, 200)

    def test_log_invalid_page(self):
        self.login_admin()
        resp = self.client.get('/logs?page=-1')
        self.assertEqual(resp.status_code, 200)  # clamps to page 1


if __name__ == '__main__':
    unittest.main()
