import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import account_store
from account_store import AccountStoreError, InvalidAuthFileError, ProfileStore, validate_auth_file


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.app_directory = self.root / "app"
        self.app_directory.mkdir()
        self.store = ProfileStore(self.app_directory)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_json(self, path: Path, value) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_validate_auth_rejects_invalid_json_and_non_object_root(self) -> None:
        invalid = self.root / "invalid.json"
        invalid.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(InvalidAuthFileError):
            validate_auth_file(invalid)

        array_root = self.write_json(self.root / "array.json", ["token"])
        with self.assertRaises(InvalidAuthFileError):
            validate_auth_file(array_root)

    def test_import_persists_profile_and_detects_duplicate(self) -> None:
        source = self.write_json(self.root / "auth.json", {"token": "secret-a"})
        first = self.store.import_profile(source, " 工作 账号 ")

        self.assertTrue(first.created)
        self.assertEqual(first.profile.remark, "工作 账号")
        self.assertEqual(first.profile.added_at, first.profile.updated_at)
        self.assertTrue(self.store.profile_path(first.profile).is_file())
        self.assertEqual(len(self.store.load_profiles()), 1)

        duplicate = self.store.import_profile(source, "另一个备注")
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.profile.profile_id, first.profile.profile_id)
        self.assertEqual(len(self.store.load_profiles()), 1)

    def test_switch_replaces_target_and_creates_backup(self) -> None:
        selected = self.write_json(self.root / "selected.json", {"token": "new-secret"})
        profile = self.store.import_profile(selected, "个人账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "old-secret"})
        old_bytes = target.read_bytes()

        result = self.store.switch_profile(profile.profile_id, target)

        self.assertTrue(result.changed)
        self.assertEqual(target.read_bytes(), selected.read_bytes())
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(result.backup_path.read_bytes(), old_bytes)
        self.assertEqual(self.store.active_profile_id(target), profile.profile_id)

        backups_before = list(self.store.backup_directory.glob("*.json"))
        second = self.store.switch_profile(profile.profile_id, target)
        backups_after = list(self.store.backup_directory.glob("*.json"))
        self.assertFalse(second.changed)
        self.assertIsNone(second.backup_path)
        self.assertEqual(backups_before, backups_after)

    def test_switch_rejects_modified_stored_profile(self) -> None:
        source = self.write_json(self.root / "auth.json", {"token": "original"})
        profile = self.store.import_profile(source, "工作账号").profile
        self.write_json(self.store.profile_path(profile), {"token": "tampered"})

        with self.assertRaises(AccountStoreError):
            self.store.switch_profile(profile.profile_id, self.root / ".codex" / "auth.json")

    def test_switch_restores_original_when_post_copy_verification_fails(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "new"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "old"})
        original_bytes = target.read_bytes()
        original_sha256 = account_store._sha256_file
        target_reads = 0

        def fail_second_target_read(path):
            nonlocal target_reads
            if Path(path) == target:
                target_reads += 1
                if target_reads == 2:
                    raise OSError("simulated verification failure")
            return original_sha256(path)

        with mock.patch("account_store._sha256_file", side_effect=fail_second_target_read):
            with self.assertRaisesRegex(AccountStoreError, "已恢复原认证文件"):
                self.store.switch_profile(profile.profile_id, target)

        self.assertEqual(target.read_bytes(), original_bytes)

    def test_completed_switch_can_be_rolled_back_for_larger_transaction(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "new"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "old"})
        original = target.read_bytes()

        result = self.store.switch_profile(profile.profile_id, target)
        self.store.rollback_switch(result)

        self.assertEqual(target.read_bytes(), original)

    def test_completed_switch_to_missing_target_can_be_rolled_back(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "new"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.root / ".codex" / "auth.json"

        result = self.store.switch_profile(profile.profile_id, target)
        self.store.rollback_switch(result)

        self.assertFalse(target.exists())

    def test_load_uses_manifest_backup_when_primary_is_corrupt(self) -> None:
        first_source = self.write_json(self.root / "first.json", {"token": "one"})
        second_source = self.write_json(self.root / "second.json", {"token": "two"})
        first = self.store.import_profile(first_source, "账号一").profile
        self.store.import_profile(second_source, "账号二")
        self.store.manifest_path.write_text("{broken", encoding="utf-8")

        recovered = self.store.load_profiles()

        self.assertTrue(self.store.recovered_from_backup)
        self.assertEqual([profile.profile_id for profile in recovered], [first.profile_id])

    def test_load_migrates_legacy_profile_without_updated_at(self) -> None:
        source = self.write_json(self.root / "auth.json", {"token": "legacy"})
        profile = self.store.import_profile(source, "旧版账号").profile
        payload = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        del payload["profiles"][0]["updated_at"]
        self.store.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        loaded = self.store.load_profiles()[0]

        self.assertEqual(loaded.profile_id, profile.profile_id)
        self.assertEqual(loaded.updated_at, loaded.added_at)

    def test_rename_update_and_remove_profile(self) -> None:
        original = self.write_json(self.root / "original.json", {"token": "old"})
        profile = self.store.import_profile(original, "旧备注").profile
        stored_path = self.store.profile_path(profile)

        renamed = self.store.rename_profile(profile.profile_id, " 新 备注 ")
        self.assertEqual(renamed.remark, "新 备注")
        self.assertEqual(self.store.load_profiles()[0].remark, "新 备注")

        current = self.write_json(self.root / ".codex" / "auth.json", {"token": "fresh"})
        update = self.store.update_profile(profile.profile_id, current)

        self.assertTrue(update.changed)
        self.assertIsNotNone(update.backup_path)
        self.assertEqual(update.backup_path.read_bytes(), original.read_bytes())
        self.assertEqual(stored_path.read_bytes(), current.read_bytes())
        self.assertEqual(update.profile.sha256, validate_auth_file(current))

        unchanged = self.store.update_profile(profile.profile_id, current)
        self.assertFalse(unchanged.changed)
        self.assertIsNone(unchanged.backup_path)

        removed = self.store.remove_profile(profile.profile_id)
        self.assertEqual(removed.profile_id, profile.profile_id)
        self.assertEqual(self.store.load_profiles(), [])
        self.assertFalse(stored_path.exists())
        self.assertTrue(current.is_file())

    def test_update_rejects_content_already_saved_for_another_profile(self) -> None:
        first_source = self.write_json(self.root / "first.json", {"token": "one"})
        second_source = self.write_json(self.root / "second.json", {"token": "two"})
        first = self.store.import_profile(first_source, "账号一").profile
        second = self.store.import_profile(second_source, "账号二").profile
        first_stored_bytes = self.store.profile_path(first).read_bytes()

        with self.assertRaisesRegex(AccountStoreError, second.remark):
            self.store.update_profile(first.profile_id, second_source)

        self.assertEqual(self.store.profile_path(first).read_bytes(), first_stored_bytes)

    def test_remove_profile_with_missing_file_still_cleans_manifest(self) -> None:
        source = self.write_json(self.root / "auth.json", {"token": "secret"})
        profile = self.store.import_profile(source, "待移除账号").profile
        self.store.profile_path(profile).unlink()

        self.store.remove_profile(profile.profile_id)

        self.assertEqual(self.store.load_profiles(), [])

    def test_update_restores_a_missing_profile_file(self) -> None:
        source = self.write_json(self.root / "original.json", {"token": "old"})
        profile = self.store.import_profile(source, "可恢复账号").profile
        stored_path = self.store.profile_path(profile)
        stored_path.unlink()
        current = self.write_json(self.root / ".codex" / "auth.json", {"token": "new"})

        result = self.store.update_profile(profile.profile_id, current)

        self.assertTrue(result.changed)
        self.assertIsNone(result.backup_path)
        self.assertEqual(stored_path.read_bytes(), current.read_bytes())


if __name__ == "__main__":
    unittest.main()
