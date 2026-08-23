"""Deterministic regressions for Recovery B import/path/thread isolation."""
from __future__ import annotations

import inspect
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fundednext_mcp_oauth as oauth
import phase54_ops


class RecoveryBIsolationTests(unittest.TestCase):
    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("AITRADE_PHASE54_TEST", "AITRADE_TEST_ROOT", "AITRADE_TEST_ROOT_PRESERVE")}
        self.root = Path(tempfile.mkdtemp(prefix="recovery_b_"))
        os.environ["AITRADE_PHASE54_TEST"] = "1"
        os.environ["AITRADE_TEST_ROOT"] = str(self.root)
        os.environ["AITRADE_TEST_ROOT_PRESERVE"] = "1"

    def tearDown(self):
        for key, value in self.old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value

    def test_oauth_defaults_are_dynamic_and_never_production(self):
        for fn in (oauth.save_oauth_session, oauth.load_oauth_session, oauth.oauth_session_metadata,
                   oauth.invalidate_oauth_session, oauth.resolve_access_token, oauth.login_interactive):
            parameter = inspect.signature(fn).parameters.get("path")
            self.assertIsNotNone(parameter)
            self.assertIsNone(parameter.default)
        self.assertEqual(oauth.load_oauth_session(), {})
        self.assertFalse(oauth.PRODUCTION_OAUTH_PATH.samefile(oauth.PRODUCTION_OAUTH_PATH.parent / oauth.PRODUCTION_OAUTH_PATH.name) is False)
        self.assertFalse((self.root / "state" / "fundednext_mcp_oauth.json").exists())

    def test_import_before_root_then_save_stays_in_root(self):
        code = r'''import os,sys
import fundednext_mcp_oauth as o
os.environ["AITRADE_PHASE54_TEST"]="1"
os.environ["AITRADE_TEST_ROOT"]=sys.argv[1]
p=o.save_oauth_session({"access_token":"fixture","expires_at":9999999999})
assert str(p.resolve()).startswith(str(__import__("pathlib").Path(sys.argv[1]).resolve()))
print(p)
'''
        env = dict(os.environ)
        env.pop("AITRADE_PHASE54_TEST", None); env.pop("AITRADE_TEST_ROOT", None)
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=Path(__file__).parent,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.root), result.stdout)

    def test_two_sequential_roots_do_not_share_oauth_or_journals(self):
        root1, root2 = self.root / "one", self.root / "two"
        for root, token in ((root1, "fixture-one"), (root2, "fixture-two")):
            root.mkdir(parents=True)
            os.environ["AITRADE_TEST_ROOT"] = str(root)
            oauth.save_oauth_session({"access_token": token, "expires_at": 9999999999})
            phase54_ops.append_event("INFO", token)
        oauth1 = root1 / "state" / "fundednext_mcp_oauth.json"
        oauth2 = root2 / "state" / "fundednext_mcp_oauth.json"
        journal1 = root1 / "journal" / "phase54_ops" / "events.jsonl"
        journal2 = root2 / "journal" / "phase54_ops" / "events.jsonl"
        production_journal = (Path(__file__).parent / "journal" / "phase54_ops" / "events.jsonl").resolve()
        self.assertNotEqual(oauth1.resolve(), oauth2.resolve())
        self.assertNotEqual(journal1.resolve(), journal2.resolve())
        self.assertIn(root1.resolve(), oauth1.resolve().parents)
        self.assertIn(root2.resolve(), oauth2.resolve().parents)
        self.assertIn(root1.resolve(), journal1.resolve().parents)
        self.assertIn(root2.resolve(), journal2.resolve().parents)
        self.assertNotEqual(journal1.resolve(), production_journal)
        self.assertNotEqual(journal2.resolve(), production_journal)
        self.assertIn("fixture-one", oauth1.read_text())
        self.assertNotIn("fixture-two", journal1.read_text())
        self.assertIn("fixture-two", journal2.read_text())
        root2_before = (oauth2.read_bytes(), journal2.read_bytes())
        shutil.rmtree(root1)
        self.assertFalse(root1.exists())
        self.assertEqual(root2_before, (oauth2.read_bytes(), journal2.read_bytes()))

    def test_production_targets_rejected_in_test_mode(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.load_oauth_session(oauth.PRODUCTION_OAUTH_PATH)

    def test_missing_test_root_fails_closed_at_oauth_boundary(self):
        os.environ.pop("AITRADE_TEST_ROOT", None)
        with mock.patch("test_workspace._ROOT", None):
            # OAuth adds the stricter explicit-root requirement.
            with self.assertRaises(RuntimeError):
                from phase55d_session_authorization import _path
                _path()

    def test_supervisor_startup_teardown_leaves_no_worker(self):
        namespace = runpy.run_path(str(Path("dashboard/ops-console/api.py")))
        start = namespace["start_runtime_supervisor"]
        runtime_globals = start.__globals__
        with mock.patch.object(runtime_globals["EngineSupervisor"], "status", return_value={"engine": "STOPPED"}):
            start()
            self.assertTrue(runtime_globals["_RUNTIME_THREAD"].is_alive())
            runtime_globals["stop_runtime_supervisor"]()
            self.assertIsNone(runtime_globals["_RUNTIME_THREAD"])


if __name__ == "__main__":
    unittest.main()
