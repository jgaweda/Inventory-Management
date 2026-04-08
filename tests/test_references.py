from tests import BaseTestCase, db, json, os, shutil, _test_dir, patch


class TestProductReferenceAPI(BaseTestCase):
    """Test inline edit API for product references."""

    def test_inline_edit_requires_permission(self):
        db.save_guest_permissions(set())  # clear guest defaults for this test
        resp = self.client.patch('/api/reference/1',
                                 data=json.dumps({'codename': 'Test'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 403)  # no permission

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


class TestProductReferenceDeleteExport(BaseTestCase):
    """Test product reference delete and export routes."""

    def test_delete_reference_via_web(self):
        self.login_admin()
        db.add_product_reference(codename='DeleteMe')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.post(f'/reference/{ref_id}/delete', follow_redirects=True)
        self.assertIn(b'deleted', resp.data.lower())
        self.assertIsNone(db.get_product_reference(ref_id))

    def test_export_references_csv(self):
        self.login_admin()
        db.add_product_reference(codename='ExportProd', model_name='X100', wifi_gen='6E')
        resp = self.client.get('/reference/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp.content_type)
        self.assertIn(b'ExportProd', resp.data)
        self.assertIn(b'X100', resp.data)
        self.assertIn(b'6E', resp.data)

    def test_viewer_cannot_delete_reference(self):
        db.create_user('viewer1', 'pass1234', role='custom')
        db.add_product_reference(codename='Protected')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        self.client.post('/login', data={'username': 'viewer1', 'password': 'pass1234'})
        resp = self.client.post(f'/reference/{ref_id}/delete', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)
        self.assertIsNotNone(db.get_product_reference(ref_id))

    def test_viewer_cannot_export_references(self):
        db.create_user('viewer1', 'pass1234', role='custom')
        self.client.post('/login', data={'username': 'viewer1', 'password': 'pass1234'})
        resp = self.client.get('/reference/export', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)


class TestProductReferenceSeed(BaseTestCase):
    """Test automatic product reference seeding from seed_data/."""

    def setUp(self):
        # Clean up any seed_data from prior tests BEFORE init_db()
        seed_dir = os.path.join(_test_dir, 'seed_data')
        if os.path.isdir(seed_dir):
            shutil.rmtree(seed_dir)
        super().setUp()

    def _create_seed_csv(self, seed_dir, rows):
        """Helper: write a seed CSV file."""
        os.makedirs(seed_dir, exist_ok=True)
        csv_path = os.path.join(seed_dir, 'product_reference.csv')
        import csv
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Codename', 'Model Name', 'Wi-Fi Gen', 'Year',
                             'Print Technology'])
            for row in rows:
                writer.writerow(row)
        return csv_path

    def _create_seed_zip(self, seed_dir, images):
        """Helper: create a printer_images.zip with given {name: bytes} entries."""
        import zipfile
        zip_path = os.path.join(seed_dir, 'printer_images.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for name, data in images.items():
                zf.writestr(name, data)
        return zip_path

    def test_seed_csv_imports_on_empty_table(self):
        """Seed CSV is imported when product_reference table is empty."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'HP OJ Pro 9120', '6E', '2025', 'Ink'],
            ['Tesla', 'HP LJ Pro 400', '6', '2024', 'Laser'],
        ])
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        codenames = [r['codename'] for r in refs]
        self.assertIn('Marconi', codenames)
        self.assertIn('Tesla', codenames)
        self.assertEqual(len(refs), 2)
        marconi = [r for r in refs if r['codename'] == 'Marconi'][0]
        self.assertEqual(marconi['model_name'], 'HP OJ Pro 9120')
        self.assertEqual(marconi['wifi_gen'], '6E')
        self.assertEqual(marconi['print_technology'], 'Ink')

    def test_seed_skips_when_refs_exist(self):
        """Seeding is skipped when product_reference table already has data."""
        db.add_product_reference(codename='Existing')
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['NewProduct', 'Model X', '7', '2026', 'Ink'],
        ])
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        codenames = [r['codename'] for r in refs]
        self.assertIn('Existing', codenames)
        self.assertNotIn('NewProduct', codenames)

    def test_seed_skips_when_no_csv(self):
        """Seeding does nothing when no CSV file exists."""
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()  # should not raise
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 0)

    def test_seed_images_matched_by_model_name(self):
        """Wiki images from zip are matched to refs by model name."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'HP OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        # Create a fake PNG (just needs to exist, not be a valid image)
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'HP OJ Pro 9120.png': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 1)
        ref_id = refs[0]['ref_id']
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]['original_name'], 'HP OJ Pro 9120.png')
        self.assertTrue(attachments[0]['content_type'].startswith('image/'))
        # Verify file exists on disk
        from runtime_dirs import DATA_DIR
        file_path = os.path.join(DATA_DIR, 'wiki_uploads', str(ref_id), attachments[0]['filename'])
        self.assertTrue(os.path.isfile(file_path))

    def test_seed_images_matched_by_codename(self):
        """Wiki images can also match by codename."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'HP OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'Marconi.png': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]['original_name'], 'Marconi.png')

    def test_seed_unmatched_images_ignored(self):
        """Images that don't match any product reference are skipped."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'HP OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'Unknown Printer.png': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 0)

    def test_seed_images_fuzzy_match_abbreviations(self):
        """Image filenames with full names match CSV abbreviated model names."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Muscatel', 'OJ 69x0', '', '2020', 'Ink'],
            ['Weber', 'OJ Pro 87x0', '', '2019', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'officejet_6950_6960.jpg': fake_png,
            'officejet_pro_8710_8740.jpg': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        for r in refs:
            attachments = db.get_wiki_attachments(r['ref_id'])
            self.assertEqual(len(attachments), 1,
                             f"Expected 1 image for {r['codename']}, got {len(attachments)}")

    def test_seed_images_fuzzy_match_model_tokens(self):
        """Image filenames match when model number tokens overlap."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Kay', 'M109/M110/M111/M112', '', '2022', 'Laser'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'laserjet_m109_m112.jpg': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 1)
        attachments = db.get_wiki_attachments(refs[0]['ref_id'])
        self.assertEqual(len(attachments), 1)

    def test_seed_images_year_suffix_stripped(self):
        """Image filenames with year suffixes still match."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Spirit', 'PageWide Pro 750', '', '2017', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'pagewide_pro_750dw_2017.jpg': fake_png,
        })
        with patch('database.BUNDLE_DIR', _test_dir):
            from database import _seed_product_references
            _seed_product_references()
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 1)
        attachments = db.get_wiki_attachments(refs[0]['ref_id'])
        self.assertEqual(len(attachments), 1)


class TestUpsertProductReference(BaseTestCase):
    """Test upsert_product_reference for seed import mode."""

    def test_upsert_adds_new_entry(self):
        """Upsert creates a new entry when codename doesn't exist."""
        ref_id, action = db.upsert_product_reference(
            codename='NewProd', model_name='Model X', year='2025',
            print_technology='Ink')
        self.assertEqual(action, 'added')
        self.assertIsNotNone(ref_id)
        refs = db.get_product_reference_by_codename('NewProd')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['model_name'], 'Model X')
        self.assertEqual(refs[0]['print_technology'], 'Ink')

    def test_upsert_updates_existing_entry(self):
        """Upsert updates an existing entry matched by codename."""
        db.add_product_reference(codename='Marconi', model_name='Old Model',
                                 year='2023', print_technology='Ink')
        ref_id, action = db.upsert_product_reference(
            codename='Marconi', model_name='New Model', year='2024')
        self.assertEqual(action, 'updated')
        refs = db.get_product_reference_by_codename('Marconi')
        self.assertEqual(refs[0]['model_name'], 'New Model')
        self.assertEqual(refs[0]['year'], '2024')

    def test_upsert_preserves_nonempty_fields(self):
        """Upsert doesn't overwrite existing fields with empty values."""
        db.add_product_reference(codename='Tesla', model_name='LJ Pro 400',
                                 year='2024', print_technology='Laser',
                                 wifi_gen='6')
        ref_id, action = db.upsert_product_reference(
            codename='Tesla', model_name='', year='', wifi_gen='')
        self.assertEqual(action, 'updated')
        refs = db.get_product_reference_by_codename('Tesla')
        self.assertEqual(refs[0]['model_name'], 'LJ Pro 400')
        self.assertEqual(refs[0]['year'], '2024')
        self.assertEqual(refs[0]['print_technology'], 'Laser')
        self.assertEqual(refs[0]['wifi_gen'], '6')

    def test_upsert_creates_wiki_page(self):
        """Upsert add mode auto-creates a wiki page."""
        ref_id, action = db.upsert_product_reference(codename='WikiTest')
        self.assertEqual(action, 'added')
        wiki = db.get_wiki_by_ref_id(ref_id)
        self.assertIsNotNone(wiki)


