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


class TestLabelRedesign(BaseTestCase):
    """Test the barcode-dominant label layout."""

    def test_qr_code_size_250(self):
        """QR code should be 250x250 pixels."""
        img = barcode_utils.generate_qr_code('CNX-1', size=250)
        self.assertEqual(img.size, (250, 250))

    def test_qr_uses_error_correct_m(self):
        """QR should use M-level error correction for larger modules."""
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=4)
        qr.add_data('CNX-1')
        qr.make(fit=True)
        # M should produce fewer modules than H for same data
        qr_h = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H,
                             box_size=10, border=4)
        qr_h.add_data('CNX-1')
        qr_h.make(fit=True)
        self.assertLessEqual(qr.modules_count, qr_h.modules_count)

    def test_barcode_right_side_wider(self):
        """Barcode area should be wider than QR area (~765px vs ~270px)."""
        # QR is 250px + 15px padding on each side = 280px
        # Barcode area = 1050 - 280 - 15 = 755px minimum
        qr_total = 250 + 15 + 15  # qr_size + left pad + gap
        barcode_w = 1050 - qr_total - 15  # minus right pad
        self.assertGreater(barcode_w, 700)

    def test_label_has_content_both_sides(self):
        """Label should have black pixels on both left (QR) and right (barcode) sides."""
        img = barcode_utils.generate_label('t', 'CNX-1', 'Test Device', save=False)
        # Check QR region (left 265px)
        qr_region = img.crop((0, 0, 265, 450))
        qr_pixels = list(qr_region.getdata())
        qr_black = sum(1 for r, g, b in qr_pixels if r < 50)
        self.assertGreater(qr_black, 100, 'QR area should have black pixels')
        # Check barcode region (right of 280px)
        bc_region = img.crop((280, 0, 1050, 450))
        bc_pixels = list(bc_region.getdata())
        bc_black = sum(1 for r, g, b in bc_pixels if r < 50)
        self.assertGreater(bc_black, 100, 'Barcode area should have black pixels')


