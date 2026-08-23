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
_ROOTS: set[Path] = set()


def test_mode() -> bool:
    return os.environ.get("AITRADE_PHASE54_TEST") == "1"


def test_root() -> Path:
    global _ROOT
    if not test_mode():
        raise RuntimeError("test_workspace_requested_outside_test_mode")
    configured = os.environ.get("AITRADE_TEST_ROOT")
    configured_root = Path(configured).resolve() if configured else None
    if _ROOT is None or (configured_root is not None and configured_root != _ROOT):
        _ROOT = configured_root or Path(tempfile.mkdtemp(prefix="aitrade_test_")).resolve()
        _ROOT.mkdir(parents=True, exist_ok=True)
        _ROOTS.add(_ROOT)
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
    if not os.environ.get("AITRADE_TEST_ROOT_PRESERVE"):
        for root in tuple(_ROOTS):
            shutil.rmtree(root, ignore_errors=True)


atexit.register(_cleanup)
