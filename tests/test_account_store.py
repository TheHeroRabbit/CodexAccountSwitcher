import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import account_store
from account_store import (
    SYNC_STATUS_UNCHANGED,
    SYNC_STATUS_UNMATCHED,
    SYNC_STATUS_UNSUPPORTED,
    SYNC_STATUS_UPDATED,
    AccountStoreError,
    InvalidAuthFileError,
    ProfileStore,
    validate_auth_file,
)


def _hold_account_switch_lock(app_directory, target_auth_path, ready, release) -> None:
    store = ProfileStore(Path(app_directory), lock_timeout=5.0)
    with store.account_switch_guard(Path(target_auth_path)):
        ready.set()
        release.wait(5.0)


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

    @staticmethod
    def managed_auth(account_id: str, generation: str):
        return {
            "auth_mode": "chatgpt",
            "last_refresh": "2026-08-0{}T00:00:00Z".format(generation),
            "OPENAI_API_KEY": None,
            "tokens": {
                "account_id": account_id,
                "access_token": "access-{}".format(generation),
                "id_token": "identity-{}".format(generation),
                "refresh_token": "refresh-{}".format(generation),
            },
        }

    def test_validate_auth_rejects_invalid_json_and_non_object_root(self) -> None:
        invalid = self.root / "invalid.json"
        invalid.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(InvalidAuthFileError):
            validate_auth_file(invalid)

        array_root = self.write_json(self.root / "array.json", ["token"])
        with self.assertRaises(InvalidAuthFileError):
            validate_auth_file(array_root)

    def test_validate_auth_bounded_read_rejects_file_that_grows_after_stat(self) -> None:
        oversized = self.root / "oversized.json"
        with oversized.open("wb") as stream:
            stream.write(b'{"token":"')
            stream.seek(account_store.MAX_AUTH_FILE_BYTES)
            stream.write(b'"}')

        real_stat = Path.stat

        def report_initial_size(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if Path(path) != oversized:
                return result
            values = list(result)
            values[6] = account_store.MAX_AUTH_FILE_BYTES
            return os.stat_result(values)

        with mock.patch.object(Path, "stat", report_initial_size):
            with self.assertRaisesRegex(InvalidAuthFileError, "超过 10 MB"):
                validate_auth_file(oversized)

    def test_invalid_unicode_account_id_is_valid_json_but_not_auto_matched(self) -> None:
        malformed_identity = self.write_json(
            self.root / "malformed-identity.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"account_id": "\ud800"},
            },
        )
        profile = self.store.import_profile(malformed_identity, "不可识别身份").profile

        result = self.store.sync_current_profile(malformed_identity)

        self.assertEqual(result.status, SYNC_STATUS_UNCHANGED)
        self.assertEqual(result.profile.profile_id, profile.profile_id)

        changed = self.write_json(
            self.root / "malformed-identity-changed.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "\ud800",
                    "access_token": "different-test-value",
                },
            },
        )
        unsupported = self.store.sync_current_profile(changed)
        self.assertEqual(unsupported.status, SYNC_STATUS_UNSUPPORTED)
        self.assertFalse(unsupported.changed)

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

    def test_import_prevents_duplicate_managed_account_after_token_rotation(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        first = self.store.import_profile(original, "账号 A")
        rotated = self.write_json(
            self.root / "rotated.json",
            self.managed_auth("account-a", "2"),
        )

        duplicate = self.store.import_profile(rotated, "账号 A 重复")

        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.profile.profile_id, first.profile.profile_id)
        self.assertEqual(len(self.store.load_profiles()), 1)
        self.assertEqual(self.store.profile_path(first.profile).read_bytes(), original.read_bytes())

    def test_import_does_not_hide_modified_copy_behind_manifest_hash(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        self.write_json(
            self.store.profile_path(profile),
            self.managed_auth("account-a", "2"),
        )

        with self.assertRaisesRegex(AccountStoreError, "已在外部修改"):
            self.store.import_profile(original, "账号 A 再次添加")

    def test_import_does_not_duplicate_a_modified_legacy_copy(self) -> None:
        original = self.write_json(self.root / "original.json", {"token": "original"})
        profile = self.store.import_profile(original, "旧格式账号").profile
        changed = self.write_json(self.root / "changed.json", {"token": "changed"})
        self.store.profile_path(profile).write_bytes(changed.read_bytes())

        with self.assertRaisesRegex(AccountStoreError, "已在外部修改"):
            self.store.import_profile(changed, "重复旧格式账号")

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

        def fail_post_copy_target_read(path):
            nonlocal target_reads
            if Path(path) == target:
                target_reads += 1
                if target_reads == 3:
                    raise OSError("simulated verification failure")
            return original_sha256(path)

        with mock.patch("account_store._sha256_file", side_effect=fail_post_copy_target_read):
            with self.assertRaisesRegex(AccountStoreError, "已恢复原认证文件"):
                self.store.switch_profile(profile.profile_id, target)

        self.assertEqual(target.read_bytes(), original_bytes)

    def test_switch_compare_and_swap_preserves_target_changed_after_backup(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "selected"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "old"})
        newer_payload = {"token": "newer-external-login"}
        newer_bytes = json.dumps(newer_payload).encode("utf-8")
        original_atomic_copy = account_store._atomic_copy

        def mutate_target_after_backup(copy_source, destination, **kwargs):
            result = original_atomic_copy(copy_source, destination, **kwargs)
            if (
                Path(copy_source) == target
                and Path(destination).parent == self.store.backup_directory
            ):
                self.write_json(target, newer_payload)
            return result

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_target_after_backup,
        ):
            with self.assertRaisesRegex(AccountStoreError, "备份后发生变化"):
                self.store.switch_profile(profile.profile_id, target)

        self.assertEqual(target.read_bytes(), newer_bytes)

    def test_switch_failure_does_not_restore_over_a_post_write_external_login(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "selected"})
        profile = self.store.import_profile(source, "工作账号").profile
        stored_path = self.store.profile_path(profile)
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "original"})
        original_bytes = target.read_bytes()
        newer_payload = {"token": "newer-external-login"}
        newer_bytes = json.dumps(newer_payload).encode("utf-8")
        original_atomic_copy = account_store._atomic_copy

        def mutate_target_after_replacement(copy_source, destination, **kwargs):
            result = original_atomic_copy(copy_source, destination, **kwargs)
            if Path(copy_source) == stored_path and Path(destination) == target:
                self.write_json(target, newer_payload)
            return result

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_target_after_replacement,
        ):
            with self.assertRaisesRegex(AccountStoreError, "未自动恢复"):
                self.store.switch_profile(profile.profile_id, target)

        self.assertEqual(target.read_bytes(), newer_bytes)
        backups = list(self.store.backup_directory.glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_bytes)

    def test_completed_switch_can_be_rolled_back_for_larger_transaction(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "new"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "old"})
        original = target.read_bytes()

        result = self.store.switch_profile(profile.profile_id, target)
        self.store.rollback_switch(result)

        self.assertEqual(target.read_bytes(), original)

    def test_rollback_rejects_overwriting_a_newer_external_login(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "selected"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "original"})
        result = self.store.switch_profile(profile.profile_id, target)
        newer = self.write_json(self.root / "newer.json", {"token": "external-newer"})
        target.write_bytes(newer.read_bytes())

        with self.assertRaisesRegex(AccountStoreError, "拒绝回滚"):
            self.store.rollback_switch(result)

        self.assertEqual(target.read_bytes(), newer.read_bytes())

    def test_rollback_rejects_a_modified_switch_backup(self) -> None:
        source = self.write_json(self.root / "selected.json", {"token": "selected"})
        profile = self.store.import_profile(source, "工作账号").profile
        target = self.write_json(self.root / ".codex" / "auth.json", {"token": "original"})
        result = self.store.switch_profile(profile.profile_id, target)
        self.assertIsNotNone(result.backup_path)
        self.write_json(result.backup_path, {"token": "modified-backup"})
        selected_bytes = target.read_bytes()

        with self.assertRaisesRegex(AccountStoreError, "认证备份已被修改"):
            self.store.rollback_switch(result)

        self.assertEqual(target.read_bytes(), selected_bytes)

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

    def test_manifest_repair_does_not_create_a_duplicate_profile(self) -> None:
        first_source = self.write_json(self.root / "first.json", {"token": "one"})
        second_source = self.write_json(self.root / "second.json", {"token": "two"})
        first = self.store.import_profile(first_source, "账号一").profile
        second = self.store.import_profile(second_source, "账号二").profile
        self.store.profile_path(first).write_bytes(second_source.read_bytes())
        manifest_before = self.store.manifest_path.read_bytes()

        with self.assertRaisesRegex(AccountStoreError, second.remark):
            self.store.update_profile(first.profile_id, second_source)

        self.assertEqual(self.store.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.store.load_profiles()[0].sha256, first.sha256)

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

    def test_active_profile_uses_managed_identity_after_token_rotation(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )

        self.assertNotEqual(validate_auth_file(current), profile.sha256)
        self.assertEqual(self.store.active_profile_id(current), profile.profile_id)

    def test_active_profile_read_does_not_create_a_missing_codex_directory(self) -> None:
        target = self.root / "missing-home" / ".codex" / "auth.json"

        self.assertIsNone(self.store.active_profile_id(target))

        self.assertFalse(target.parent.exists())

    def test_sync_current_profile_persists_rotated_tokens_with_backup(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        original_bytes = stored_path.read_bytes()
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )

        result = self.store.sync_current_profile(current)

        self.assertEqual(result.status, SYNC_STATUS_UPDATED)
        self.assertTrue(result.changed)
        self.assertEqual(result.profile.profile_id, profile.profile_id)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(result.backup_path.read_bytes(), original_bytes)
        self.assertEqual(stored_path.read_bytes(), current.read_bytes())
        self.assertEqual(self.store.load_profiles()[0].sha256, validate_auth_file(current))

        unchanged = self.store.sync_current_profile(current)
        self.assertEqual(unchanged.status, SYNC_STATUS_UNCHANGED)
        self.assertFalse(unchanged.changed)
        self.assertIsNone(unchanged.backup_path)

    def test_sync_current_profile_leaves_unmatched_and_legacy_auth_unchanged(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        stored_bytes = stored_path.read_bytes()
        other_account = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-b", "2"),
        )

        unmatched = self.store.sync_current_profile(other_account)

        self.assertEqual(unmatched.status, SYNC_STATUS_UNMATCHED)
        self.assertFalse(unmatched.changed)
        self.assertEqual(stored_path.read_bytes(), stored_bytes)

        legacy = self.write_json(self.root / ".codex" / "auth.json", {"token": "legacy"})
        unsupported = self.store.sync_current_profile(legacy)
        self.assertEqual(unsupported.status, SYNC_STATUS_UNSUPPORTED)
        self.assertFalse(unsupported.changed)
        self.assertEqual(stored_path.read_bytes(), stored_bytes)

    def test_sync_current_profile_rejects_ambiguous_managed_identity(self) -> None:
        first_source = self.write_json(
            self.root / "first.json",
            self.managed_auth("account-a", "1"),
        )
        second_source = self.write_json(
            self.root / "second.json",
            self.managed_auth("account-a", "2"),
        )
        first = self.store.import_profile(first_source, "账号 A 旧副本").profile
        legacy_source = self.write_json(self.root / "legacy.json", {"token": "legacy"})
        second = self.store.import_profile(legacy_source, "账号 A 新副本").profile
        second_path = self.store.profile_path(second)
        second_path.write_bytes(second_source.read_bytes())
        manifest = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        for item in manifest["profiles"]:
            if item["id"] == second.profile_id:
                item["sha256"] = validate_auth_file(second_path)
        self.store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = next(
            item for item in self.store.load_profiles() if item.profile_id == second.profile_id
        )
        first_bytes = self.store.profile_path(first).read_bytes()
        second_bytes = self.store.profile_path(second).read_bytes()
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "3"),
        )

        with self.assertRaisesRegex(AccountStoreError, "多个账号副本"):
            self.store.sync_current_profile(current)

        self.assertEqual(self.store.profile_path(first).read_bytes(), first_bytes)
        self.assertEqual(self.store.profile_path(second).read_bytes(), second_bytes)

    def test_sync_current_profile_rejects_modified_matching_copy(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        self.write_json(
            self.store.profile_path(profile),
            self.managed_auth("account-a", "2"),
        )
        modified_bytes = self.store.profile_path(profile).read_bytes()
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "3"),
        )

        with self.assertRaisesRegex(AccountStoreError, "已在外部修改"):
            self.store.sync_current_profile(current)

        self.assertEqual(self.store.profile_path(profile).read_bytes(), modified_bytes)

    def test_update_rejects_different_managed_chatgpt_account(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        stored_bytes = stored_path.read_bytes()
        other_account = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-b", "2"),
        )

        with self.assertRaisesRegex(AccountStoreError, "不是同一 ChatGPT 账号"):
            self.store.update_profile(profile.profile_id, other_account)

        self.assertEqual(stored_path.read_bytes(), stored_bytes)

    def test_update_prevents_creating_duplicate_managed_account(self) -> None:
        managed_source = self.write_json(
            self.root / "managed.json",
            self.managed_auth("account-a", "1"),
        )
        managed = self.store.import_profile(managed_source, "账号 A").profile
        legacy_source = self.write_json(self.root / "legacy.json", {"token": "legacy"})
        legacy = self.store.import_profile(legacy_source, "旧格式账号").profile
        legacy_path = self.store.profile_path(legacy)
        legacy_bytes = legacy_path.read_bytes()
        refreshed = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )

        with self.assertRaisesRegex(AccountStoreError, managed.remark):
            self.store.update_profile(legacy.profile_id, refreshed)

        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)

    def test_update_rejects_crossing_chatgpt_and_api_key_auth_modes(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        stored_bytes = stored_path.read_bytes()
        api_key_auth = self.write_json(
            self.root / ".codex" / "auth.json",
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-api-key-one",
            },
        )

        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(profile.profile_id, api_key_auth)

        self.assertEqual(stored_path.read_bytes(), stored_bytes)

    def test_update_rejects_legacy_chatgpt_and_api_key_modes_in_both_directions(self) -> None:
        legacy_chatgpt = self.write_json(
            self.root / "legacy-chatgpt.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "legacy-access-token"},
            },
        )
        api_key_auth = self.write_json(
            self.root / "api-key.json",
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-api-key-one",
            },
        )
        api_key_without_mode = self.write_json(
            self.root / "api-key-without-mode.json",
            {"OPENAI_API_KEY": "test-api-key-two"},
        )
        legacy_chatgpt_update = self.write_json(
            self.root / "legacy-chatgpt-update.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "new-legacy-access-token"},
            },
        )
        api_key_update = self.write_json(
            self.root / "api-key-update.json",
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-api-key-updated",
            },
        )
        api_key_without_mode_update = self.write_json(
            self.root / "api-key-without-mode-update.json",
            {"OPENAI_API_KEY": "test-api-key-without-mode-updated"},
        )
        legacy_profile = self.store.import_profile(legacy_chatgpt, "旧版 ChatGPT").profile
        api_profile = self.store.import_profile(api_key_auth, "API Key").profile
        unknown_mode_profile = self.store.import_profile(
            api_key_without_mode,
            "旧版 API Key",
        ).profile
        legacy_bytes = self.store.profile_path(legacy_profile).read_bytes()
        api_bytes = self.store.profile_path(api_profile).read_bytes()
        unknown_mode_bytes = self.store.profile_path(unknown_mode_profile).read_bytes()

        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(legacy_profile.profile_id, api_key_update)
        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(api_profile.profile_id, legacy_chatgpt_update)
        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(
                legacy_profile.profile_id,
                api_key_without_mode_update,
            )
        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(
                unknown_mode_profile.profile_id,
                legacy_chatgpt_update,
            )

        self.assertEqual(self.store.profile_path(legacy_profile).read_bytes(), legacy_bytes)
        self.assertEqual(self.store.profile_path(api_profile).read_bytes(), api_bytes)
        self.assertEqual(
            self.store.profile_path(unknown_mode_profile).read_bytes(),
            unknown_mode_bytes,
        )

    def test_update_requires_reimport_when_legacy_chatgpt_identity_is_unavailable(self) -> None:
        legacy_chatgpt = self.write_json(
            self.root / "legacy-chatgpt.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "legacy-access-token"},
            },
        )
        profile = self.store.import_profile(legacy_chatgpt, "旧版 ChatGPT").profile
        stored_bytes = self.store.profile_path(profile).read_bytes()

        unchanged = self.store.update_profile(profile.profile_id, legacy_chatgpt)
        self.assertFalse(unchanged.changed)

        modern_chatgpt = self.write_json(
            self.root / "modern-chatgpt.json",
            self.managed_auth("account-a", "2"),
        )
        with self.assertRaisesRegex(AccountStoreError, "无法确认当前认证"):
            self.store.update_profile(profile.profile_id, modern_chatgpt)

        self.assertEqual(self.store.profile_path(profile).read_bytes(), stored_bytes)

    def test_api_key_profile_keeps_existing_non_refreshing_behavior(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-api-key-one",
            },
        )
        profile = self.store.import_profile(original, "API 账号").profile
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-api-key-two",
            },
        )

        result = self.store.sync_current_profile(current)

        self.assertEqual(result.status, SYNC_STATUS_UNSUPPORTED)
        self.assertFalse(result.changed)
        self.assertNotEqual(self.store.profile_path(profile).read_bytes(), current.read_bytes())

    def test_sync_restores_saved_copy_when_current_changes_during_copy(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        stored_bytes = stored_path.read_bytes()
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )
        original_atomic_copy = account_store._atomic_copy

        def mutate_source_before_profile_copy(source, destination, **kwargs):
            if Path(source) == current and Path(destination) == stored_path:
                self.write_json(current, self.managed_auth("account-a", "3"))
            return original_atomic_copy(source, destination, **kwargs)

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_source_before_profile_copy,
        ):
            with self.assertRaisesRegex(AccountStoreError, "复制过程中发生变化"):
                self.store.sync_current_profile(current)

        self.assertEqual(stored_path.read_bytes(), stored_bytes)

    def test_sync_restores_saved_copy_when_manifest_write_fails(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        stored_bytes = stored_path.read_bytes()
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )

        with mock.patch.object(self.store, "_save_profiles", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(AccountStoreError, "更新账号认证文件失败"):
                self.store.sync_current_profile(current)

        self.assertEqual(stored_path.read_bytes(), stored_bytes)

    def test_sync_compare_and_swap_preserves_newer_profile_generation(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )
        newer = self.write_json(
            self.root / "newer.json",
            self.managed_auth("account-a", "3"),
        )
        newer_bytes = newer.read_bytes()
        original_update = self.store.update_profile

        def update_newer_generation_first(profile_id, source_path, **kwargs):
            original_update(profile_id, newer)
            return original_update(profile_id, source_path, **kwargs)

        with mock.patch.object(
            self.store,
            "update_profile",
            side_effect=update_newer_generation_first,
        ):
            with self.assertRaisesRegex(AccountStoreError, "另一项操作更新"):
                self.store.sync_current_profile(current)

        saved = self.store.load_profiles()[0]
        self.assertEqual(stored_path.read_bytes(), newer_bytes)
        self.assertEqual(saved.sha256, validate_auth_file(newer))
        self.assertNotEqual(saved.sha256, validate_auth_file(current))

    def test_sync_compare_and_swap_detects_change_after_backup(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )
        newer_payload = self.managed_auth("account-a", "3")
        newer_bytes = json.dumps(newer_payload).encode("utf-8")
        original_atomic_copy = account_store._atomic_copy

        def mutate_profile_after_backup(source, destination, **kwargs):
            result = original_atomic_copy(source, destination, **kwargs)
            if (
                Path(source) == stored_path
                and Path(destination).parent == self.store.update_backup_directory
            ):
                self.write_json(stored_path, newer_payload)
            return result

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_profile_after_backup,
        ):
            with self.assertRaisesRegex(AccountStoreError, "备份后发生变化"):
                self.store.sync_current_profile(current)

        self.assertEqual(stored_path.read_bytes(), newer_bytes)
        self.assertEqual(self.store.load_profiles()[0].sha256, profile.sha256)

    def test_manual_update_compare_and_swap_detects_change_after_backup(self) -> None:
        original = self.write_json(self.root / "original.json", {"token": "original"})
        profile = self.store.import_profile(original, "旧格式账号").profile
        stored_path = self.store.profile_path(profile)
        current = self.write_json(self.root / ".codex" / "auth.json", {"token": "current"})
        newer_payload = {"token": "newer-external-copy"}
        newer_bytes = json.dumps(newer_payload).encode("utf-8")
        original_atomic_copy = account_store._atomic_copy

        def mutate_profile_after_backup(source, destination, **kwargs):
            result = original_atomic_copy(source, destination, **kwargs)
            if (
                Path(source) == stored_path
                and Path(destination).parent == self.store.update_backup_directory
            ):
                self.write_json(stored_path, newer_payload)
            return result

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_profile_after_backup,
        ):
            with self.assertRaisesRegex(AccountStoreError, "备份后发生变化"):
                self.store.update_profile(profile.profile_id, current)

        self.assertEqual(stored_path.read_bytes(), newer_bytes)
        self.assertEqual(self.store.load_profiles()[0].sha256, profile.sha256)

    def test_update_failure_does_not_rollback_over_a_post_write_external_change(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )
        external_payload = self.managed_auth("account-a", "3")
        external_bytes = json.dumps(external_payload).encode("utf-8")

        def fail_manifest_after_external_change(_profiles):
            self.write_json(stored_path, external_payload)
            raise OSError("simulated manifest failure")

        with mock.patch.object(
            self.store,
            "_save_profiles",
            side_effect=fail_manifest_after_external_change,
        ):
            with self.assertRaisesRegex(AccountStoreError, "未自动恢复"):
                self.store.sync_current_profile(current)

        self.assertEqual(stored_path.read_bytes(), external_bytes)
        self.assertEqual(self.store.load_profiles()[0].sha256, profile.sha256)

    def test_update_verification_does_not_restore_over_a_newer_external_copy(self) -> None:
        original = self.write_json(
            self.root / "original.json",
            self.managed_auth("account-a", "1"),
        )
        profile = self.store.import_profile(original, "账号 A").profile
        stored_path = self.store.profile_path(profile)
        current = self.write_json(
            self.root / ".codex" / "auth.json",
            self.managed_auth("account-a", "2"),
        )
        external_payload = self.managed_auth("account-a", "3")
        external_bytes = json.dumps(external_payload).encode("utf-8")
        original_atomic_copy = account_store._atomic_copy

        def mutate_profile_after_replacement(source, destination, **kwargs):
            result = original_atomic_copy(source, destination, **kwargs)
            if Path(source) == current and Path(destination) == stored_path:
                self.write_json(stored_path, external_payload)
            return result

        with mock.patch(
            "account_store._atomic_copy",
            side_effect=mutate_profile_after_replacement,
        ):
            with self.assertRaisesRegex(AccountStoreError, "未自动恢复"):
                self.store.sync_current_profile(current)

        self.assertEqual(stored_path.read_bytes(), external_bytes)
        self.assertEqual(self.store.load_profiles()[0].sha256, profile.sha256)

    def test_account_switch_guard_blocks_another_process(self) -> None:
        target = self.root / ".codex" / "auth.json"
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_account_switch_lock,
            args=(str(self.app_directory), str(target), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5.0), "child process did not acquire the switch lock")
            contender = ProfileStore(self.app_directory, lock_timeout=0.2)
            with self.assertRaisesRegex(AccountStoreError, "Codex 操作锁"):
                with contender.account_switch_guard(target):
                    self.fail("a second process acquired an already-held switch lock")
        finally:
            release.set()
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(5.0)

        self.assertEqual(process.exitcode, 0)

    def test_account_switch_guard_does_not_relabel_operation_errors_as_lock_errors(self) -> None:
        target = self.root / ".codex" / "auth.json"

        with self.assertRaisesRegex(OSError, "simulated operation failure"):
            with self.store.account_switch_guard(target):
                raise OSError("simulated operation failure")


if __name__ == "__main__":
    unittest.main()
