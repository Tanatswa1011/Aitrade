"""AITRADE FundedNext MCP OAuth login (authorization code + PKCE S256).

Usage:
    python tools/fundednext_mcp_login.py

Stores a refreshable session in gitignored state/fundednext_mcp_oauth.json.
Never prints tokens. PROP_EXECUTION remains false. No order path.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundednext_mcp_oauth import (  # noqa: E402
    OAuthError,
    PREFERRED_SCOPE,
    login_interactive,
)


def main() -> int:
    print("AITRADE FundedNext MCP login", flush=True)
    print("Opening the FundedNext authorization page in your browser.", flush=True)
    print("This process requests scope %s only." % PREFERRED_SCOPE, flush=True)
    print("Callback is bound to 127.0.0.1 on an ephemeral port.", flush=True)
    try:
        result = login_interactive(timeout_sec=240.0)
    except OAuthError as exc:
        print("LOGIN FAILED", flush=True)
        print(str(exc), flush=True)
        return 1
    except Exception:
        print("LOGIN FAILED", flush=True)
        return 1
    if not result.get("authenticated"):
        print("LOGIN FAILED", flush=True)
        return 1
    print("AUTHENTICATED", flush=True)
    print("scope requested: %s" % PREFERRED_SCOPE, flush=True)
    print("session stored locally (gitignored)", flush=True)
    print("PROP_EXECUTION=false", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
