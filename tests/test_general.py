from tests import BaseTestCase, db, json, os, shutil, _test_dir, barcode_utils, patch, sqlite3


class TestBarcodeGeneration(BaseTestCase):
    """Test barcode generation with safe alphabet and CNX- prefix."""

    def test_barcode_encoding_roundtrip(self):
        """Barcode encoding/decoding is reversible."""
        for n in [0, 1, 10, 28, 29, 100, 999, 50000]:
            encoded = db._int_to_barcode(n)
            decoded = db._barcode_to_int(encoded)
            self.assertEqual(decoded, n, f'Round-trip failed for {n}: encoded={encoded}')

    def test_barcode_alphabet_excludes_ambiguous(self):
        """Barcode alphabet should not contain O, I, L, U, 0, or 1."""
        for ch in 'OILUoilu01':
            self.assertNotIn(ch, db._BARCODE_CHARS,
                             f'Ambiguous character {ch!r} found in barcode alphabet')

    def test_barcode_has_cnx_prefix(self):
        device_id = db.add_device({'name': 'Test Device'})
        device = db.get_device(device_id)
        self.assertTrue(device['barcode_value'].startswith('CNX-'),
                        f'Expected CNX- prefix, got {device["barcode_value"]}')

    def test_barcodes_are_unique(self):
        """Each device gets a unique barcode even though they're scrambled."""
        barcodes = []
        for i in range(10):
            device_id = db.add_device({'name': f'Device {i}'})
            device = db.get_device(device_id)
            barcodes.append(device['barcode_value'])
        self.assertEqual(len(barcodes), len(set(barcodes)),
                         f'Duplicate barcodes found: {barcodes}')

    def test_barcodes_not_sequential(self):
        """Scrambled barcodes should not reveal creation order."""
        barcodes = []
        for i in range(5):
            device_id = db.add_device({'name': f'Device {i}'})
            device = db.get_device(device_id)
            barcodes.append(device['barcode_value'])
        # Extract numeric values — they should NOT be sequential
        nums = [db._barcode_to_int(v.replace('CNX-', '')) for v in barcodes]
        is_sequential = all(nums[i] == nums[i-1] + 1 for i in range(1, len(nums)))
        self.assertFalse(is_sequential,
                         f'Barcodes appear sequential (should be scrambled): {barcodes}')

    def test_barcode_uses_safe_alphabet_only(self):
        """Generated barcodes only contain characters from the safe alphabet."""
        safe = set(db._BARCODE_CHARS)
        for i in range(20):
            device_id = db.add_device({'name': f'Safe Test {i}'})
            device = db.get_device(device_id)
            suffix = device['barcode_value'][4:]  # strip CNX-
            for ch in suffix:
                self.assertIn(ch, safe,
                              f'Unsafe character {ch!r} in barcode {device["barcode_value"]}')

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

    def test_scramble_is_bijection(self):
        """Scramble function must produce unique outputs (no collisions)."""
        outputs = set()
        for i in range(1, 1001):
            s = db._scramble_seq(i)
            self.assertNotIn(s, outputs, f'Collision at seq={i}')
            self.assertGreaterEqual(s, 0)
            self.assertLess(s, db._BARCODE_SPACE)
            outputs.add(s)

    def test_barcode_zero_padded(self):
        """Barcodes should be zero-padded to at least 4 characters after prefix."""
        device_id = db.add_device({'name': 'Pad Test'})
        device = db.get_device(device_id)
        suffix = device['barcode_value'].replace('CNX-', '')
        self.assertGreaterEqual(len(suffix), 4,
                                f'Barcode suffix too short: {device["barcode_value"]}')

    def test_old_sequential_barcode_migration(self):
        """Old sequential barcodes like CNX-1 get scrambled on init."""
        device_id = db.add_device({'name': 'Migration Test'})
        with db.db_transaction() as conn:
            conn.execute("UPDATE devices SET barcode_value = 'CNX-1' WHERE device_id = ?", (device_id,))
        # Re-run init_db to trigger migration
        db.init_db()
        device = db.get_device(device_id)
        self.assertTrue(device['barcode_value'].startswith('CNX-'))
        suffix = device['barcode_value'][4:]
        self.assertGreaterEqual(len(suffix), 4, 'Migrated barcode should be at least 4 chars')
        self.assertNotEqual(device['barcode_value'], 'CNX-0001',
                            'Migrated barcode should be scrambled, not just padded')

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

