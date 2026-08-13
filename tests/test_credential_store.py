import sys
import tempfile
import unittest
from pathlib import Path

from credential_store import (
    InvalidApiKeyError,
    WindowsDataProtector,
    credential_target_for_codex_home,
    legacy_migration_marker_target_for_codex_home,
    masked_api_key,
    migrate_legacy_credential,
    normalize_api_key,
    reconcile_active_credential,
)


class MemoryCredentialStore:
    def __init__(self, value=None) -> None:
        self.value = value
        self.write_error = None
        self.delete_error = None

    def read(self):
        return self.value

    def write(self, value):
        if self.write_error is not None:
            raise self.write_error
        self.value = value

    def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.value = None


class CredentialHelpersTests(unittest.TestCase):
    def test_credential_target_is_stable_and_scoped_to_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            first_target = credential_target_for_codex_home(first)
            self.assertEqual(first_target, credential_target_for_codex_home(first))
            self.assertNotEqual(first_target, credential_target_for_codex_home(second))
            self.assertTrue(first_target.startswith("CodexSwitcher.DeepSeekApiKey."))

    def test_v14_legacy_credential_copies_once_to_scoped_target(self) -> None:
        scoped = MemoryCredentialStore()
        legacy = MemoryCredentialStore("sk-legacy-test")
        marker = MemoryCredentialStore()

        result = migrate_legacy_credential(
            scoped, legacy, marker, allow_claim=True, canonical_owner=True
        )

        self.assertEqual(result, "sk-legacy-test")
        self.assertEqual(scoped.value, "sk-legacy-test")
        self.assertEqual(legacy.value, "sk-legacy-test")
        self.assertEqual(marker.value, "sk-migrated-v15")

    def test_existing_scoped_credential_is_not_overwritten_by_legacy(self) -> None:
        scoped = MemoryCredentialStore("sk-scoped-test")
        legacy = MemoryCredentialStore("sk-stale-legacy")
        marker = MemoryCredentialStore()

        result = migrate_legacy_credential(
            scoped, legacy, marker, allow_claim=True, canonical_owner=True
        )

        self.assertEqual(result, "sk-scoped-test")
        self.assertEqual(scoped.value, "sk-scoped-test")
        self.assertEqual(legacy.value, "sk-stale-legacy")
        self.assertEqual(marker.value, "sk-migrated-v15")

    def test_unowned_custom_home_does_not_claim_legacy_credential(self) -> None:
        scoped = MemoryCredentialStore()
        legacy = MemoryCredentialStore("sk-legacy-test")
        marker = MemoryCredentialStore()

        result = migrate_legacy_credential(scoped, legacy, marker, allow_claim=False)

        self.assertIsNone(result)
        self.assertIsNone(scoped.value)
        self.assertEqual(legacy.value, "sk-legacy-test")

    def test_failed_marker_write_preserves_both_credential_copies(self) -> None:
        from credential_store import CredentialStoreError

        scoped = MemoryCredentialStore()
        legacy = MemoryCredentialStore("sk-legacy-test")
        marker = MemoryCredentialStore()
        marker.write_error = CredentialStoreError("marker failed")

        with self.assertRaises(CredentialStoreError):
            migrate_legacy_credential(
                scoped, legacy, marker, allow_claim=True, canonical_owner=True
            )

        self.assertEqual(scoped.value, "sk-legacy-test")
        self.assertEqual(legacy.value, "sk-legacy-test")

    def test_custom_home_does_not_delete_another_homes_legacy_key(self) -> None:
        scoped = MemoryCredentialStore("sk-custom-active")
        legacy = MemoryCredentialStore("sk-default-legacy")
        marker = MemoryCredentialStore()

        result = migrate_legacy_credential(
            scoped,
            legacy,
            marker,
            allow_claim=True,
            canonical_owner=False,
            trusted_active_key="sk-custom-active",
        )

        self.assertEqual(result, "sk-custom-active")
        self.assertEqual(legacy.value, "sk-default-legacy")

    def test_unowned_custom_home_does_not_delete_equal_legacy_key(self) -> None:
        scoped = MemoryCredentialStore("sk-shared-key")
        legacy = MemoryCredentialStore("sk-shared-key")
        marker = MemoryCredentialStore()

        result = migrate_legacy_credential(
            scoped,
            legacy,
            marker,
            allow_claim=False,
            canonical_owner=False,
        )

        self.assertEqual(result, "sk-shared-key")
        self.assertEqual(legacy.value, "sk-shared-key")

    def test_migration_marker_prevents_deleted_scoped_key_from_resurrecting(self) -> None:
        scoped = MemoryCredentialStore()
        legacy = MemoryCredentialStore("sk-legacy-test")
        marker = MemoryCredentialStore("sk-migrated-v15")

        result = migrate_legacy_credential(
            scoped, legacy, marker, allow_claim=True, canonical_owner=True
        )

        self.assertIsNone(result)
        self.assertIsNone(scoped.value)
        self.assertEqual(legacy.value, "sk-legacy-test")

    def test_legacy_marker_target_is_scoped_to_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".codex"
            self.assertEqual(
                legacy_migration_marker_target_for_codex_home(home),
                credential_target_for_codex_home(home) + ".LegacyMigrated.v15",
            )

    def test_active_managed_key_reconciles_saved_credential(self) -> None:
        scoped = MemoryCredentialStore("sk-stale-test")

        result = reconcile_active_credential(scoped, "sk-active-test")

        self.assertEqual(result, "sk-active-test")
        self.assertEqual(scoped.value, "sk-active-test")

    def test_api_key_validation_and_masking(self) -> None:
        self.assertEqual(normalize_api_key("  sk-example-1234  "), "sk-example-1234")
        self.assertEqual(masked_api_key("sk-example-1234"), "已保存 ····1234")
        self.assertEqual(masked_api_key(None), "未保存")

    def test_api_key_rejects_unsafe_values(self) -> None:
        for value in (
            "",
            "token",
            "sk-a",
            "sk-line\nbreak",
            'sk-bad"key',
            "sk-" + "a" * 1024,
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidApiKeyError):
                    normalize_api_key(value)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
    def test_dpapi_round_trip_is_user_scoped_and_not_plain_text(self) -> None:
        protector = WindowsDataProtector()
        original = b"provider-state-without-real-credentials"

        protected = protector.protect(original)

        self.assertNotEqual(protected, original)
        self.assertNotIn(original, protected)
        self.assertEqual(protector.unprotect(protected), original)


if __name__ == "__main__":
    unittest.main()
