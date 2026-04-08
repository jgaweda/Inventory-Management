from tests import BaseTestCase, db, json, os, _test_dir, ROLE_PERMISSIONS, GUEST_ASSIGNABLE_PERMISSIONS, has_permission, get_user_permissions, app, patch

import sqlite3


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


class TestAuthEdgeCases(BaseTestCase):
    """Test authentication edge cases."""

    def test_empty_username_login(self):
        resp = self.client.post('/login', data={
            'username': '', 'password': 'admin',
        }, follow_redirects=True)
        self.assertIn(b'Invalid', resp.data)

    def test_empty_password_login(self):
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': '',
        }, follow_redirects=True)
        self.assertIn(b'Invalid', resp.data)

    def test_nonexistent_user_login(self):
        resp = self.client.post('/login', data={
            'username': 'nobody', 'password': 'pass',
        }, follow_redirects=True)
        self.assertIn(b'Invalid username or password', resp.data)

    def test_session_invalid_user_id(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 99999
        resp = self.client.get('/devices/add', follow_redirects=True)
        self.assertIn(b'login', resp.data.lower())

    def test_change_password_wrong_current(self):
        self.login_admin()
        resp = self.client.post('/account', data={
            'current_password': 'wrongpass',
            'new_password': 'newpass',
            'confirm_password': 'newpass',
        }, follow_redirects=True)
        self.assertIn(b'incorrect', resp.data.lower())

    def test_change_password_mismatch(self):
        self.login_admin()
        resp = self.client.post('/account', data={
            'current_password': 'admin',
            'new_password': 'newpass1',
            'confirm_password': 'newpass2',
        }, follow_redirects=True)
        self.assertIn(b'match', resp.data.lower())

    def test_change_password_too_short(self):
        self.login_admin()
        resp = self.client.post('/account', data={
            'current_password': 'admin',
            'new_password': 'ab',
            'confirm_password': 'ab',
        }, follow_redirects=True)
        self.assertIn(b'4', resp.data)

    def test_change_password_success(self):
        self.login_admin()
        resp = self.client.post('/account', data={
            'current_password': 'admin',
            'new_password': 'newadmin1',
            'confirm_password': 'newadmin1',
        }, follow_redirects=True)
        self.assertIn(b'changed', resp.data.lower())
        self.client.get('/logout')
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'newadmin1',
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_cannot_delete_last_admin(self):
        with self.assertRaises(ValueError) as ctx:
            user = db.get_user_by_username('admin')
            db.delete_user(user['user_id'])
        self.assertIn('last admin', str(ctx.exception))

    def test_duplicate_username_rejected(self):
        with self.assertRaises(ValueError):
            db.create_user('admin', 'pass', role='custom')


class TestAdminPasswordRecovery(BaseTestCase):
    """Test admin password reset and emergency user creation."""

    def test_reset_admin_password(self):
        """reset_admin_password should change the admin's password."""
        username, created = db.reset_admin_password('newpass123')
        self.assertEqual(username, 'admin')
        self.assertFalse(created)
        self.assertIsNone(db.authenticate_user('admin', 'admin'))
        user = db.authenticate_user('admin', 'newpass123')
        self.assertIsNotNone(user)
        self.assertEqual(user['role'], 'admin')

    def test_reset_creates_admin_when_none_exist(self):
        """If no admin user exists, reset should create one."""
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute('DELETE FROM users')
        conn.commit()
        conn.close()
        username, created = db.reset_admin_password('rescue123')
        self.assertEqual(username, 'admin')
        self.assertTrue(created)
        user = db.authenticate_user('admin', 'rescue123')
        self.assertIsNotNone(user)
        self.assertEqual(user['role'], 'admin')

    def test_reset_targets_first_admin(self):
        """If multiple admins exist, reset should target the first one."""
        db.create_user('admin2', 'pass2', role='admin', display_name='Admin 2')
        username, _ = db.reset_admin_password('reset999')
        self.assertEqual(username, 'admin')
        self.assertIsNotNone(db.authenticate_user('admin2', 'pass2'))

    def test_login_after_reset(self):
        """Full integration: reset password then log in via web."""
        db.reset_admin_password('weblogin')
        resp = self.client.post('/login', data={
            'username': 'admin', 'password': 'weblogin',
        }, follow_redirects=True)
        self.assertIn(b'Barcode Scanner', resp.data)


class TestUserManagementEdgeCases(BaseTestCase):
    """Test user management edge cases."""

    def test_create_user_with_all_roles(self):
        uid_admin = db.create_user('test_admin2', 'pass1234', role='admin')
        user = db.get_user(uid_admin)
        self.assertEqual(user['role'], 'admin')

        uid_custom = db.create_user('test_custom', 'pass1234', role='custom',
                                    permissions=['devices', 'wiki'])
        user = db.get_user(uid_custom)
        self.assertEqual(user['role'], 'custom')

    def test_update_user_role(self):
        uid = db.create_user('roletest', 'pass1234', role='admin')
        db.update_user(uid, {'role': 'custom'})
        user = db.get_user(uid)
        self.assertEqual(user['role'], 'custom')

    def test_update_user_password(self):
        uid = db.create_user('pwtest', 'oldpass1', role='custom')
        db.update_user(uid, {'password': 'newpass1'})
        self.assertIsNone(db.authenticate_user('pwtest', 'oldpass1'))
        self.assertIsNotNone(db.authenticate_user('pwtest', 'newpass1'))

    def test_delete_non_last_admin(self):
        uid2 = db.create_user('admin2', 'pass1234', role='admin')
        db.delete_user(uid2)
        self.assertIsNone(db.get_user(uid2))

    def test_delete_nonexistent_user(self):
        with self.assertRaises(ValueError):
            db.delete_user(99999)

    def test_admin_user_list_page(self):
        self.login_admin()
        resp = self.client.get('/account')
        self.assertIn(b'User Management', resp.data)
        self.assertIn(b'admin', resp.data)

    def test_add_user_via_web(self):
        self.login_admin()
        resp = self.client.post('/users/add', data={
            'username': 'newuser', 'password': 'pass1234',
            'display_name': 'New User', 'role': 'custom',
            'permissions': ['devices', 'wiki'],
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        user = db.get_user_by_username('newuser')
        self.assertIsNotNone(user)
        self.assertEqual(user['role'], 'custom')


class TestUserEditDeleteRoutes(BaseTestCase):
    """Test /users/<id>/edit and /users/<id>/delete routes."""

    def test_edit_user_form_loads(self):
        self.login_admin()
        uid = db.create_user('editme', 'pass1234', role='custom', display_name='Edit Me')
        resp = self.client.get(f'/users/{uid}/edit')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'editme', resp.data)

    def test_edit_user_updates_role(self):
        self.login_admin()
        uid = db.create_user('rolechange', 'pass1234', role='custom')
        resp = self.client.post(f'/users/{uid}/edit', data={
            'display_name': 'Role Changed', 'role': 'admin',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        user = db.get_user(uid)
        self.assertEqual(user['role'], 'admin')
        self.assertEqual(user['display_name'], 'Role Changed')

    def test_edit_user_updates_password(self):
        self.login_admin()
        uid = db.create_user('pwchange', 'oldpass1', role='custom')
        self.client.post(f'/users/{uid}/edit', data={
            'display_name': 'PW Changed', 'role': 'custom', 'password': 'newpass1',
        }, follow_redirects=True)
        self.assertIsNone(db.authenticate_user('pwchange', 'oldpass1'))
        self.assertIsNotNone(db.authenticate_user('pwchange', 'newpass1'))

    def test_edit_user_short_password_rejected(self):
        self.login_admin()
        uid = db.create_user('shortpw', 'pass1234', role='custom')
        resp = self.client.post(f'/users/{uid}/edit', data={
            'display_name': 'Short PW', 'role': 'custom', 'password': 'ab',
        }, follow_redirects=True)
        self.assertIn(b'4 characters', resp.data)

    def test_edit_nonexistent_user(self):
        self.login_admin()
        resp = self.client.get('/users/99999/edit', follow_redirects=True)
        self.assertIn(b'User not found', resp.data)

    def test_delete_user_via_web(self):
        self.login_admin()
        uid = db.create_user('deleteme', 'pass1234', role='custom')
        resp = self.client.post(f'/users/{uid}/delete', follow_redirects=True)
        self.assertIn(b'deleted', resp.data.lower())
        self.assertIsNone(db.get_user(uid))

    def test_delete_last_admin_via_web(self):
        self.login_admin()
        admin = db.get_user_by_username('admin')
        resp = self.client.post(f'/users/{admin["user_id"]}/delete', follow_redirects=True)
        self.assertIn(b'last admin', resp.data.lower())
        # Admin should still exist
        self.assertIsNotNone(db.get_user_by_username('admin'))


class TestRoleGranularity(BaseTestCase):
    """Test editor role permissions."""

    def _create_editor(self):
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'editor1', 'password': 'test',
            'display_name': 'Editor One', 'role': 'custom',
            'permissions': ['devices', 'wiki'],
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
            'username': 'viewer2', 'password': 'test', 'role': 'custom',
            'permissions': ['wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={
            'username': 'viewer2', 'password': 'test',
        })
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'category': 'Router',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)


class TestPowerUserRole(BaseTestCase):
    """Test power_user role — can manage product references but not devices/users."""

    def _create_power_user(self):
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'pu1', 'password': 'test',
            'display_name': 'Power User 1', 'role': 'custom',
            'permissions': ['references', 'wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={
            'username': 'pu1', 'password': 'test',
        })

    def test_power_user_can_add_reference(self):
        """Power user can add a product reference."""
        self._create_power_user()
        resp = self.client.post('/reference/add', data={
            'codename': 'PUTestRef',
            'model_name': 'Test Model',
            'print_technology': 'Ink',
        }, follow_redirects=True)
        self.assertIn(b'PUTestRef', resp.data)
        self.assertIn(b'added', resp.data)

    def test_power_user_can_edit_reference(self):
        """Power user can edit a product reference."""
        self._create_power_user()
        db.add_product_reference(codename='EditMe')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.post(f'/reference/{ref_id}/edit', data={
            'codename': 'EditMe',
            'model_name': 'Updated Model',
            'wifi_gen': 'Wi-Fi 6',
            'year': '2024',
            'chip_manufacturer': '',
            'chip_codename': '',
            'fw_codebase': '',
            'print_technology': 'Laser',
        }, follow_redirects=True)
        self.assertIn(b'updated', resp.data)

    def test_power_user_can_inline_edit(self):
        """Power user can inline-edit a product reference field."""
        self._create_power_user()
        db.add_product_reference(codename='InlineTest')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.patch(f'/api/reference/{ref_id}',
                                 json={'model_name': 'New Model'},
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_power_user_can_delete_reference(self):
        """Power user can delete a product reference."""
        self._create_power_user()
        db.add_product_reference(codename='DeleteMe')
        refs = db.get_all_product_references()
        ref_id = refs[0]['ref_id']
        resp = self.client.post(f'/reference/{ref_id}/delete',
                                follow_redirects=True)
        self.assertIn(b'deleted', resp.data)

    def test_power_user_cannot_add_device(self):
        """Power user cannot add devices (not an editor)."""
        self._create_power_user()
        resp = self.client.post('/devices/add', data={
            'manufacturer': 'HP', 'category': 'Router',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_power_user_cannot_manage_users(self):
        """Power user cannot access user management."""
        self._create_power_user()
        resp = self.client.get('/users', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_power_user_sees_reference_controls(self):
        """Power user should see import/add buttons on product reference page."""
        self._create_power_user()
        db.add_product_reference(codename='VisTest')
        resp = self.client.get('/reference')
        self.assertIn(b'Import', resp.data)
        self.assertIn(b'Add Product', resp.data)

    def test_viewer_cannot_manage_references(self):
        """Viewer cannot add product references."""
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'v1', 'password': 'test', 'role': 'custom',
            'permissions': ['wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={'username': 'v1', 'password': 'test'})
        resp = self.client.post('/reference/add', data={
            'codename': 'ShouldFail',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_editor_cannot_manage_references(self):
        """Editor (devices/wiki only) cannot manage product references."""
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'ed1', 'password': 'test', 'role': 'custom',
            'permissions': ['devices', 'wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={'username': 'ed1', 'password': 'test'})
        resp = self.client.post('/reference/add', data={
            'codename': 'EditorShouldFail',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)


class TestPowerUserPermissions(BaseTestCase):
    """Test power_user role boundary cases — backups and checkout denial."""

    def _create_power_user(self):
        self.login_admin()
        self.client.post('/users/add', data={
            'username': 'puser', 'password': 'test1234', 'role': 'custom',
            'permissions': ['references', 'wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={'username': 'puser', 'password': 'test1234'})

    def test_power_user_cannot_access_backups(self):
        self._create_power_user()
        resp = self.client.get('/backups', follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_power_user_cannot_checkout(self):
        self.login_admin()
        did = db.add_device({'name': 'Checkout Test'})
        self.client.post('/users/add', data={
            'username': 'puser', 'password': 'test1234', 'role': 'custom',
            'permissions': ['references', 'wiki'],
        })
        self.client.get('/logout')
        self.client.post('/login', data={'username': 'puser', 'password': 'test1234'})
        resp = self.client.post(f'/devices/{did}/checkout', data={
            'assigned_to': 'Someone',
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)


class TestPermissionModel(BaseTestCase):
    """Test the centralized ROLE_PERMISSIONS system."""

    def test_all_roles_defined(self):
        """Only 'admin' and 'custom' roles must be in ROLE_PERMISSIONS."""
        for role in ['admin', 'custom']:
            self.assertIn(role, ROLE_PERMISSIONS, f'{role} missing from ROLE_PERMISSIONS')
        self.assertEqual(set(ROLE_PERMISSIONS.keys()), {'admin', 'custom'})

    def test_admin_has_all_permissions(self):
        """Admin should have every permission defined in ROLE_PERMISSIONS."""
        admin_perms = ROLE_PERMISSIONS['admin']
        self.assertIn('devices', admin_perms)
        self.assertIn('references', admin_perms)
        self.assertIn('users', admin_perms)
        self.assertIn('backups', admin_perms)
        self.assertIn('logs', admin_perms)
        self.assertIn('settings', admin_perms)
        self.assertIn('wiki', admin_perms)

    def test_custom_user_gets_per_user_permissions(self):
        """Custom users should get permissions from their permissions list."""
        uid = db.create_user('custom1', 'pass1234', role='custom',
                             permissions=['devices', 'wiki'])
        user = db.get_user(uid)
        perms = get_user_permissions(user)
        self.assertIn('devices', perms)
        self.assertIn('wiki', perms)
        self.assertNotIn('references', perms)
        self.assertNotIn('users', perms)

    def test_custom_user_references_permissions(self):
        """Custom user with references/wiki permissions."""
        uid = db.create_user('custom2', 'pass1234', role='custom',
                             permissions=['references', 'wiki'])
        user = db.get_user(uid)
        perms = get_user_permissions(user)
        self.assertIn('references', perms)
        self.assertIn('wiki', perms)
        self.assertNotIn('devices', perms)
        self.assertNotIn('users', perms)

    def test_custom_user_no_permissions(self):
        """Custom user with empty permissions list has no permissions."""
        uid = db.create_user('custom3', 'pass1234', role='custom',
                             permissions=[])
        user = db.get_user(uid)
        perms = get_user_permissions(user)
        self.assertNotIn('devices', perms)
        self.assertNotIn('references', perms)
        self.assertNotIn('users', perms)

    def test_get_user_permissions_admin(self):
        """get_user_permissions returns full set for admin."""
        user = db.get_user_by_username('admin')
        perms = get_user_permissions(user)
        self.assertIn('devices', perms)
        self.assertIn('users', perms)
        self.assertIn('backups', perms)

    def test_has_permission_with_custom_user(self):
        """has_permission should check per-user permissions for custom role."""
        with self.app.test_request_context():
            from flask import g
            g.user = {'role': 'custom', 'permissions': ['devices', 'wiki']}
            self.assertTrue(has_permission('devices'))
            self.assertFalse(has_permission('backups'))

    def test_has_permission_no_user_default(self):
        """has_permission should return False with no user and no guest permissions."""
        with self.app.test_request_context():
            from flask import g
            g.user = None
            # Clear any guest permissions
            db.save_guest_permissions(set())
            self.assertFalse(has_permission('devices'))
            self.assertFalse(has_permission('wiki'))

    def test_has_permission_no_user_with_guest_perms(self):
        """has_permission should check guest permissions when no user logged in."""
        with self.app.test_request_context():
            from flask import g
            g.user = None
            db.save_guest_permissions({'wiki', 'references'})
            self.assertTrue(has_permission('wiki'))
            self.assertTrue(has_permission('references'))
            self.assertFalse(has_permission('devices'))
            self.assertFalse(has_permission('backups'))
            # Clean up
            db.save_guest_permissions(set())


class TestGuestPermissions(BaseTestCase):
    """Test guest/public user permissions system."""

    def test_guest_permissions_default_references_wiki(self):
        """Guest permissions should default to references and wiki."""
        perms = db.get_guest_permissions()
        self.assertEqual(perms, {'references', 'wiki'})

    def test_save_and_load_guest_permissions(self):
        """Guest permissions round-trip through database."""
        db.save_guest_permissions({'wiki', 'references'})
        perms = db.get_guest_permissions()
        self.assertEqual(perms, {'wiki', 'references'})

    def test_save_empty_guest_permissions(self):
        """Saving empty permissions clears all guest access."""
        db.save_guest_permissions({'wiki'})
        db.save_guest_permissions(set())
        perms = db.get_guest_permissions()
        self.assertEqual(perms, set())

    def test_get_user_permissions_guest(self):
        """get_user_permissions(None) should return guest permissions."""
        db.save_guest_permissions({'references'})
        perms = get_user_permissions(None)
        self.assertIn('references', perms)
        self.assertNotIn('devices', perms)
        db.save_guest_permissions(set())

    def test_admin_can_save_guest_permissions(self):
        """Admin can update guest permissions via the settings route."""
        self.login_admin()
        resp = self.client.post('/settings/guest-permissions', data={
            'guest_permissions': ['wiki', 'references'],
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Public access permissions saved', resp.data)
        perms = db.get_guest_permissions()
        self.assertEqual(perms, {'wiki', 'references'})

    def test_non_admin_cannot_save_guest_permissions(self):
        """Non-admin user cannot update guest permissions."""
        db.create_user('viewer', 'test1234', role='custom', permissions=['wiki'])
        self.client.post('/login', data={'username': 'viewer', 'password': 'test1234'})
        resp = self.client.post('/settings/guest-permissions', data={
            'guest_permissions': ['devices'],
        }, follow_redirects=True)
        self.assertIn(b'do not have permission', resp.data)

    def test_guest_cannot_save_guest_permissions(self):
        """Non-logged-in user cannot update guest permissions."""
        resp = self.client.post('/settings/guest-permissions', data={
            'guest_permissions': ['devices'],
        }, follow_redirects=True)
        self.assertIn(b'log in', resp.data.lower())

    def test_invalid_permissions_filtered(self):
        """Invalid permission keys are filtered out when saving."""
        self.login_admin()
        self.client.post('/settings/guest-permissions', data={
            'guest_permissions': ['wiki', 'devices', 'retire', 'backups', 'settings', 'fake_perm'],
        }, follow_redirects=True)
        perms = db.get_guest_permissions()
        # Only wiki should be saved (others not in GUEST_ASSIGNABLE)
        self.assertIn('wiki', perms)
        self.assertNotIn('devices', perms)
        self.assertNotIn('retire', perms)
        self.assertNotIn('backups', perms)
        self.assertNotIn('settings', perms)
        self.assertNotIn('fake_perm', perms)

    def test_guest_with_permission_can_access_protected_route(self):
        """Guest with wiki permission can access wiki edit routes."""
        db.save_guest_permissions({'references'})
        # Guest should be able to access add reference form (permission_required('references'))
        resp = self.client.get('/reference/add')
        self.assertEqual(resp.status_code, 200)
        db.save_guest_permissions(set())

    def test_guest_without_permission_redirected_to_login(self):
        """Guest without permission is redirected to login."""
        db.save_guest_permissions(set())
        resp = self.client.get('/reference/add', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_login_required_ignores_guest_permissions(self):
        """Routes with @login_required always require login, regardless of guest permissions."""
        db.save_guest_permissions({'references', 'wiki'})
        # /account uses @login_required, not @permission_required
        resp = self.client.get('/account', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])
        db.save_guest_permissions(set())

    def test_user_management_shows_guest_permissions_card(self):
        """User management page shows the Public Access card."""
        self.login_admin()
        resp = self.client.get('/users')
        self.assertIn(b'Public Access', resp.data)
        self.assertIn(b'guest_permissions', resp.data)

    def test_settings_page_does_not_show_guest_permissions(self):
        """Settings page should not show the Public Access card (moved to user management)."""
        self.login_admin()
        resp = self.client.get('/account')
        self.assertNotIn(b'save_guest_permissions', resp.data)

    def test_guest_assignable_excludes_sensitive(self):
        """GUEST_ASSIGNABLE_PERMISSIONS should not include sensitive or destructive permissions."""
        guest_keys = {k for k, _ in GUEST_ASSIGNABLE_PERMISSIONS}
        self.assertNotIn('backups', guest_keys)
        self.assertNotIn('logs', guest_keys)
        self.assertNotIn('settings', guest_keys)
        self.assertNotIn('users', guest_keys)
        self.assertNotIn('devices', guest_keys)
        self.assertNotIn('retire', guest_keys)
