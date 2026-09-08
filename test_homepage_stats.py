import datetime as dt
import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import homepage_stats


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.repos = self.root / "repos"
        self.logs = self.root / "logs"
        self.hooks = self.root / "hooks"
        (self.repos / "demo.git" / "objects").mkdir(parents=True)
        (self.repos / "demo.git" / "objects" / "loose").write_bytes(b"1234")
        self.now = dt.datetime(2026, 9, 8, 16, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, name, kind, ended_at, exit_code):
        directory = self.logs / "demo"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(
            "\n".join(
                [
                    f"# hook-log: type={kind}",
                    "# hook-log: --- output ---",
                    f"# hook-log: ended_at={ended_at}",
                    f"# hook-log: exit={exit_code}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_storage_counts_only_regular_repository_files(self):
        collector = homepage_stats.Collector(
            self.repos, self.logs, self.hooks, proc_root=self.root / "proc",
            pidfile=self.root / "missing.pid",
        )
        with mock.patch.object(collector, "failed_builds", return_value=0):
            result = collector.collect()
        self.assertEqual(1, result["repositories"])
        self.assertEqual(4, result["storageBytes"])
        self.assertIsNone(result["buildsRunning"])

    def test_counts_recent_failed_build_and_returns_null_without_history(self):
        collector = homepage_stats.Collector(self.repos, self.logs, self.hooks)
        failed = self.write_log("recent.log", "build", "2026-09-08T15:00:00Z", 1)
        os.utime(failed, (self.now.timestamp(), self.now.timestamp()))
        self.assertEqual(1, collector.failed_builds(self.now, lambda: None))

        for path in (self.logs / "demo").glob("*.log"):
            path.unlink()
        self.assertIsNone(collector.failed_builds(self.now, lambda: None))

    def test_endpoint_requires_allowed_client_and_hashed_token(self):
        config = self.root / "homepage.json"
        token = "test-token"
        config.write_text(
            json.dumps({
                "allowedClients": ["127.0.0.1"],
                "tokenSha256": hashlib.sha256(token.encode()).hexdigest(),
            }),
            encoding="utf-8",
        )
        endpoint = homepage_stats.StatsEndpoint()
        expected = {
            "schemaVersion": 1,
            "status": "ok",
            "generatedAt": "2026-09-08T16:00:00Z",
            "repositories": 1,
            "storageBytes": 4,
            "buildsRunning": 0,
            "buildsFailed24h": 0,
        }
        with mock.patch.object(
            homepage_stats.Collector, "collect", return_value=expected
        ):
            status, payload = endpoint.response(
                "127.0.0.1", token, self.repos, self.logs, self.hooks, config
            )
            self.assertEqual(200, status)
            self.assertEqual(expected, payload)
            self.assertEqual(
                401,
                endpoint.response(
                    "127.0.0.1", "wrong", self.repos, self.logs, self.hooks, config
                )[0],
            )
            self.assertEqual(
                403,
                endpoint.response(
                    "192.0.2.10", token, self.repos, self.logs, self.hooks, config
                )[0],
            )


if __name__ == "__main__":
    unittest.main()
