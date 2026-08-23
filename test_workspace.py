"""Authoritative isolation for mutable test artifacts.

Production code is unchanged unless ``AITRADE_PHASE54_TEST=1``.  In test mode all
mutable paths must live below one process-unique temporary root.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_ROOT: Path | None = None


def test_mode() -> bool:
    return os.environ.get("AITRADE_PHASE54_TEST") == "1"


def test_root() -> Path:
    global _ROOT
    if not test_mode():
        raise RuntimeError("test_workspace_requested_outside_test_mode")
    if _ROOT is None:
        configured = os.environ.get("AITRADE_TEST_ROOT")
        _ROOT = Path(configured).resolve() if configured else Path(
            tempfile.mkdtemp(prefix="aitrade_test_")
        ).resolve()
        _ROOT.mkdir(parents=True, exist_ok=True)
        os.environ["AITRADE_TEST_ROOT"] = str(_ROOT)
    return _ROOT


def mutable_path(*parts: str) -> Path:
    root = test_root()
    path = root.joinpath(*parts).resolve()
    if path != root and root not in path.parents:
        raise RuntimeError(f"test_path_escaped_workspace:{path}")
    return path


def production_or_test(production: Path, *test_parts: str) -> Path:
    return mutable_path(*test_parts) if test_mode() else production


def _cleanup() -> None:
    if _ROOT is not None and not os.environ.get("AITRADE_TEST_ROOT_PRESERVE"):
        shutil.rmtree(_ROOT, ignore_errors=True)


atexit.register(_cleanup)
