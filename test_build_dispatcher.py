import json
import pathlib
import tempfile
import unittest
from unittest import mock

import build_dispatcher
import build_submit
import configure_build_env


SHA = "a" * 40
BUILD_ENV = {
    "REGISTRY_ADDRESS": "registry.example.test",
    "REGISTRY_USER": "builder",
    "REGISTRY_PASSWORD": "p a'ssword",
    "REGISTRY_INSECURE": "false",
    "BUILDKIT_ADDRESS": "tcp://buildkit.example.test:1234",
    "BUILDX_BUILDER": "remote",
    "GIT_DEFAULT_BRANCH": "main",
}


class DispatcherValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "demo.git").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self, payload):
        with mock.patch.object(build_dispatcher, "validate_branch_name", side_effect=lambda value, field: value):
            return build_dispatcher.validate_job(payload, self.root)

    def test_accepts_exact_valid_contract(self):
        job = self.validate({"repo": "demo", "branch": "main", "sha": SHA})
        self.assertEqual("demo", job["repo"])
        self.assertEqual(str((self.root / "demo.git").resolve()), job["repo_path"])

    def test_rejects_traversal_and_unknown_fields(self):
        for payload in (
            {"repo": "../demo", "branch": "main", "sha": SHA},
            {"repo": "demo", "branch": "main", "sha": SHA, "extra": True},
            {"repo": "demo", "branch": "main", "sha": "not-a-sha"},
        ):
            with self.subTest(payload=payload), self.assertRaises(build_dispatcher.JobError):
                self.validate(payload)

    def test_rejects_missing_repository(self):
        with self.assertRaisesRegex(build_dispatcher.JobError, "repository not found"):
            self.validate({"repo": "missing", "branch": "main", "sha": SHA})


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.repos = self.root / "repos"
        self.queue = self.root / "queue"
        (self.repos / "demo.git").mkdir(parents=True)
        self.environ = {
            "GIT_REPOS_ROOT": str(self.repos),
            "GIT_BUILD_QUEUE_ROOT": str(self.queue),
            "GIT_BUILD_QUEUE_SIZE": "2",
            "GIT_BUILD_WORKER": "/worker.py",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_job_is_durable_until_worker_finishes(self):
        build_queue = build_dispatcher.BuildQueue(self.environ)
        with mock.patch.object(build_dispatcher, "validate_branch_name", side_effect=lambda value, field: value):
            job_id = build_queue.submit({"repo": "demo", "branch": "main", "sha": SHA})
            path = self.queue / f"{job_id}.job"
            self.assertTrue(path.is_file())
            with mock.patch.object(build_dispatcher.subprocess, "run") as run:
                build_queue.run_job(path)
        run.assert_called_once()
        self.assertEqual(
            ["/worker.py", str((self.repos / "demo.git").resolve()), "main", SHA, "demo"],
            run.call_args.args[0],
        )
        self.assertEqual(self.environ, run.call_args.kwargs["env"])
        self.assertFalse(path.exists())

    def test_recover_requeues_existing_jobs(self):
        self.queue.mkdir()
        path = self.queue / "build-existing.job"
        path.write_text(json.dumps({"repo": "demo", "branch": "main", "sha": SHA}), encoding="utf-8")
        build_queue = build_dispatcher.BuildQueue(self.environ)
        build_queue.recover()
        self.assertEqual(path, build_queue.jobs.get_nowait())

    def test_capacity_counts_durable_jobs(self):
        self.environ["GIT_BUILD_QUEUE_SIZE"] = "1"
        build_queue = build_dispatcher.BuildQueue(self.environ)
        with mock.patch.object(build_dispatcher, "validate_branch_name", side_effect=lambda value, field: value):
            build_queue.submit({"repo": "demo", "branch": "main", "sha": SHA})
            with self.assertRaisesRegex(build_dispatcher.JobError, "queue is full"):
                build_queue.submit({"repo": "demo", "branch": "main", "sha": SHA})

    def test_corrupt_recovered_job_is_reported_and_removed(self):
        self.queue.mkdir()
        path = self.queue / "build-corrupt.job"
        path.write_text("not json", encoding="utf-8")
        build_queue = build_dispatcher.BuildQueue(self.environ)
        with mock.patch("builtins.print") as output:
            build_queue.run_job(path)
        output.assert_called_once()
        self.assertFalse(path.exists())


class SubmitClientTests(unittest.TestCase):
    def test_success_response(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.recv.return_value = b'{"queued":true,"id":"build-1"}\n'
        with mock.patch.object(build_submit.socket, "AF_UNIX", 1, create=True), \
                mock.patch.object(build_submit.socket, "socket", return_value=client):
            result = build_submit.submit("main", SHA, "demo")
        self.assertEqual("build-1", result["id"])
        sent = json.loads(client.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual({"branch": "main", "sha": SHA, "repo": "demo"}, sent)

    def test_dispatcher_error_is_reported(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.recv.return_value = b'{"queued":false,"error":"queue is full"}\n'
        with mock.patch.object(build_submit.socket, "AF_UNIX", 1, create=True), \
                mock.patch.object(build_submit.socket, "socket", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "queue is full"):
                build_submit.submit("main", SHA, "demo")


class EnvironmentConfigurationTests(unittest.TestCase):
    def test_requires_all_build_values(self):
        for missing in BUILD_ENV:
            environ = dict(BUILD_ENV)
            del environ[missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, missing):
                configure_build_env.render(environ)

    def test_quotes_values_and_omits_unset_optional_values(self):
        content = configure_build_env.render({
            **BUILD_ENV,
            "GIT_BUILD_WORKERS": "2",
            "GIT_REPOSITORY_ENV_ROOT": "/srv/repository env",
        })
        self.assertIn("export REGISTRY_PASSWORD='p a'\"'\"'ssword'\n", content)
        self.assertIn("export GIT_BUILD_WORKERS=2\n", content)
        self.assertIn("export GIT_REPOSITORY_ENV_ROOT='/srv/repository env'\n", content)
        self.assertNotIn("GIT_BUILD_QUEUE_ROOT", content)


if __name__ == "__main__":
    unittest.main()