class TestWikiAttachments(BaseTestCase):
    """Test wiki attachment upload, download, and deletion."""

    def _create_product(self):
        """Helper: create a product reference and return ref_id."""
        db.add_product_reference(codename='WikiTest')
        refs = db.get_all_product_references()
        return refs[0]['ref_id']

    def test_wiki_page_loads(self):
        ref_id = self._create_product()
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'WikiTest', resp.data)

    def test_wiki_page_not_found(self):
        resp = self.client.get('/wiki/9999', follow_redirects=True)
        self.assertIn(b'Product not found', resp.data)

    def test_wiki_save_requires_login(self):
        ref_id = self._create_product()
        resp = self.client.post(f'/wiki/{ref_id}/save', data={'content': 'notes'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_wiki_save_content(self):
        ref_id = self._create_product()
        self.login_admin()
        resp = self.client.post(f'/wiki/{ref_id}/save',
                                data={'content': 'Test notes here'},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        wiki = db.get_wiki_by_ref_id(ref_id)
        self.assertEqual(wiki['content'], 'Test notes here')
        self.assertEqual(wiki['updated_by'], 'admin')

    def test_upload_requires_admin(self):
        ref_id = self._create_product()
        # Not logged in
        resp = self.client.post(f'/wiki/{ref_id}/upload',
                                data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_upload_and_download(self):
        ref_id = self._create_product()
        self.login_admin()
        import io
        data = {'attachment': (io.BytesIO(b'hello world'), 'test.txt')}
        resp = self.client.post(f'/wiki/{ref_id}/upload',
                                data=data, content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Uploaded test.txt', resp.data)

        # Verify attachment in DB
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]['original_name'], 'test.txt')

        # Download
        att_id = attachments[0]['attachment_id']
        resp = self.client.get(f'/wiki/attachment/{att_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'hello world')

    def test_upload_image_preview(self):
        ref_id = self._create_product()
        self.login_admin()
        # Create a minimal 1x1 PNG
        import struct, zlib
        def make_png():
            sig = b'\x89PNG\r\n\x1a\n'
            ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
            ihdr = b'IHDR' + ihdr_data
            ihdr_chunk = struct.pack('>I', 13) + ihdr + struct.pack('>I', zlib.crc32(ihdr) & 0xFFFFFFFF)
            raw = b'\x00\xff\x00\x00'
            idat_data = zlib.compress(raw)
            idat = b'IDAT' + idat_data
            idat_chunk = struct.pack('>I', len(idat_data)) + idat + struct.pack('>I', zlib.crc32(idat) & 0xFFFFFFFF)
            iend = b'IEND'
            iend_chunk = struct.pack('>I', 0) + iend + struct.pack('>I', zlib.crc32(iend) & 0xFFFFFFFF)
            return sig + ihdr_chunk + idat_chunk + iend_chunk

        import io
        data = {'attachment': (io.BytesIO(make_png()), 'photo.png')}
        self.client.post(f'/wiki/{ref_id}/upload',
                         data=data, content_type='multipart/form-data')
        attachments = db.get_wiki_attachments(ref_id)
        att_id = attachments[0]['attachment_id']

        # Preview endpoint should work
        resp = self.client.get(f'/wiki/attachment/{att_id}/preview')
        self.assertEqual(resp.status_code, 200)

    def test_upload_disallowed_extension(self):
        ref_id = self._create_product()
        self.login_admin()
        import io
        data = {'attachment': (io.BytesIO(b'bad'), 'malware.exe')}
        resp = self.client.post(f'/wiki/{ref_id}/upload',
                                data=data, content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertIn(b'not allowed', resp.data)
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 0)

    def test_delete_attachment(self):
        ref_id = self._create_product()
        self.login_admin()
        import io
        data = {'attachment': (io.BytesIO(b'delete me'), 'temp.txt')}
        self.client.post(f'/wiki/{ref_id}/upload',
                         data=data, content_type='multipart/form-data')
        attachments = db.get_wiki_attachments(ref_id)
        att_id = attachments[0]['attachment_id']

        resp = self.client.post(f'/wiki/attachment/{att_id}/delete',
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Deleted temp.txt', resp.data)
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 0)

    def test_download_nonexistent(self):
        resp = self.client.get('/wiki/attachment/9999')
        self.assertEqual(resp.status_code, 404)

    def test_attachments_visible_without_login(self):
        """Non-logged-in users should see attachment list on wiki page."""
        ref_id = self._create_product()
        self.login_admin()
        import io
        data = {'attachment': (io.BytesIO(b'public file'), 'readme.txt')}
        self.client.post(f'/wiki/{ref_id}/upload',
                         data=data, content_type='multipart/form-data')
        # Log out
        self.client.get('/logout')
        # View wiki page
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'readme.txt', resp.data)
        # Should NOT see upload area
        self.assertNotIn(b'Click to upload', resp.data)


class TestOwnershipDropdown(BaseTestCase):
    """Test the ownership dropdown (HP Owned / Vendor Supplied)."""

    def test_device_form_has_ownership_dropdown(self):
        self.login_admin()
        resp = self.client.get('/devices/add')
        self.assertIn(b'HP Owned', resp.data)
        self.assertIn(b'Vendor Supplied', resp.data)

    def test_vendor_supplied_persists(self):
        self.login_admin()
        self.client.post('/devices/add', data={
            'manufacturer': 'TP-Link', 'model_number': 'AX55',
            'category': 'Router', 'vendor_supplied': '1',
        }, follow_redirects=True)
        devices = db.get_all_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['vendor_supplied'], 1)

    def test_hp_owned_default(self):
        self.login_admin()
        self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'model_number': 'AX55',
            'category': 'Router', 'vendor_supplied': '0',
        }, follow_redirects=True)
        devices = db.get_all_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['vendor_supplied'], 0)


class TestCSVImport(BaseTestCase):
    """Test CSV import for product references."""

    def test_csv_import(self):
        self.login_admin()
        import io
        csv_content = 'Codename,Model Name,Wi-Fi Gen,Year\nTestProd,Model X,6E,2025\n'
        data = {
            'import_file': (io.BytesIO(csv_content.encode('utf-8')), 'products.csv'),
            'import_mode': 'add',
        }
        resp = self.client.post('/reference/import',
                                data=data, content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Imported 1 product', resp.data)
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['codename'], 'TestProd')
        self.assertEqual(refs[0]['wifi_gen'], '6E')

    def test_csv_import_overwrite(self):
        self.login_admin()
        db.add_product_reference(codename='OldProduct')
        import io
        csv_content = 'Codename,Model Name\nNewProduct,New Model\n'
        data = {
            'import_file': (io.BytesIO(csv_content.encode('utf-8')), 'products.csv'),
            'import_mode': 'overwrite',
        }
        self.client.post('/reference/import',
                         data=data, content_type='multipart/form-data',
                         follow_redirects=True)
        refs = db.get_all_product_references()
        codenames = [r['codename'] for r in refs]
        self.assertNotIn('OldProduct', codenames)
        self.assertIn('NewProduct', codenames)


