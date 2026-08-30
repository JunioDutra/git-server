import json
import os
import pathlib
import tempfile
import threading
import unittest

import repository_variables


class VariableStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "variables"
        self.store = repository_variables.VariableStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_patch_preserves_siblings_and_supports_multiline_empty_and_placeholder(self):
        private_key = "-----BEGIN KEY-----\nsecret\n-----END KEY-----\n"
        self.store.patch("demo", {"SSH_DEPLOY_KEY": private_key, "EMPTY": ""}, [])
        self.store.patch("demo", {"PLACEHOLDER": "***"}, [])
        values = self.store.load("demo")
        self.assertEqual(private_key, values["SSH_DEPLOY_KEY"])
        self.assertEqual("", values["EMPTY"])
        self.assertEqual("***", values["PLACEHOLDER"])
        self.assertEqual(
            ["EMPTY", "PLACEHOLDER", "SSH_DEPLOY_KEY"],
            [item["name"] for item in self.store.list_configured("demo")],
        )

    def test_explicit_delete_and_repository_delete_remove_value_file(self):
        self.store.patch("demo", {"TOKEN": "secret", "KEEP": "value"}, [])
        self.store.patch("demo", {}, ["TOKEN"])
        self.assertEqual({"KEEP": "value"}, self.store.load("demo"))
        self.store.delete_repository("demo")
        self.assertFalse((self.root / "demo.json").exists())
        self.assertEqual({}, self.store.load("demo"))

    def test_rejects_reserved_invalid_conflicting_and_oversized_values(self):
        invalid_patches = (
            ({"lower": "value"}, []),
            ({"GIT_SERVER_SHA": "value"}, []),
            ({"PATH": "value"}, []),
            ({"TOKEN": "bad\x00value"}, []),
            ({"TOKEN": "value"}, ["TOKEN"]),
            ({"TOKEN": "x" * (repository_variables.MAX_VALUE_BYTES + 1)}, []),
        )
        for upsert, delete in invalid_patches:
            with self.subTest(upsert=tuple(upsert), delete=delete), \
                    self.assertRaises(repository_variables.VariableStoreError):
                self.store.patch("demo", upsert, delete)

    def test_malformed_storage_is_not_returned(self):
        self.root.mkdir(mode=0o700)
        (self.root / "demo.json").write_text('{"TOKEN":', encoding="utf-8")
        with self.assertRaises(repository_variables.VariableStorageError):
            self.store.load("demo")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes and flock")
    def test_file_modes_and_concurrent_updates(self):
        errors = []

        def update(index):
            try:
                self.store.patch("demo", {f"VALUE_{index}": str(index)}, [])
            except Exception as exc:  # pragma: no cover - reported by assertion
                errors.append(exc)

        threads = [threading.Thread(target=update, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(16, len(self.store.load("demo")))
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.root / "demo.json").stat().st_mode & 0o777)
        self.assertIsInstance(json.loads((self.root / "demo.json").read_text()), dict)


if __name__ == "__main__":
    unittest.main()
