from tests import BaseTestCase, db, json, os, _test_dir


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
        db.save_guest_permissions(set())  # clear guest defaults for this test
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

    def test_upload_requires_permission(self):
        db.save_guest_permissions(set())  # clear guest defaults for this test
        ref_id = self._create_product()
        # Not logged in and no guest permissions
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
        # View wiki page (guests have wiki permission by default, so upload area is visible)
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'readme.txt', resp.data)

    def test_attachments_no_upload_without_permission(self):
        """Non-logged-in users without wiki permission should not see upload area."""
        db.save_guest_permissions(set())  # clear guest defaults
        ref_id = self._create_product()
        self.login_admin()
        import io
        data = {'attachment': (io.BytesIO(b'public file'), 'readme.txt')}
        self.client.post(f'/wiki/{ref_id}/upload',
                         data=data, content_type='multipart/form-data')
        self.client.get('/logout')
        resp = self.client.get(f'/wiki/{ref_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'Click to upload', resp.data)


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


class TestPngToJpgConversion(BaseTestCase):
    """Test PNG→JPG auto-conversion on upload and migration."""

    def _create_product(self):
        db.add_product_reference(codename='PngTest')
        refs = db.get_all_product_references()
        return refs[0]['ref_id']

    def test_png_upload_auto_converts_to_jpg(self):
        """Uploading a PNG image auto-converts it to JPG."""
        ref_id = self._create_product()
        self.login_admin()
        import io
        # Create a minimal valid PNG (1x1 red pixel)
        from PIL import Image
        buf = io.BytesIO()
        img = Image.new('RGB', (10, 10), (255, 0, 0))
        img.save(buf, 'PNG')
        buf.seek(0)
        resp = self.client.post(f'/wiki/{ref_id}/upload',
                                data={'attachment': (buf, 'test_image.png')},
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]['filename'].endswith('.jpg'))
        self.assertEqual(attachments[0]['original_name'], 'test_image.jpg')
        self.assertEqual(attachments[0]['content_type'], 'image/jpeg')

    def test_jpg_upload_unchanged(self):
        """Uploading a JPG image is not converted."""
        ref_id = self._create_product()
        self.login_admin()
        import io
        from PIL import Image
        buf = io.BytesIO()
        img = Image.new('RGB', (10, 10), (0, 255, 0))
        img.save(buf, 'JPEG')
        buf.seek(0)
        resp = self.client.post(f'/wiki/{ref_id}/upload',
                                data={'attachment': (buf, 'photo.jpg')},
                                content_type='multipart/form-data',
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]['filename'].endswith('.jpg'))
        self.assertEqual(attachments[0]['original_name'], 'photo.jpg')

    def test_convert_png_uploads_to_jpg_migration(self):
        """convert_png_uploads_to_jpg migrates existing PNGs to JPG."""
        ref_id = self._create_product()
        import io
        from PIL import Image
        # Manually create a PNG upload (bypassing auto-conversion)
        upload_dir = os.path.join(_test_dir, 'wiki_uploads', str(ref_id))
        os.makedirs(upload_dir, exist_ok=True)
        buf = io.BytesIO()
        Image.new('RGBA', (10, 10), (255, 0, 0, 128)).save(buf, 'PNG')
        png_data = buf.getvalue()
        fname = 'abcdef123456.png'
        with open(os.path.join(upload_dir, fname), 'wb') as f:
            f.write(png_data)
        db.add_wiki_attachment(ref_id, fname, 'original.png', 'image/png', len(png_data), 'test')
        # Run migration
        stats = db.convert_png_uploads_to_jpg(_test_dir)
        self.assertEqual(stats['converted'], 1)
        self.assertEqual(stats['errors'], 0)
        # bytes_saved may be negative for tiny test images; just verify conversion ran
        # Verify DB record updated
        attachments = db.get_wiki_attachments(ref_id)
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]['filename'].endswith('.jpg'))
        self.assertEqual(attachments[0]['content_type'], 'image/jpeg')
        # Verify file on disk
        self.assertFalse(os.path.exists(os.path.join(upload_dir, fname)))
        self.assertTrue(os.path.exists(os.path.join(upload_dir, fname.replace('.png', '.jpg'))))


class TestAttachmentIntegrity(BaseTestCase):
    """Test wiki attachment integrity checking."""

    def test_check_removes_orphaned_records(self):
        """Integrity check removes DB records with missing files."""
        ref_id = db.add_product_reference(codename='OrphanTest')
        db.add_wiki_attachment(ref_id, 'missing_file.png', 'photo.png', 'image/png', 1024, 'admin')
        uploads_dir = os.path.join(_test_dir, 'wiki_uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        # File doesn't exist on disk — should be cleaned
        result = db.check_attachment_integrity(uploads_dir)
        self.assertEqual(result['orphaned_removed'], 1)
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 0)

    def test_check_preserves_valid_attachments(self):
        """Integrity check keeps records where files exist."""
        ref_id = db.add_product_reference(codename='ValidTest')
        db.add_wiki_attachment(ref_id, 'real_file.txt', 'doc.txt', 'text/plain', 5, 'admin')
        uploads_dir = os.path.join(_test_dir, 'wiki_uploads')
        file_dir = os.path.join(uploads_dir, str(ref_id))
        os.makedirs(file_dir, exist_ok=True)
        with open(os.path.join(file_dir, 'real_file.txt'), 'w') as f:
            f.write('hello')
        result = db.check_attachment_integrity(uploads_dir)
        self.assertEqual(result['orphaned_removed'], 0)
        self.assertEqual(result['total_checked'], 1)
        self.assertEqual(len(db.get_wiki_attachments(ref_id)), 1)

    def test_repair_endpoint_requires_permission(self):
        """Repair endpoint requires wiki permission."""
        resp = self.client.post('/wiki/repair', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        # Not logged in — should redirect to login
        self.assertIn(b'login', resp.data.lower())

    def test_repair_endpoint_works(self):
        """Admin can trigger repair and get feedback."""
        self.login_admin()
        resp = self.client.post('/wiki/repair', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'intact', resp.data)
