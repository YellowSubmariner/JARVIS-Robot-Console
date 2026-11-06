"""Credential storage uses only disposable synthetic credentials in tests."""
import os
import unittest
import uuid

from jarvis_camera.credentials import WindowsCredentialStore, credential_target


class CredentialTests(unittest.TestCase):
    def test_identity_is_scoped_by_address_and_user(self):
        self.assertEqual(credential_target(' EXAMPLE ', 'admin'), credential_target('example', 'admin'))
        self.assertNotEqual(credential_target('example', 'admin'), credential_target('example', 'operator'))
        self.assertNotEqual(credential_target('example', 'admin'), credential_target('other', 'admin'))

    @unittest.skipUnless(os.name == 'nt', 'requires Windows Credential Manager')
    def test_windows_store_persists_update_and_clear_without_application_file(self):
        # The randomly named record is solely synthetic and always deleted.
        host = 'unit-test-' + uuid.uuid4().hex
        user = 'fake-operator'
        store = WindowsCredentialStore()
        try:
            self.assertIsNone(store.load(host, user))
            store.save(host, user, 'unit-test-示例')
            self.assertEqual(WindowsCredentialStore().load(host, user), 'unit-test-示例')
            store.save(host, user, 'replacement')
            self.assertEqual(store.load(host, user), 'replacement')
            self.assertIsNone(store.load(host, 'other'))
        finally:
            store.delete(host, user)
        self.assertIsNone(store.load(host, user))
        store.delete(host, user)  # deleting an already absent credential is harmless


if __name__ == '__main__':
    unittest.main()
