import os
import tempfile
import time
import unittest
from unittest import mock

import app


class HookLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repos = os.path.join(self.tmp.name, "repos")
        self.logs = os.path.join(self.tmp.name, "hooks")
        self.legacy = os.path.join(self.tmp.name, "legacy")
        os.makedirs(os.path.join(self.repos, "demo.git"))
        self.values = app.REPOS_ROOT, app.HOOK_LOGS_ROOT, app.VARIABLE_STORE
        app.REPOS_ROOT, app.HOOK_LOGS_ROOT = self.repos, self.logs
        app.VARIABLE_STORE = app.VariableStore(os.path.join(self.tmp.name, "variables"))
        self.legacy_paths = lambda name: {
            "legacy-build.log": os.path.join(self.legacy, f"{name}-build.log"),
            "legacy-mirror.log": os.path.join(self.legacy, f"{name}-mirror.log"),
        }
        self.patch = mock.patch.object(app, "legacy_hook_log_paths", self.legacy_paths)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        app.REPOS_ROOT, app.HOOK_LOGS_ROOT, app.VARIABLE_STORE = self.values
        self.tmp.cleanup()

    def write_log(self, log_id, exit_code=0, content="output", build=None, image=None):
        directory = app.hook_log_dir("demo")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, log_id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# hook-log: type=build\n# hook-log: started_at=2026-08-21T12:00:00Z\n")
            fh.write("# hook-log: branch=main\n# hook-log: --- output ---\n")
            if build:
                fh.write(f"# hook-log: build={build}\n")
            if image:
                fh.write(f"# hook-log: image={image}\n")
            fh.write(content)
            fh.write(f"\n# hook-log: exit={exit_code}\n")
        return path

    def test_lists_metadata_status_and_legacy_logs(self):
        self.write_log("20260821T120000Z-build-aaaaaa.log", 0)
        self.write_log("20260821T120001Z-build-bbbbbb.log", 1)
        os.makedirs(self.legacy, exist_ok=True)
        with open(self.legacy_paths("demo")["legacy-build.log"], "w", encoding="utf-8") as fh:
            fh.write("old build")
        records = app.list_hook_logs("demo")
        self.assertEqual(3, len(records))
        self.assertEqual({"ok", "failed", "legacy"}, {record["status"] for record in records})

    def test_page_separates_current_and_legacy_and_shows_build_image(self):
        self.write_log("20260821T120000Z-build-aaaaaa.log", build="api",
                       image="registry.local/demo/api")
        os.makedirs(self.legacy, exist_ok=True)
        with open(self.legacy_paths("demo")["legacy-build.log"], "w", encoding="utf-8") as fh:
            fh.write("old build")
        output = app.hook_logs_page("demo")
        self.assertIn("Hook executions", output)
        self.assertIn("Legacy aggregate logs", output)
        self.assertIn("registry.local/demo/api", output)

    def test_rejects_traversal_and_deletes_a_single_log(self):
        log_id = "20260821T120000Z-build-aaaaaa.log"
        self.write_log(log_id)
        self.assertEqual((None, False), app.hook_log_path("demo", "../secret.log"))
        self.assertEqual((None, False), app.hook_log_path("demo", "not-a-log.txt"))
        self.assertTrue(app.delete_hook_logs("demo", log_id)[0])
        self.assertFalse(os.path.exists(os.path.join(app.hook_log_dir("demo"), log_id)))

    def test_tail_uses_last_mebibyte_and_html_is_escaped(self):
        log_id = "20260821T120000Z-build-aaaaaa.log"
        path = self.write_log(log_id, content="x" * (1024 * 1024) + "<script>alert(1)</script>")
        content, truncated = app.read_log_tail(path)
        self.assertTrue(truncated)
        self.assertIn("x", content)
        self.assertEqual("ok", app.list_hook_logs("demo")[0]["status"])
        page = app.hook_log_detail_page("demo", log_id)
        self.assertIn("&lt;script&gt;", page)

    def test_retention_and_repository_deletion_remove_logs(self):
        old = self.write_log("20260801T120000Z-build-aaaaaa.log")
        old_time = time.time() - 31 * 86400
        os.utime(old, (old_time, old_time))
        app.cleanup_hook_logs("demo")
        self.assertFalse(os.path.exists(old))
        self.write_log("20260821T120000Z-build-bbbbbb.log")
        os.makedirs(self.legacy, exist_ok=True)
        with open(self.legacy_paths("demo")["legacy-mirror.log"], "w", encoding="utf-8") as fh:
            fh.write("legacy")
        self.assertTrue(app.delete_bare_repo("demo")[0])
        self.assertFalse(os.path.exists(app.hook_log_dir("demo")))
        self.assertFalse(os.path.exists(self.legacy_paths("demo")["legacy-mirror.log"]))


if __name__ == "__main__":
    unittest.main()