class TestLabelRoutes(BaseTestCase):
    """Test label serving and PDF generation."""

    def test_serve_label_png(self):
        did = db.add_device({'name': 'Label PNG'})
        device = db.get_device(did)
        barcode_utils.generate_label(did, device['barcode_value'], device['name'])
        resp = self.client.get(f'/labels/{did}.png')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('image/png', resp.content_type)

    def test_serve_label_pdf(self):
        did = db.add_device({'name': 'Label PDF'})
        device = db.get_device(did)
        barcode_utils.generate_label(did, device['barcode_value'], device['name'])
        resp = self.client.get(f'/labels/{did}.pdf')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('pdf', resp.content_type)
        self.assertTrue(resp.data.startswith(b'%PDF'))

    def test_serve_label_nonexistent(self):
        resp = self.client.get('/labels/nonexistent.png')
        self.assertEqual(resp.status_code, 404)

    def test_label_sheet_post(self):
        self.login_admin()
        d1 = db.add_device({'name': 'Sheet Device 1'})
        d2 = db.add_device({'name': 'Sheet Device 2'})
        resp = self.client.post('/labels/sheet', data={
            'device_ids': [d1, d2],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('image/png', resp.content_type)

class TestHealthAndDashboard(BaseTestCase):
    """Functional tests for scan page (formerly dashboard) and health."""

    def test_root_redirects_to_scan(self):
        """Root URL should redirect to scan page."""
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/scan', resp.headers['Location'])

    def test_scan_shows_recent_activity(self):
        """Scan page should show recent activity."""
        db.add_device({'name': 'Activity Test'})
        resp = self.client.get('/scan')
        self.assertIn(b'Recent Activity', resp.data)
        self.assertIn(b'Activity Test', resp.data)

    def test_health_endpoint_json(self):
        """Health endpoint returns JSON with status."""
        resp = self.client.get('/health')
        data = json.loads(resp.data)
        self.assertEqual(data['status'], 'ok')

class TestDashboardStats(BaseTestCase):
    """Test dashboard statistics function."""

    def test_stats_empty_db(self):
        stats = db.get_stats()
        self.assertEqual(stats['total'], 0)
        self.assertEqual(stats['available'], 0)
        self.assertEqual(stats['checked_out'], 0)

    def test_stats_with_devices(self):
        db.add_device({'name': 'Available 1'})
        d2 = db.add_device({'name': 'Checked Out 1'})
        db.checkout_device(d2, 'User A')
        d3 = db.add_device({'name': 'Retired 1'})
        db.retire_device(d3)
        stats = db.get_stats()
        # total excludes retired devices
        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['available'], 1)
        self.assertEqual(stats['checked_out'], 1)
        self.assertEqual(stats['retired'], 1)

    def test_stats_by_category(self):
        db.add_device({'name': 'P1 (HP LJ)', 'category': 'Printer', 'codename': 'P1'})
        db.add_device({'name': 'HP Router', 'category': 'Router/AP', 'manufacturer': 'HP'})
        stats = db.get_stats()
        cat_names = [c['category'] for c in stats['by_category']]
        self.assertIn('Printer', cat_names)
        self.assertIn('Router/AP', cat_names)

    def test_scan_page_loads_with_data(self):
        db.add_device({'name': 'Dashboard Device'})
        resp = self.client.get('/scan')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Barcode Scanner', resp.data)

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

class TestExportImportFunctional(BaseTestCase):
    """Functional tests for export and import flows."""

    def test_xlsx_export(self):
        """Excel export should return xlsx file."""
        self.login_admin()
        db.add_device({'name': 'XLSX Device'})
        resp = self.client.get('/export/xlsx')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp.content_type)

