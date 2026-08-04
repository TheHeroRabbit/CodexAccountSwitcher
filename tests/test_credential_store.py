import sys
import unittest

from credential_store import (
    InvalidApiKeyError,
    WindowsDataProtector,
    masked_api_key,
    normalize_api_key,
)


class CredentialHelpersTests(unittest.TestCase):
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
