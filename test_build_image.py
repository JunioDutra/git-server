import io
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
    def test_accepts_da_school_contract(self):
        config = build_image.parse_build_config("""
default_branch: master
build:
  - name: api
    context: .
    dockerfile: apps/api/Dockerfile
    args:
      NODE_ENV: "production"
  - name: web
    context: .
    dockerfile: apps/web/Dockerfile
    args:
      PUBLIC_API_URL: "https://api.example.test"
  - name: keycloak
    context: .
    dockerfile: infra/keycloak/Dockerfile.production
""")
        self.assertEqual("master", config["default_branch"])
        self.assertEqual(["api", "web", "keycloak"], [
            item["name"] for item in config["builds"]
        ])
        self.assertEqual({}, config["builds"][2]["args"])

    def test_parses_multiple_builds(self):
        config = build_image.parse_build_config("""
default_branch: master
build:
  - name: api
    context: services/api
    dockerfile: Dockerfile
    args:
      APP_ENV: "production"
      PORT: "8080"
  - name: web
    context: services/web
    dockerfile: docker/Dockerfile
mirrors:
  - url: git@example/repo.git
""")
        builds = config["builds"]
        self.assertEqual("master", config["default_branch"])
        self.assertEqual(["api", "web"], [item["name"] for item in builds])
        self.assertEqual("docker/Dockerfile", builds[1]["dockerfile"])
        self.assertEqual({"APP_ENV": "production", "PORT": "8080"}, builds[0]["args"])

    def test_absent_build_is_opt_out(self):
        config = build_image.parse_build_config("mirrors: []\n")
        self.assertEqual([], config["builds"])
        self.assertIsNone(config["default_branch"])

    def test_rejects_legacy_and_invalid_builds(self):
        invalid = (
            "dockerfile: Dockerfile\n",
            "build: []\n",
            "build:\n  - name: API\n    context: .\n    dockerfile: Dockerfile\n",
            "build:\n  - name: api\n    context: ../api\n    dockerfile: Dockerfile\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    target: prod\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n  - name: api\n    context: web\n    dockerfile: Dockerfile\n",
            "unknown: true\n",
            "default_branch: 'bad branch'\n",
            "default_branch: null\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    args: []\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    args: null\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    args:\n      BAD-NAME: value\n",
            "build:\n  - name: api\n    context: .\n    dockerfile: Dockerfile\n    args:\n      PORT: 8080\n",
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
            attempted.append((build["name"], _args[-1]))
            return 1 if build["name"] == "api" else 0

        with mock.patch.object(build_image, "materialize_tree", self.materialize), \
             mock.patch.object(build_image, "registry_login", return_value=0), \
             mock.patch.object(build_image, "ensure_builder", return_value=0), \
             mock.patch.object(build_image, "build_one", side_effect=fake_build):
            result = build_image.execute("repo.git", "main", "a" * 40, "demo", self.env)
        self.assertEqual(1, result)
        self.assertEqual([("api", "main"), ("web", "main")], attempted)

    def test_one_log_per_build_and_no_password_in_log(self):
        work = pathlib.Path(self.tmp.name) / "work"
        context = work / "api"
        context.mkdir(parents=True)
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        with mock.patch.object(build_image, "run_command", side_effect=(0, 0, 0)):
            result = build_image.build_one(
                "demo", "main", "b" * 40,
                {"name": "api", "context": "api", "dockerfile": "Dockerfile",
                 "args": {"APP_ENV": "production"}},
                context, dockerfile, work, os.path.join(self.logs, "demo"), self.env, True,
                "main",
            )
        self.assertEqual(0, result)
        logs = list((pathlib.Path(self.logs) / "demo").glob("*.log"))
        self.assertEqual(1, len(logs))
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("# hook-log: build=api", content)
        self.assertIn("# hook-log: image=registry.local:5000/demo/api", content)
        self.assertIn("# hook-log: default_branch=main", content)
        self.assertIn("# hook-log: build_args=APP_ENV", content)
        self.assertNotIn("production", content)
        self.assertNotIn(VALID_ENV["REGISTRY_PASSWORD"], content)

    def test_build_args_are_executed_but_redacted_and_latest_uses_repo_branch(self):
        work = pathlib.Path(self.tmp.name) / "work-redaction"
        context = work / "api"
        context.mkdir(parents=True)
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        build = {
            "name": "api", "context": "api", "dockerfile": "Dockerfile",
            "args": {"APP_ENV": "production", "PORT": "8080"},
        }
        with mock.patch.object(build_image, "run_command", return_value=0) as run:
            result = build_image.build_one(
                "demo", "master", "d" * 40, build, context, dockerfile, work,
                os.path.join(self.logs, "demo"), self.env, False, "master",
            )
        self.assertEqual(0, result)
        self.assertEqual(3, run.call_count)
        build_call = run.call_args_list[0]
        command = build_call.args[0]
        display = build_call.kwargs["display_command"]
        self.assertIn("APP_ENV=production", command)
        self.assertIn("PORT=8080", command)
        self.assertIn("APP_ENV=<redacted>", display)
        self.assertIn("PORT=<redacted>", display)
        self.assertNotIn("APP_ENV=production", display)
        self.assertTrue(run.call_args_list[-1].args[0][-1].endswith(":latest"))

        with mock.patch.object(build_image, "run_command", return_value=0) as run:
            build_image.build_one(
                "demo", "feature", "e" * 40, build, context, dockerfile, work,
                os.path.join(self.logs, "demo"), self.env, False, "master",
            )
        self.assertEqual(2, run.call_count)
        self.assertFalse(any(call.args[0][-1].endswith(":latest") for call in run.call_args_list))

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
        display = run.call_args.kwargs["display_command"]
        self.assertIn("--insecure", command)
        self.assertNotIn(self.env["REGISTRY_PASSWORD"], command)
        self.assertNotIn(self.env["REGISTRY_USER"], display)
        self.assertEqual(self.env["REGISTRY_PASSWORD"], run.call_args.kwargs["stdin_text"])

    def test_run_command_never_records_registry_credentials(self):
        log = mock.Mock()
        log.file = io.StringIO()
        completed = mock.Mock(returncode=0)
        with mock.patch.object(build_image.subprocess, "run", return_value=completed):
            result = build_image.run_command(
                ["tool", "--username", self.env["REGISTRY_USER"]],
                log,
                env=self.env,
                stdin_text=self.env["REGISTRY_PASSWORD"],
            )
        self.assertEqual(0, result)
        content = log.file.getvalue()
        self.assertNotIn(self.env["REGISTRY_USER"], content)
        self.assertNotIn(self.env["REGISTRY_PASSWORD"], content)
        self.assertIn("<redacted>", content)


if __name__ == "__main__":
    unittest.main()