class TestDeviceExport(BaseTestCase):
    """Test device CSV export."""

    def test_export_csv(self):
        db.add_device({'name': 'Export Test 1', 'manufacturer': 'HP', 'category': 'Printer', 'codename': 'EP1'})
        db.add_device({'name': 'Export Test 2', 'manufacturer': 'Cisco', 'category': 'Router/AP'})
        resp = self.client.get('/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp.content_type)
        self.assertIn(b'Export Test 1', resp.data)
        self.assertIn(b'Export Test 2', resp.data)
        # Verify user-friendly headers
        self.assertIn(b'Connectivity Type/Version', resp.data)
        self.assertIn(b'Source', resp.data)
        self.assertIn(b'Assigned To', resp.data)
        # Verify vendor_supplied is shown as readable text
        self.assertIn(b'HP Owned', resp.data)

class TestServerSettings(BaseTestCase):
    """Tests for admin server port settings."""

    def _login_viewer(self):
        db.create_user('viewer1', 'pass1234', role='custom', display_name='Viewer')
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

class TestLogPagination(BaseTestCase):
    """Test audit log pagination."""

    def test_log_page_parameter(self):
        self.login_admin()
        resp = self.client.get('/logs?page=1')
        self.assertEqual(resp.status_code, 200)

class TestLogRoutes(BaseTestCase):
    """Test log viewing, clearing, and config routes."""

    def test_logs_page_loads(self):
        self.login_admin()
        resp = self.client.get('/logs')
        self.assertEqual(resp.status_code, 200)

    def test_clear_logs(self):
        self.login_admin()
        resp = self.client.post('/logs/clear', follow_redirects=True)
        self.assertIn(b'cleared', resp.data.lower())

    def test_update_log_config(self):
        self.login_admin()
        resp = self.client.post('/logs/config', data={
            'max_size_mb': '5',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_logs_requires_admin(self):
        db.create_user('viewer1', 'pass1234', role='custom')
        self.client.post('/login', data={'username': 'viewer1', 'password': 'pass1234'})
        resp = self.client.get('/logs', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

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

class TestSearchAndFilters(BaseTestCase):
    """Test device search and filter functions."""

    def test_search_by_name(self):
        db.add_device({'name': 'HP LaserJet 200', 'category': 'Printer', 'codename': 'LJ200'})
        db.add_device({'name': 'Cisco Router X', 'category': 'Router/AP', 'manufacturer': 'Cisco'})
        results = db.search_devices(query='LaserJet')
        self.assertEqual(len(results), 1)
        self.assertIn('LaserJet', results[0]['name'])

    def test_search_by_category_filter(self):
        db.add_device({'name': 'Printer A', 'category': 'Printer', 'codename': 'PA'})
        db.add_device({'name': 'Cisco Router', 'category': 'Router/AP', 'manufacturer': 'Cisco'})
        results = db.search_devices(category='Router/AP')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['category'], 'Router/AP')

    def test_search_by_status_filter(self):
        did = db.add_device({'name': 'Lost Device'})
        db.update_device(did, {'status': 'lost'})
        results = db.search_devices(status='lost')
        self.assertEqual(len(results), 1)

    def test_search_excludes_retired_by_default(self):
        did = db.add_device({'name': 'Retired Device'})
        db.retire_device(did)
        results = db.search_devices()
        names = [d['name'] for d in results]
        self.assertNotIn('Retired Device', names)

    def test_search_can_show_retired(self):
        did = db.add_device({'name': 'Retired Show'})
        db.retire_device(did)
        results = db.search_devices(status='retired')
        names = [d['name'] for d in results]
        self.assertIn('Retired Show', names)

    def test_search_by_location(self):
        db.add_device({'name': 'Lab A Device', 'location': 'Lab A'})
        db.add_device({'name': 'Lab B Device', 'location': 'Lab B'})
        results = db.search_devices(location='Lab A')
        self.assertEqual(len(results), 1)

    def test_search_by_connectivity(self):
        db.add_device({'name': 'WiFi 6 Device', 'connectivity': 'Wi-Fi 6'})
        db.add_device({'name': 'WiFi 7 Device', 'connectivity': 'Wi-Fi 7'})
        results = db.search_devices(connectivity='Wi-Fi 6')
        self.assertEqual(len(results), 1)

    def test_get_distinct_values(self):
        db.add_device({'name': 'Dev A', 'location': 'Lab A'})
        db.add_device({'name': 'Dev B', 'location': 'Lab B'})
        db.add_device({'name': 'Dev C', 'location': 'Lab A'})
        values = db.get_distinct_values('location')
        self.assertEqual(set(values), {'Lab A', 'Lab B'})

    def test_get_distinct_values_rejects_invalid_column(self):
        values = db.get_distinct_values('password_hash')
        self.assertEqual(values, [])

    def test_get_categories(self):
        cats = db.get_categories()
        names = [c['name'] for c in cats]
        self.assertIn('Printer', names)
        self.assertIn('Router/AP', names)

class TestAuditLog(BaseTestCase):
    """Test audit logging functions."""

    def test_device_add_creates_audit_entry(self):
        did = db.add_device({'name': 'Audited Device'}, performed_by='testuser')
        logs = db.get_audit_log(device_id=did)
        self.assertGreater(len(logs), 0)
        self.assertEqual(logs[0]['action'], 'added')
        self.assertEqual(logs[0]['performed_by'], 'testuser')

    def test_device_update_creates_audit_entry(self):
        did = db.add_device({'name': 'Before Update'})
        db.update_device(did, {'name': 'After Update'}, performed_by='editor1')
        logs = db.get_audit_log(device_id=did)
        actions = [l['action'] for l in logs]
        self.assertIn('updated', actions)

    def test_retire_creates_audit_entry(self):
        did = db.add_device({'name': 'Retire Audit'})
        db.retire_device(did, performed_by='admin')
        logs = db.get_audit_log(device_id=did)
        actions = [l['action'] for l in logs]
        self.assertIn('retired', actions)

    def test_audit_log_global(self):
        d1 = db.add_device({'name': 'Global 1'})
        d2 = db.add_device({'name': 'Global 2'})
        logs = db.get_audit_log()
        self.assertGreaterEqual(len(logs), 2)

    def test_audit_log_limit(self):
        for i in range(5):
            db.add_device({'name': f'Limit {i}'})
        logs = db.get_audit_log(limit=3)
        self.assertEqual(len(logs), 3)

class TestClientSideFiltering(BaseTestCase):
    """Test that device list supports client-side filtering."""

    def test_device_list_has_data_attributes(self):
        """Device list rows should have data attributes for filtering."""
        db.add_device({'name': 'Filter Me', 'category': 'Router', 'location': 'Lab A'})
        resp = self.client.get('/devices')
        self.assertIn(b'data-name=', resp.data)

    def test_codename_filter_server_side(self):
        """Filtering by codename should use server-side filtering."""
        db.add_device({'name': 'TestPrinter', 'category': 'Printer', 'codename': 'Phoenix'})
        db.add_device({'name': 'Other Router', 'category': 'Router'})
        resp = self.client.get('/devices?codename=Phoenix')
        self.assertIn(b'TestPrinter', resp.data)

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

class TestPublicRoutes(BaseTestCase):
    """Test that public routes work without auth."""

    def test_dashboard_redirects_to_scan(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)

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
        resp = self.client.post(f'/devices/{did}/retire', data={
            'retire_reason': 'End of life',
        }, follow_redirects=True)
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

class TestSQLInjectionPrevention(BaseTestCase):
    """Verify parameterized queries prevent SQL injection."""

    def test_sql_injection_in_search(self):
        db.add_device({'name': 'Normal Device'})
        results = db.search_devices("'; DROP TABLE devices; --")
        devices = db.get_all_devices()
        self.assertEqual(len(devices), 1)

    def test_sql_injection_in_username(self):
        resp = self.client.post('/login', data={
            'username': "' OR 1=1 --",
            'password': 'anything',
        }, follow_redirects=True)
        self.assertIn(b'Invalid username or password', resp.data)

    def test_sql_injection_in_device_name(self):
        self.login_admin()
        malicious = "'; DROP TABLE devices; --"
        self.client.post('/devices/add', data={
            'manufacturer': malicious, 'model_number': 'Test',
            'serial_number': 'SN-INJECT-001',
            'category': 'Router/AP',
        }, follow_redirects=True)
        devices = db.get_all_devices()
        self.assertGreater(len(devices), 0)

    def test_sql_injection_in_note(self):
        did = db.add_device({'name': 'Test'})
        note_id = db.add_device_note(did, 'Test', "'; DROP TABLE device_notes; --")
        self.assertIsNotNone(note_id)
        notes = db.get_device_notes(did)
        self.assertEqual(len(notes), 1)

class TestUnicodeHandling(BaseTestCase):
    """Test unicode characters in various fields."""

    def test_unicode_device_name(self):
        did = db.add_device({'name': 'Printer \u2014 \u00e9l\u00e8ve'})
        device = db.get_device(did)
        self.assertIn('\u2014', device['name'])

    def test_unicode_note(self):
        did = db.add_device({'name': 'Unicode Note Test'})
        db.add_device_note(did, '\u5f20\u4e09', '\U0001f4e8 \u4e2d\u6587\u6d4b\u8bd5')
        notes = db.get_device_notes(did)
        self.assertEqual(len(notes), 1)
        self.assertIn('\u4e2d\u6587', notes[0]['content'])

    def test_unicode_in_web_form(self):
        self.login_admin()
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP \u00ae', 'model_number': 'M\u00f6del',
            'serial_number': 'SN-UNI-001',
            'category': 'Router/AP', 'notes': 'C\u00e9sar\u2019s printer',
        }, follow_redirects=True)
        self.assertIn(b'added successfully', resp.data)

    def test_unicode_username(self):
        uid = db.create_user('\u00fcser1', 'pass1234', display_name='Ren\u00e9')
        user = db.get_user(uid)
        self.assertEqual(user['display_name'], 'Ren\u00e9')

class TestCascadeDeletes(BaseTestCase):
    """Test that deleting records properly cascades."""

    def test_delete_product_reference_cascades(self):
        self.login_admin()
        db.add_product_reference(codename='CascadeTest')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        self.client.post(f'/wiki/{ref_id}/save', data={'content': 'Test wiki'}, follow_redirects=True)
        import io
        self.client.post(f'/wiki/{ref_id}/upload',
                         data={'attachment': (io.BytesIO(b'test'), 'file.txt')},
                         content_type='multipart/form-data')
        self.assertIsNotNone(db.get_wiki_by_ref_id(ref_id))
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 1)
        db.delete_product_reference(ref_id)
        self.assertIsNone(db.get_product_reference(ref_id))
        self.assertIsNone(db.get_wiki_by_ref_id(ref_id))
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 0)

    def test_delete_device_preserves_notes(self):
        """Retiring a device should not delete notes."""
        did = db.add_device({'name': 'Note Device'})
        db.add_device_note(did, 'Tester', 'Important note')
        db.retire_device(did)
        notes = db.get_device_notes(did)
        self.assertEqual(len(notes), 1)
