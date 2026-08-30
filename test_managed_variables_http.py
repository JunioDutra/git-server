import http.client
import json
import os
import tempfile
import threading
import unittest

import app


class ManagedVariableHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repos = os.path.join(self.temporary.name, "repos")
        os.makedirs(os.path.join(self.repos, "demo.git"))
        self.originals = app.REPOS_ROOT, app.VARIABLE_STORE
        app.REPOS_ROOT = self.repos
        app.VARIABLE_STORE = app.VariableStore(os.path.join(self.temporary.name, "variables"))
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.REPOS_ROOT, app.VARIABLE_STORE = self.originals
        self.temporary.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address)
        body = None if payload is None else json.dumps(payload)
        headers = {} if payload is None else {
            "Content-Type": "application/json",
            "X-GitServer-CSRF": "1",
        }
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        return response.status, raw

    def test_patch_and_read_models_never_return_values(self):
        secret = "multiline-secret\nsecond-line"
        status, raw = self.request("PATCH", "/api/repo/demo/variables", {
            "upsert": {"SSH_DEPLOY_KEY": secret, "EMPTY": ""}, "delete": [],
        })
        self.assertEqual(200, status)
        self.assertNotIn(secret, raw)
        self.assertEqual(
            {"variables": [
                {"name": "EMPTY", "configured": True},
                {"name": "SSH_DEPLOY_KEY", "configured": True},
            ]},
            json.loads(raw),
        )

        status, raw = self.request("GET", "/api/repo/demo/variables")
        self.assertEqual(200, status)
        self.assertNotIn(secret, raw)
        status, page = self.request("GET", "/repo/demo/variables")
        self.assertEqual(200, status)
        self.assertNotIn(secret, page)
        self.assertIn('placeholder="***"', page)
        self.assertIn("'X-GitServer-CSRF': '1'", page)

    def test_patch_preserves_untouched_values_and_deletes_explicitly(self):
        app.VARIABLE_STORE.patch("demo", {"KEEP": "first", "REMOVE": "second"}, [])
        status, raw = self.request("PATCH", "/api/repo/demo/variables", {
            "upsert": {"NEW": "***"}, "delete": ["REMOVE"],
        })
        self.assertEqual(200, status, raw)
        self.assertEqual({"KEEP": "first", "NEW": "***"}, app.VARIABLE_STORE.load("demo"))

    def test_invalid_payload_does_not_echo_supplied_value(self):
        secret = "must-not-be-echoed"
        status, raw = self.request("PATCH", "/api/repo/demo/variables", {
            "upsert": {"lowercase": secret}, "delete": [],
        })
        self.assertEqual(400, status)
        self.assertNotIn(secret, raw)

    def test_existing_browser_mutations_send_csrf_header(self):
        index = app.index_page()
        repo_page = app.repo_page("demo", os.path.join(self.repos, "demo.git"), "", "", [])
        self.assertIn("X-GitServer-CSRF", index)
        self.assertIn("X-GitServer-CSRF", repo_page)
        self.assertIn("X-GitServer-CSRF", app.JS_DELETE_HOOK_LOGS)


if __name__ == "__main__":
    unittest.main()