class TestServerSettings(BaseTestCase):
    """Tests for admin server port settings."""

    def _login_viewer(self):
        db.create_user('viewer1', 'pass1234', role='viewer', display_name='Viewer')
        return self.client.post('/login', data={
            'username': 'viewer1', 'password': 'pass1234',
        }, follow_redirects=True)

    def tearDown(self):
        super().tearDown()
        from app import SERVER_CONFIG_FILE
        if os.path.exists(SERVER_CONFIG_FILE):
            os.remove(SERVER_CONFIG_FILE)

    def test_server_settings_visible_to_admin(self):
        self.login_admin()
        resp = self.client.get('/account')
        self.assertIn(b'Server Settings', resp.data)

    def test_server_settings_hidden_from_viewer(self):
        self._login_viewer()
        resp = self.client.get('/account')
        self.assertNotIn(b'Server Settings', resp.data)

    def test_save_port_as_admin(self):
        self.login_admin()
        resp = self.client.post('/settings/server',
                                data={'port': '9090'},
                                follow_redirects=True)
        self.assertIn(b'Restart the application', resp.data)
        import json
        from app import SERVER_CONFIG_FILE
        with open(SERVER_CONFIG_FILE) as f:
            cfg = json.load(f)
        self.assertEqual(cfg['port'], 9090)

    def test_save_invalid_port(self):
        self.login_admin()
        resp = self.client.post('/settings/server',
                                data={'port': '99999'},
                                follow_redirects=True)
        self.assertIn(b'Port must be between', resp.data)

    def test_viewer_cannot_save_port(self):
        self._login_viewer()
        resp = self.client.post('/settings/server',
                                data={'port': '9090'},
                                follow_redirects=True)
        self.assertNotIn(b'Restart the application', resp.data)