class TestSeedImportMode(BaseTestCase):
    """Test the seed import mode via the web UI."""

    def setUp(self):
        seed_dir = os.path.join(_test_dir, 'seed_data')
        if os.path.isdir(seed_dir):
            shutil.rmtree(seed_dir)
        super().setUp()

    def _create_seed_csv(self, seed_dir, rows):
        os.makedirs(seed_dir, exist_ok=True)
        csv_path = os.path.join(seed_dir, 'product_reference.csv')
        import csv
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Codename', 'Model Name', 'Wi-Fi Gen', 'Year',
                             'Print Technology'])
            for row in rows:
                writer.writerow(row)
        return csv_path

    def _create_seed_zip(self, seed_dir, images):
        import zipfile
        zip_path = os.path.join(seed_dir, 'printer_images.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for name, data in images.items():
                zf.writestr(name, data)
        return zip_path

    def test_seed_mode_adds_missing_entries(self):
        """Seed mode adds entries not already present."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'OJ Pro 9120', '6E', '2025', 'Ink'],
            ['Tesla', 'LJ Pro 400', '6', '2024', 'Laser'],
        ])
        self.login_admin()
        with patch('app.BUNDLE_DIR', _test_dir):
            resp = self.client.post('/reference/seed', follow_redirects=True)
        self.assertIn(b'2 added', resp.data)
        refs = db.get_all_product_references()
        self.assertEqual(len(refs), 2)

    def test_seed_mode_updates_existing(self):
        """Seed mode updates existing entries by codename."""
        db.add_product_reference(codename='Marconi', model_name='Old Model')
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        self.login_admin()
        with patch('app.BUNDLE_DIR', _test_dir):
            resp = self.client.post('/reference/seed', follow_redirects=True)
        self.assertIn(b'1 updated', resp.data)
        refs = db.get_product_reference_by_codename('Marconi')
        self.assertEqual(refs[0]['model_name'], 'OJ Pro 9120')

    def test_seed_mode_attaches_images(self):
        """Seed mode attaches images from the seed zip."""
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'officejet_pro_9120_9120b.jpg': fake_png,
        })
        self.login_admin()
        with patch('app.BUNDLE_DIR', _test_dir):
            resp = self.client.post('/reference/seed', follow_redirects=True)
        self.assertIn(b'1 images attached', resp.data)
        refs = db.get_product_reference_by_codename('Marconi')
        attachments = db.get_wiki_attachments(refs[0]['ref_id'])
        self.assertEqual(len(attachments), 1)

    def test_seed_mode_skips_existing_attachments(self):
        """Seed mode does not duplicate images on refs that already have attachments."""
        ref_id = db.add_product_reference(codename='Marconi', model_name='OJ Pro 9120')
        db.add_wiki_attachment(ref_id=ref_id, filename='existing.png',
                               original_name='existing.png',
                               content_type='image/png', size_bytes=100,
                               uploaded_by='admin')
        seed_dir = os.path.join(_test_dir, 'seed_data')
        self._create_seed_csv(seed_dir, [
            ['Marconi', 'OJ Pro 9120', '6E', '2025', 'Ink'],
        ])
        fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self._create_seed_zip(seed_dir, {
            'officejet_pro_9120_9120b.jpg': fake_png,
        })
        self.login_admin()
        with patch('app.BUNDLE_DIR', _test_dir):
            resp = self.client.post('/reference/seed', follow_redirects=True)
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)  # still just the original

    def test_seed_mode_requires_login(self):
        """Seed mode requires authentication when guest has no permissions."""
        db.save_guest_permissions(set())  # clear guest defaults for this test
        resp = self.client.post('/reference/seed')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_seed_mode_no_seed_data(self):
        """Seed mode shows error when seed data is missing."""
        self.login_admin()
        with patch('app.BUNDLE_DIR', _test_dir):
            resp = self.client.post('/reference/seed', follow_redirects=True)
        self.assertIn(b'Seed data not found', resp.data)


class TestLargeFormatPrintTechnology(BaseTestCase):
    """Test Large Format as a print technology option."""

    def test_large_format_in_dropdown(self):
        """Large Format appears in the print technology dropdown."""
        db.add_product_reference(codename='Beam', print_technology='Large Format')
        self.login_admin()
        resp = self.client.get('/reference')
        self.assertIn(b'Large Format', resp.data)

    def test_large_format_badge_styling(self):
        """Large Format badge uses amber color for non-admin view."""
        db.add_product_reference(codename='Beam', print_technology='Large Format')
        resp = self.client.get('/reference')
        self.assertIn(b'Large Format', resp.data)

    def test_add_product_with_large_format(self):
        """Can create a product reference with Large Format technology."""
        ref_id = db.add_product_reference(
            codename='TestLF', model_name='DesignJet Test',
            print_technology='Large Format')
        ref = db.get_product_reference(ref_id)
        self.assertEqual(ref['print_technology'], 'Large Format')

    def test_upsert_preserves_large_format(self):
        """Upsert preserves Large Format when incoming value is empty."""
        db.add_product_reference(codename='Beam', print_technology='Large Format')
        ref_id, action = db.upsert_product_reference(codename='Beam', model_name='DJ XT950')
        refs = db.get_product_reference_by_codename('Beam')
        self.assertEqual(refs[0]['print_technology'], 'Large Format')


class TestCartridgeToner(BaseTestCase):
    """Test the Cartridge/Toner field across the application."""

    def test_add_product_with_cartridge_toner(self):
        """Can create a product reference with cartridge_toner."""
        ref_id = db.add_product_reference(
            codename='TestCart', model_name='OJ Pro 9120',
            print_technology='Ink', cartridge_toner='HP 936/937/938')
        ref = db.get_product_reference(ref_id)
        self.assertEqual(ref['cartridge_toner'], 'HP 936/937/938')

    def test_update_product_cartridge_toner(self):
        """Can update cartridge_toner on an existing product."""
        ref_id = db.add_product_reference(codename='UpdateCart', cartridge_toner='HP 67/67XL')
        db.update_product_reference(ref_id=ref_id, codename='UpdateCart',
                                    cartridge_toner='HP 67XL/305XL')
        ref = db.get_product_reference(ref_id)
        self.assertEqual(ref['cartridge_toner'], 'HP 67XL/305XL')

    def test_upsert_preserves_cartridge_toner(self):
        """Upsert preserves cartridge_toner when incoming value is empty."""
        db.add_product_reference(codename='UpsertCart', cartridge_toner='HP 230A/230X')
        ref_id, action = db.upsert_product_reference(codename='UpsertCart', model_name='LJ Pro 400')
        refs = db.get_product_reference_by_codename('UpsertCart')
        self.assertEqual(refs[0]['cartridge_toner'], 'HP 230A/230X')

    def test_upsert_updates_cartridge_toner(self):
        """Upsert updates cartridge_toner when incoming value is non-empty."""
        db.add_product_reference(codename='UpsertCart2', cartridge_toner='HP 78A')
        ref_id, action = db.upsert_product_reference(codename='UpsertCart2', cartridge_toner='HP 78A/78X')
        refs = db.get_product_reference_by_codename('UpsertCart2')
        self.assertEqual(refs[0]['cartridge_toner'], 'HP 78A/78X')

    def test_search_by_cartridge_toner(self):
        """Search finds products by cartridge_toner value."""
        db.add_product_reference(codename='SearchCart', cartridge_toner='HP 936/937/938')
        results = db.get_all_product_references(search='936')
        codenames = [r['codename'] for r in results]
        self.assertIn('SearchCart', codenames)

    def test_inline_edit_cartridge_toner(self):
        """Inline edit API accepts cartridge_toner field."""
        ref_id = db.add_product_reference(codename='InlineCart')
        self.login_admin()
        resp = self.client.patch(f'/api/reference/{ref_id}',
                                 json={'cartridge_toner': 'HP 67/67XL'},
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        ref = db.get_product_reference(ref_id)
        self.assertEqual(ref['cartridge_toner'], 'HP 67/67XL')

    def test_export_includes_cartridge_toner(self):
        """CSV export includes Cartridge/Toner column."""
        db.add_product_reference(codename='ExportCart', cartridge_toner='HP 230A')
        self.login_admin()
        resp = self.client.get('/reference/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Cartridge/Toner', resp.data)
        self.assertIn(b'HP 230A', resp.data)

    def test_form_shows_cartridge_toner(self):
        """Product reference form includes cartridge_toner field."""
        self.login_admin()
        resp = self.client.get('/reference/add')
        self.assertIn(b'cartridge_toner', resp.data)
        self.assertIn(b'Cartridge/Toner', resp.data)

    def test_add_via_form_with_cartridge_toner(self):
        """Adding a product via POST includes cartridge_toner."""
        self.login_admin()
        resp = self.client.post('/reference/add', data={
            'codename': 'FormCart',
            'cartridge_toner': 'HP 962/962XL',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        refs = db.get_product_reference_by_codename('FormCart')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['cartridge_toner'], 'HP 962/962XL')

    def test_table_shows_cartridge_toner_column(self):
        """Product reference table has Cartridge/Toner header."""
        db.add_product_reference(codename='TableCart', cartridge_toner='HP 67/67XL')
        resp = self.client.get('/reference')
        self.assertIn(b'Cartridge/Toner', resp.data)
        self.assertIn(b'HP 67/67XL', resp.data)
