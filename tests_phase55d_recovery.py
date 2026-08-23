"""Task 1A regressions: tests cannot mutate protected production paths."""
from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

os.environ["AITRADE_PHASE54_TEST"] = "1"

import fundednext_mcp_oauth
import gc_vwap_freeze
import nq_dvp_freeze
import nq_dvp_live_runner
import phase53_engine
import phase54_ops
from phase34_validate import assert_frozen
from test_workspace import test_root

ROOT = Path(__file__).resolve().parent
PROTECTED = (
    ROOT / "strategy_frozen",
    ROOT / "journal" / "phase31_nq_dvp_sim",
    ROOT / "journal" / "phase53_fn_flex_shadow",
    ROOT / "journal" / "phase54_ops",
    ROOT / "state" / "phase54_ops.json",
)


def _tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()
    if path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            h.update(str(item.relative_to(path)).replace("\\", "/").encode())
            h.update(item.read_bytes())
    return h.hexdigest()


class TestIsolationRecoveryTests(unittest.TestCase):
    def test_canonical_hashes_pass_and_freeze_writers_are_read_only(self):
        before = _tree_hash(ROOT / "strategy_frozen")
        self.assertTrue(assert_frozen()["ok"])
        self.assertTrue(gc_vwap_freeze.write_frozen_files()["test_write_rejected"])
        self.assertTrue(nq_dvp_freeze.write_frozen_files()["test_write_rejected"])
        self.assertEqual(_tree_hash(ROOT / "strategy_frozen"), before)
        self.assertTrue(assert_frozen()["ok"])

    def test_mutable_paths_share_unique_test_root(self):
        root = test_root()
        paths = (
            nq_dvp_live_runner.JOURNAL_DIR,
            phase53_engine.JOURNAL_DIR,
            phase54_ops._runtime_mutable_path(
                phase54_ops.EVENTS_LOG, relative=("journal", "phase54_ops", "events.jsonl")
            ),
            phase54_ops._runtime_mutable_path(
                phase54_ops.STATE_PATH, relative=("state", "phase54_ops.json")
            ),
            phase54_ops._runtime_mutable_path(
                phase54_ops.AUDIT_PATH_LIVE,
                relative=("journal", "phase53_fn_flex_shadow", "audit.jsonl"),
            ),
            fundednext_mcp_oauth._resolve_oauth_path(),
        )
        for path in paths:
            resolved = path.resolve()
            self.assertTrue(resolved == root or root in resolved.parents, resolved)

    def test_real_oauth_file_is_never_opened(self):
        real = (ROOT / "state" / "fundednext_mcp_oauth.json").resolve()
        self.assertNotEqual(fundednext_mcp_oauth.OAUTH_PATH.resolve(), real)
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() == real:
                raise AssertionError("real_oauth_open_attempt")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            self.assertEqual(fundednext_mcp_oauth.load_oauth_session(), {})
            self.assertFalse(fundednext_mcp_oauth.oauth_session_metadata().get("authenticated", False))

    def test_oauth_acl_uses_process_sid_and_production_target_is_rejected(self):
        from fundednext_mcp_oauth import OAuthError, PRODUCTION_OAUTH_PATH, save_oauth_session
        path = test_root() / "oauth_fixtures" / uuid4().hex / "session.json"
        save_oauth_session({"access_token": "synthetic", "scope": "mcp:read"}, path)
        self.assertTrue(path.read_bytes())
        if os.name == "nt":
            self.assertTrue(fundednext_mcp_oauth._current_windows_sid().startswith("S-"))
        with self.assertRaises(OAuthError):
            save_oauth_session({"access_token": "synthetic"}, PRODUCTION_OAUTH_PATH)

    def test_production_paths_unchanged_by_isolated_mutations(self):
        before = {str(path): _tree_hash(path) for path in PROTECTED}
        nq_dvp_live_runner.ensure_dirs()
        phase53_engine.AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        phase53_engine.AUDIT_PATH.write_text("synthetic-test-only\n", encoding="utf-8")
        state_path = phase54_ops._runtime_mutable_path(
            phase54_ops.STATE_PATH,
            relative=("state", "phase54_ops.json"),
            create_parent=True,
        )
        phase54_ops._write_json(state_path, {})
        self.assertIn(test_root(), state_path.resolve().parents)
        self.assertNotEqual(state_path.resolve(), (ROOT / "state" / "phase54_ops.json").resolve())
        self.assertEqual({str(path): _tree_hash(path) for path in PROTECTED}, before)


if __name__ == "__main__":
    unittest.main()