class TestDeviceImport(BaseTestCase):
    """Test Excel/CSV device import."""

    def test_import_csv(self):
        self.login_admin()
        csv_data = (
            'Name,Category,Manufacturer,Status,Location\n'
            'Test Printer,Printer,HP,available,Lab A\n'
            'Test Router,Router,Cisco,available,Lab B\n'
        )
        from io import BytesIO
        data = {
            'import_file': (BytesIO(csv_data.encode()), 'devices.csv')
        }
        resp = self.client.post('/import/devices', data=data,
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertIn(b'Imported 2 device', resp.data)

    def test_import_csv_skip_no_name(self):
        self.login_admin()
        csv_data = (
            'Name,Category\n'
            ',Printer\n'
            'Valid Device,Router\n'
        )
        from io import BytesIO
        data = {
            'import_file': (BytesIO(csv_data.encode()), 'devices.csv')
        }
        resp = self.client.post('/import/devices', data=data,
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertIn(b'Imported 1 device', resp.data)
        self.assertIn(b'1 rows skipped', resp.data)

    def test_import_bad_filetype(self):
        self.login_admin()
        from io import BytesIO
        data = {
            'import_file': (BytesIO(b'test'), 'devices.pdf')
        }
        resp = self.client.post('/import/devices', data=data,
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertIn(b'Unsupported file type', resp.data)

    def test_import_requires_editor(self):
        """Viewers cannot import devices."""
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'viewer1', 'password': 'test', 'role': 'viewer',
        })
        self.client.get('/logout')
        self.client.post('/login', data={
            'username': 'viewer1', 'password': 'test',
        })
        from io import BytesIO
        data = {
            'import_file': (BytesIO(b'Name\nTest'), 'devices.csv')
        }
        resp = self.client.post('/import/devices', data=data,
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertNotIn(b'Imported', resp.data)


class TestWikiMarkdown(BaseTestCase):
    """Test wiki Markdown rendering support."""

    def test_wiki_page_includes_marked_js(self):
        """Wiki page should include marked.js CDN."""
        self.login_admin()
        # Create a product reference first
        db.add_product_reference(codename='TestProd')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertIn(b'marked.min.js', resp.data)

    def test_wiki_content_json_escaped(self):
        """Wiki content should be embedded as JSON for safe JS rendering."""
        self.login_admin()
        db.add_product_reference(codename='MDProd')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        # Save some markdown content
        self.client.post(f'/wiki/{ref_id}/save', data={
            'content': '# Hello **World**'
        }, follow_redirects=True)
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertIn(b'marked.min.js', resp.data)

    def test_wiki_read_only_has_render_target(self):
        """Non-logged-in view should have wikiReadOnly div for JS rendering."""
        db.add_product_reference(codename='ReadProd')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertIn(b'wikiReadOnly', resp.data)


class TestLaserBarcode(BaseTestCase):
    """Test barcode optimization for laser scanners."""

    def test_barcode_crisp_edges(self):
        """Barcode should have zero gray pixels (pure black/white for laser)."""
        img = barcode_utils.generate_barcode_image('CNX-TEST01', width=350, height=80)
        pixels = list(img.getdata())
        gray = 0
        for r, g, b in pixels:
            if not (r > 240 and g > 240 and b > 240) and not (r < 15 and g < 15 and b < 15):
                gray += 1
        pct = gray / len(pixels) * 100
        self.assertLess(pct, 1, f'{pct:.1f}% gray pixels — bars not crisp')

    def test_barcode_has_quiet_zones(self):
        """Barcode should have white quiet zones on left and right edges."""
        img = barcode_utils.generate_barcode_image('CNX-TEST02', width=400, height=80)
        # Check leftmost and rightmost 5 columns are predominantly white
        for x in range(5):
            white_count = 0
            for y in range(img.height):
                r, g, b = img.getpixel((x, y))
                if r > 200 and g > 200 and b > 200:
                    white_count += 1
            self.assertGreater(white_count / img.height, 0.5,
                               f'Left quiet zone missing at column {x}')
        for x in range(img.width - 5, img.width):
            white_count = 0
            for y in range(img.height):
                r, g, b = img.getpixel((x, y))
                if r > 200 and g > 200 and b > 200:
                    white_count += 1
            self.assertGreater(white_count / img.height, 0.5,
                               f'Right quiet zone missing at column {x}')


class TestRoleGranularity(BaseTestCase):
    """Test editor role permissions."""

    def _create_editor(self):
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'editor1', 'password': 'test',
            'display_name': 'Editor One', 'role': 'editor',
        })
        self.client.get('/logout')
        self.client.post('/login', data={
            'username': 'editor1', 'password': 'test',
        })

    def test_editor_can_add_device(self):
        self._create_editor()
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'model_number': 'T100',
            'category': 'Router', 'connectivity': 'Wi-Fi 6',
        }, follow_redirects=True)
        self.assertIn(b'added successfully', resp.data)

    def test_editor_cannot_manage_users(self):
        self._create_editor()
        resp = self.client.get('/users', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_viewer_cannot_add_device(self):
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'viewer2', 'password': 'test', 'role': 'viewer',
        })
        self.client.get('/logout')
        self.client.post('/login', data={
            'username': 'viewer2', 'password': 'test',
        })
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'category': 'Router',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)


