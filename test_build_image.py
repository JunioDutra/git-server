import os
import pathlib
import tempfile
import unittest
from unittest import mock

import build_image


VALID_ENV = {
    "REGISTRY_ADDRESS": "registry.local:5000",
    "REGISTRY_USER": "builder",
    "REGISTRY_PASSWORD": "secret-value",
    "REGISTRY_INSECURE": "true",
    "BUILDKIT_ADDRESS": "tcp://buildkit.local:1234",
    "BUILDX_BUILDER": "remote",
    "GIT_DEFAULT_BRANCH": "main",
}


class BuildConfigTests(unittest.TestCase):
    def test_parses_multiple_builds(self):
        builds = build_image.parse_build_config("""
build:
  - name: api
    context: services/api
    dockerfile: Dockerfile
  - name: web
    context: services/web
    dockerfile: docker/Dockerfile
mirrors:
  - url: git@example/repo.git
""")
        self.assertEqual(["api", "web"], [item["name"] for item in builds])
        self.assertEqual("docker/Dockerfile", builds[1]["dockerfile"])

    def test_absent_build_is_opt_out(self):
        self.assertEqual([], build_image.parse_build_config("mirrors: []\n"))

    def test_rejects_legacy_and_invalid_builds(self):
        invalid = (
            "dockerfile: Dockerfile\n",
            "build: []\n",
            "build:\n  - name: API\n    context: .\n    dockerfile: Dockerfile\n",
            "build:\n  - name: api\n    context: ../api\n    dockerfile: Dockerfile\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    target: prod\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n  - name: api\n    context: web\n    dockerfile: Dockerfile\n",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(build_image.ConfigError):
                build_image.parse_build_config(raw)


class BuildExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        self.env = {**VALID_ENV, "GIT_HOOK_LOGS_ROOT": self.logs}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def materialize(_repo, _sha, work):
        root = pathlib.Path(work)
        for name in ("api", "web"):
            context = root / name
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (root / "repository.yaml").write_text("""
build:
  - name: api
    context: api
    dockerfile: Dockerfile
  - name: web
    context: web
    dockerfile: Dockerfile
""", encoding="utf-8")

    def test_executes_every_item_and_aggregates_failure(self):
        attempted = []

        def fake_build(_repo, _branch, _sha, build, *_args):
            attempted.append(build["name"])
            return 1 if build["name"] == "api" else 0

        with mock.patch.object(build_image, "materialize_tree", self.materialize), \
             mock.patch.object(build_image, "registry_login", return_value=0), \
             mock.patch.object(build_image, "ensure_builder", return_value=0), \
             mock.patch.object(build_image, "build_one", side_effect=fake_build):
            result = build_image.execute("repo.git", "main", "a" * 40, "demo", self.env)
        self.assertEqual(1, result)
        self.assertEqual(["api", "web"], attempted)

    def test_one_log_per_build_and_no_password_in_log(self):
        work = pathlib.Path(self.tmp.name) / "work"
        context = work / "api"
        context.mkdir(parents=True)
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        with mock.patch.object(build_image, "run_command", side_effect=(0, 0, 0)):
            result = build_image.build_one(
                "demo", "main", "b" * 40,
                {"name": "api", "context": "api", "dockerfile": "Dockerfile"},
                context, dockerfile, work, os.path.join(self.logs, "demo"), self.env, True,
            )
        self.assertEqual(0, result)
        logs = list((pathlib.Path(self.logs) / "demo").glob("*.log"))
        self.assertEqual(1, len(logs))
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("# hook-log: build=api", content)
        self.assertIn("# hook-log: image=registry.local:5000/demo/api", content)
        self.assertNotIn(VALID_ENV["REGISTRY_PASSWORD"], content)

    def test_missing_environment_creates_diagnostic_log(self):
        with mock.patch.object(build_image, "materialize_tree", self.materialize):
            result = build_image.execute("repo.git", "main", "c" * 40, "demo", {
                "GIT_HOOK_LOGS_ROOT": self.logs,
            })
        self.assertEqual(1, result)
        logs = list((pathlib.Path(self.logs) / "demo").glob("*.log"))
        self.assertEqual(1, len(logs))
        self.assertIn("missing required environment variables", logs[0].read_text(encoding="utf-8"))

    def test_registry_login_uses_password_stdin_and_insecure_flag(self):
        log = mock.Mock()
        with mock.patch.object(build_image, "run_command", return_value=0) as run:
            result = build_image.registry_login(self.env, True, log)
        self.assertEqual(0, result)
        command = run.call_args.args[0]
        self.assertIn("--insecure", command)
        self.assertNotIn(self.env["REGISTRY_PASSWORD"], command)
        self.assertEqual(self.env["REGISTRY_PASSWORD"], run.call_args.kwargs["stdin_text"])


if __name__ == "__main__":
    unittest.main()