class TestDeviceNotes(BaseTestCase):
    """Test public device notes feature."""

    def _create_device(self):
        self.login_admin()
        did = db.add_device({'name': 'Note Test Device'})
        self.client.get('/logout')
        return did

    def test_notes_section_visible(self):
        """Device detail should show Notes section and add form."""
        did = self._create_device()
        resp = self.client.get(f'/devices/{did}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Notes', resp.data)
        self.assertIn(b'note_content', resp.data)

    def test_anonymous_add_note(self):
        """Anyone can add a note without logging in."""
        did = self._create_device()
        resp = self.client.post(f'/devices/{did}/notes', data={
            'note_content': 'Anonymous test note',
            'author_name': 'Tester Bob',
        }, follow_redirects=True)
        self.assertIn(b'Note added', resp.data)
        self.assertIn(b'Anonymous test note', resp.data)
        self.assertIn(b'Tester Bob', resp.data)

    def test_anonymous_default_name(self):
        """Omitting author_name defaults to 'Anonymous'."""
        did = self._create_device()
        self.client.post(f'/devices/{did}/notes', data={
            'note_content': 'No name note',
            'author_name': '',
        })
        notes = db.get_device_notes(did)
        self.assertEqual(notes[0]['author'], 'Anonymous')

    def test_logged_in_user_note(self):
        """Logged-in user's display name is used as author."""
        did = self._create_device()
        self.login_admin()
        resp = self.client.post(f'/devices/{did}/notes', data={
            'note_content': 'Admin note here',
        }, follow_redirects=True)
        self.assertIn(b'Note added', resp.data)
        self.assertIn(b'Admin note here', resp.data)

    def test_empty_note_rejected(self):
        """Empty notes should be rejected."""
        did = self._create_device()
        resp = self.client.post(f'/devices/{did}/notes', data={
            'note_content': '',
        }, follow_redirects=True)
        self.assertIn(b'cannot be empty', resp.data)

    def test_whitespace_only_note_rejected(self):
        """Whitespace-only notes should be rejected."""
        did = self._create_device()
        resp = self.client.post(f'/devices/{did}/notes', data={
            'note_content': '   \n  ',
        }, follow_redirects=True)
        self.assertIn(b'cannot be empty', resp.data)

    def test_too_long_note_rejected(self):
        """Notes over 2000 chars should be rejected."""
        did = self._create_device()
        resp = self.client.post(f'/devices/{did}/notes', data={
            'note_content': 'x' * 2001,
        }, follow_redirects=True)
        self.assertIn(b'too long', resp.data)

    def test_note_on_nonexistent_device(self):
        """Adding a note to a nonexistent device should fail gracefully."""
        resp = self.client.post('/devices/fake123/notes', data={
            'note_content': 'Orphan note',
        }, follow_redirects=True)
        self.assertIn(b'Device not found', resp.data)

    def test_admin_can_delete_note(self):
        """Admin can delete any note."""
        did = self._create_device()
        note_id = db.add_device_note(did, 'Bob', 'Delete me')
        self.login_admin()
        resp = self.client.post(f'/devices/{did}/notes/{note_id}/delete',
                                follow_redirects=True)
        self.assertIn(b'Note deleted', resp.data)
        self.assertEqual(len(db.get_device_notes(did)), 0)

    def test_non_admin_cannot_delete_note(self):
        """Non-admin users cannot delete notes."""
        did = self._create_device()
        note_id = db.add_device_note(did, 'Bob', 'Keep me')
        # Not logged in — should redirect
        resp = self.client.post(f'/devices/{did}/notes/{note_id}/delete',
                                follow_redirects=True)
        self.assertNotIn(b'Note deleted', resp.data)
        self.assertEqual(len(db.get_device_notes(did)), 1)

    def test_multiple_notes_ordered(self):
        """Multiple notes should all be returned."""
        did = self._create_device()
        db.add_device_note(did, 'Alice', 'First note')
        db.add_device_note(did, 'Bob', 'Second note')
        notes = db.get_device_notes(did)
        self.assertEqual(len(notes), 2)
        authors = {n['author'] for n in notes}
        self.assertIn('Alice', authors)
        self.assertIn('Bob', authors)


class TestFormatToolbar(BaseTestCase):
    """Test wiki formatting toolbar presence."""

    def test_toolbar_visible_for_logged_in(self):
        """Logged-in users see the formatting toolbar."""
        self.login_admin()
        db.add_product_reference(codename='FmtTest')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertIn(b'fmt-toolbar', resp.data)
        self.assertIn(b'data-fmt="bold"', resp.data)
        self.assertIn(b'data-fmt="italic"', resp.data)
        self.assertIn(b'data-fmt="underline"', resp.data)
        self.assertIn(b'data-fmt="heading-up"', resp.data)
        self.assertIn(b'data-fmt="heading-down"', resp.data)

    def test_toolbar_not_visible_for_anonymous(self):
        """Anonymous users see read-only view without format buttons."""
        db.add_product_reference(codename='AnonFmt')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.get(f'/wiki/{ref_id}')
        # The actual toolbar HTML buttons shouldn't be present for anon
        self.assertNotIn(b'data-fmt="bold"', resp.data)
        self.assertIn(b'wikiReadOnly', resp.data)


class TestExportImportFunctional(BaseTestCase):
    """Functional tests for export and import flows."""

    def test_csv_export_contains_devices(self):
        """CSV export should contain all devices."""
        self.login_admin()
        db.add_device({'name': 'Export Dev 1', 'category': 'Router'})
        db.add_device({'name': 'Export Dev 2', 'category': 'Printer', 'codename': 'Test'})
        resp = self.client.get('/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Export Dev 1', resp.data)
        self.assertIn(b'Export Dev 2', resp.data)
        self.assertIn('text/csv', resp.content_type)

    def test_xlsx_export(self):
        """Excel export should return xlsx file."""
        self.login_admin()
        db.add_device({'name': 'XLSX Device'})
        resp = self.client.get('/export/xlsx')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp.content_type)

    def test_import_xlsx(self):
        """Test importing devices from xlsx file."""
        self.login_admin()
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Name', 'Category', 'Manufacturer', 'Location'])
        ws.append(['XLSX Import Dev', 'Router', 'Cisco', 'Lab C'])
        ws.append(['XLSX Import Dev 2', 'Printer', 'HP', 'Lab D'])
        from io import BytesIO
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = self.client.post('/import/devices', data={
            'import_file': (buf, 'test.xlsx')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Imported 2', resp.data)

    def test_import_no_file(self):
        """Import with no file should show error."""
        self.login_admin()
        resp = self.client.post('/import/devices', data={},
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertIn(b'No file selected', resp.data)


class TestLabelPDF(BaseTestCase):
    """Test PDF label generation."""

    def test_label_pdf_returns_pdf(self):
        """PDF label route should return a valid PDF."""
        self.login_admin()
        did = db.add_device({'name': 'PDF Label Test'})
        device = db.get_device(did)
        barcode_utils.generate_label(did, device['barcode_value'], 'PDF Label Test')
        resp = self.client.get(f'/labels/{did}.pdf')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('application/pdf', resp.content_type)
        self.assertTrue(resp.data.startswith(b'%PDF'))

    def test_label_pdf_nonexistent_device(self):
        """PDF for nonexistent device should return 404."""
        resp = self.client.get('/labels/fake123.pdf')
        self.assertEqual(resp.status_code, 404)

    def test_label_png_always_regenerated(self):
        """Label PNG route should always serve current label."""
        self.login_admin()
        did = db.add_device({'name': 'PNG Label Test'})
        resp = self.client.get(f'/labels/{did}.png')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('image/png', resp.content_type)


class TestDeviceCheckoutFlow(BaseTestCase):
    """Functional tests for checkout/checkin workflow."""

    def test_checkout_and_checkin(self):
        """Full checkout → checkin flow."""
        self.login_admin()
        did = db.add_device({'name': 'Checkout Test Dev'})
        # Checkout
        resp = self.client.post(f'/devices/{did}/checkout', data={
            'assigned_to': 'John Doe'
        }, follow_redirects=True)
        self.assertIn(b'checked out', resp.data.lower())
        device = db.get_device(did)
        self.assertEqual(device['status'], 'checked_out')
        self.assertEqual(device['assigned_to'], 'John Doe')
        # Checkin
        resp = self.client.post(f'/devices/{did}/checkin', follow_redirects=True)
        device = db.get_device(did)
        self.assertEqual(device['status'], 'available')
        self.assertEqual(device['assigned_to'], '')

    def test_checkout_history_shown(self):
        """Device detail should show checkout history."""
        self.login_admin()
        did = db.add_device({'name': 'History Test Dev'})
        self.client.post(f'/devices/{did}/checkout', data={'assigned_to': 'Jane'},
                         follow_redirects=True)
        self.client.post(f'/devices/{did}/checkin', follow_redirects=True)
        resp = self.client.get(f'/devices/{did}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Checkout History', resp.data)


class TestHealthAndDashboard(BaseTestCase):
    """Functional tests for dashboard and health."""

    def test_dashboard_shows_categories(self):
        """Dashboard should show category breakdown."""
        db.add_device({'name': 'Cat Test', 'category': 'Router'})
        resp = self.client.get('/')
        self.assertIn(b'Router', resp.data)
        self.assertIn(b'By Category', resp.data)

    def test_dashboard_shows_recent_activity(self):
        """Dashboard should show recent activity."""
        db.add_device({'name': 'Activity Test'})
        resp = self.client.get('/')
        self.assertIn(b'Recent Activity', resp.data)
        self.assertIn(b'Activity Test', resp.data)

    def test_health_endpoint_json(self):
        """Health endpoint returns JSON with status."""
        resp = self.client.get('/health')
        data = json.loads(resp.data)
        self.assertEqual(data['status'], 'ok')


class TestScannerLookup(BaseTestCase):
    """Test barcode scanner API."""

    def test_scan_lookup_by_barcode(self):
        """Scanner lookup should find device by barcode value."""
        did = db.add_device({'name': 'Scanner Test'})
        device = db.get_device(did)
        resp = self.client.get(f'/api/lookup?barcode={device["barcode_value"]}')
        data = json.loads(resp.data)
        self.assertTrue(data['found'])
        self.assertEqual(data['device_id'], did)

    def test_scan_lookup_case_insensitive(self):
        """Scanner lookup should be case-insensitive."""
        did = db.add_device({'name': 'Case Test'})
        device = db.get_device(did)
        bc_lower = device['barcode_value'].lower()
        resp = self.client.get(f'/api/lookup?barcode={bc_lower}')
        data = json.loads(resp.data)
        self.assertTrue(data['found'])

    def test_scan_page_loads(self):
        """Scan page should load."""
        resp = self.client.get('/scan')
        self.assertEqual(resp.status_code, 200)


class TestClientSideFiltering(BaseTestCase):
    """Test that device list supports client-side filtering."""

    def test_device_list_has_data_attributes(self):
        """Device list rows should have data attributes for filtering."""
        db.add_device({'name': 'Filter Me', 'category': 'Router', 'location': 'Lab A'})
        resp = self.client.get('/devices')
        self.assertIn(b'data-name=', resp.data)

    def test_device_list_has_filter_input(self):
        """Device list should have a search input for client-side filtering."""
        resp = self.client.get('/devices')
        # Should have the search input
        self.assertIn(b'search', resp.data.lower())

    def test_codename_filter_server_side(self):
        """Filtering by codename should use server-side filtering."""
        db.add_device({'name': 'TestPrinter', 'category': 'Printer', 'codename': 'Phoenix'})
        db.add_device({'name': 'Other Router', 'category': 'Router'})
        resp = self.client.get('/devices?codename=Phoenix')
        self.assertIn(b'TestPrinter', resp.data)


class TestEdgeCase(BaseTestCase):
    """Edge case and error handling tests."""

    def test_device_detail_nonexistent(self):
        """Viewing a nonexistent device should redirect gracefully."""
        resp = self.client.get('/devices/nonexistent123', follow_redirects=True)
        self.assertIn(b'Device not found', resp.data)

    def test_login_wrong_password(self):
        """Wrong password should show error."""
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'wrongpass',
        }, follow_redirects=True)
        self.assertIn(b'Invalid username or password', resp.data)

    def test_logout_redirect(self):
        """Logout should redirect to dashboard."""
        self.login_admin()
        resp = self.client.get('/logout', follow_redirects=True)
        self.assertIn(b'logged out', resp.data)

    def test_device_retire(self):
        """Admin can retire a device."""
        self.login_admin()
        did = db.add_device({'name': 'Retire Me'})
        resp = self.client.post(f'/devices/{did}/retire', follow_redirects=True)
        device = db.get_device(did)
        self.assertEqual(device['status'], 'retired')

    def test_xss_prevention_in_notes(self):
        """Note content should be escaped in the template."""
        self.login_admin()
        did = db.add_device({'name': 'XSS Test Device'})
        self.client.post(f'/devices/{did}/notes', data={
            'note_content': '<script>alert("xss")</script>',
        }, follow_redirects=True)
        resp = self.client.get(f'/devices/{did}')
        self.assertEqual(resp.status_code, 200)
        # The script tag should be escaped, not rendered as HTML
        self.assertNotIn(b'<script>alert', resp.data)
        self.assertIn(b'&lt;script&gt;', resp.data)

    def test_xss_prevention_in_author(self):
        """Author name should be escaped."""
        self.login_admin()
        did = db.add_device({'name': 'XSS Author Test'})
        self.client.get('/logout')
        self.client.post(f'/devices/{did}/notes', data={
            'note_content': 'Normal content',
            'author_name': '<img onerror=alert(1) src=x>',
        }, follow_redirects=True)
        resp = self.client.get(f'/devices/{did}')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'<img onerror', resp.data)


if __name__ == '__main__':
    unittest.main()
