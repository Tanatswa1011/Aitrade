"""Phase 54E.1 — FundedNext MCP OAuth (authorization code + PKCE S256).

Uses live metadata from https://mcp.fundednext.com. Never logs or prints tokens.
No order/execution helpers exist in this module.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
OAUTH_PATH = ROOT / "state" / "fundednext_mcp_oauth.json"
PROP_EXECUTION = False

PROTECTED_RESOURCE_URL = "https://mcp.fundednext.com/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_METADATA_URL = "https://mcp.fundednext.com/.well-known/oauth-authorization-server"
RESOURCE = "https://mcp.fundednext.com"
PREFERRED_SCOPE = "mcp:read"
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AITRADE/54E.1"
)

# Live metadata snapshot (re-fetched at login; tests may inject).
DEFAULT_METADATA = {
    "issuer": "https://mcp.fundednext.com",
    "authorization_endpoint": "https://mcp.fundednext.com/oauth/authorize",
    "token_endpoint": "https://mcp.fundednext.com/oauth/token",
    "revocation_endpoint": "https://mcp.fundednext.com/oauth/revoke",
    "registration_endpoint": "https://mcp.fundednext.com/oauth/register",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
    "scopes_supported": ["mcp:read", "mcp:write"],
}

_AUTH_GEN = 0


class OAuthError(RuntimeError):
    """Safe OAuth failure. Message must never contain tokens."""


def auth_generation() -> int:
    return _AUTH_GEN


def bump_auth_generation() -> int:
    global _AUTH_GEN
    _AUTH_GEN += 1
    return _AUTH_GEN


def generate_pkce_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def pkce_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def _iso(ts: Optional[float] = None) -> str:
    value = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return value.isoformat()


def parse_expires_at(raw: Any) -> float:
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fetch_oauth_metadata(http_get_json: Optional[Callable[[str], dict[str, Any]]] = None) -> dict[str, Any]:
    getter = http_get_json or _http_get_json
    meta = dict(DEFAULT_METADATA)
    try:
        as_meta = getter(AUTHORIZATION_SERVER_METADATA_URL)
        if isinstance(as_meta, dict):
            meta.update({k: v for k, v in as_meta.items() if v is not None})
    except Exception:
        pass
    try:
        pr = getter(PROTECTED_RESOURCE_URL)
        if isinstance(pr, dict) and pr.get("resource"):
            meta["resource"] = pr["resource"]
            meta["scopes_supported"] = pr.get("scopes_supported") or meta.get("scopes_supported")
    except Exception:
        pass
    meta.setdefault("resource", RESOURCE)
    methods = meta.get("code_challenge_methods_supported") or []
    if "S256" not in methods:
        raise OAuthError("pkce_s256_not_supported")
    grants = meta.get("grant_types_supported") or []
    if "authorization_code" not in grants:
        raise OAuthError("authorization_code_not_supported")
    return meta


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": HTTP_USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_json(url: str, payload: dict[str, Any], headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": HTTP_USER_AGENT,
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            raw = ""
        detail = ""
        try:
            parsed = json.loads(raw) if raw else {}
            detail = str(parsed.get("error") or parsed.get("error_description") or parsed.get("message") or "")
        except Exception:
            detail = ""
        raise OAuthError("http_%s%s" % (exc.code, (":" + detail) if detail else "")) from None


def _http_form(url: str, body: dict[str, str], headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    hdrs = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": HTTP_USER_AGENT,
    }
    if headers:
        hdrs.update(headers)
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise OAuthError("token_http_%s" % exc.code) from None


def register_public_client(
    redirect_uri: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    http_json: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    meta = metadata or fetch_oauth_metadata()
    endpoint = str(meta.get("registration_endpoint") or "")
    if not endpoint:
        raise OAuthError("registration_endpoint_missing")
    payload = {
        "client_name": "AITRADE Phase 54E read-only",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": PREFERRED_SCOPE,
        "application_type": "native",
    }
    poster = http_json or _http_json
    doc = poster(endpoint, payload)
    client_id = str(doc.get("client_id") or "").strip()
    if not client_id:
        raise OAuthError("registration_missing_client_id")
    return {
        "client_id": client_id,
        "client_secret": str(doc.get("client_secret") or ""),
        "token_endpoint_auth_method": str(doc.get("token_endpoint_auth_method") or "none"),
    }


def build_authorize_url(
    *,
    metadata: dict[str, Any],
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str = PREFERRED_SCOPE,
) -> str:
    endpoint = str(metadata.get("authorization_endpoint") or "")
    if not endpoint:
        raise OAuthError("authorization_endpoint_missing")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": str(metadata.get("resource") or RESOURCE),
    }
    return endpoint + "?" + urllib.parse.urlencode(params)


def exchange_authorization_code(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
    client_secret: str = "",
    metadata: Optional[dict[str, Any]] = None,
    http_form: Optional[Callable[..., dict[str, Any]]] = None,
    scope: str = PREFERRED_SCOPE,
) -> dict[str, Any]:
    if not str(code or "").strip():
        raise OAuthError("missing_authorization_code")
    meta = metadata or fetch_oauth_metadata()
    token_url = str(meta.get("token_endpoint") or "")
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "resource": str(meta.get("resource") or RESOURCE),
    }
    if scope:
        body["scope"] = scope
    headers: dict[str, str] = {}
    if client_secret:
        body["client_secret"] = client_secret
    poster = http_form or _http_form
    try:
        doc = poster(token_url, body, headers)
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("token_exchange_failed") from None
    if doc.get("error"):
        raise OAuthError("token_exchange_rejected")
    access = str(doc.get("access_token") or "").strip()
    if not access:
        raise OAuthError("token_exchange_missing_access_token")
    return doc


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = "",
    client_secret: str = "",
    metadata: Optional[dict[str, Any]] = None,
    http_form: Optional[Callable[..., dict[str, Any]]] = None,
    scope: str = PREFERRED_SCOPE,
) -> dict[str, Any]:
    if not str(refresh_token or "").strip():
        raise OAuthError("missing_refresh_token")
    meta = metadata or DEFAULT_METADATA
    token_url = str(meta.get("token_endpoint") or DEFAULT_METADATA["token_endpoint"])
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
        "resource": str(meta.get("resource") or RESOURCE),
    }
    if client_id:
        body["client_id"] = client_id
    if client_secret:
        body["client_secret"] = client_secret
    poster = http_form or _http_form
    doc = poster(token_url, body, {})
    if doc.get("error") or not str(doc.get("access_token") or "").strip():
        raise OAuthError("refresh_rejected")
    return doc


def session_from_token_response(
    doc: dict[str, Any],
    *,
    client_id: str = "",
    client_secret: str = "",
    fallback_scope: str = PREFERRED_SCOPE,
) -> dict[str, Any]:
    exp_in = doc.get("expires_in")
    expires_at = time.time() + float(exp_in) if exp_in else time.time() + 3600
    granted = str(doc.get("scope") or fallback_scope).strip() or fallback_scope
    out = {
        "access_token": str(doc.get("access_token") or ""),
        "refresh_token": str(doc.get("refresh_token") or ""),
        "expires_at": _iso(expires_at),
        "scope": granted,
        "token_type": str(doc.get("token_type") or "Bearer"),
        "client_id": client_id,
        "resource": RESOURCE,
        "PROP_EXECUTION": False,
    }
    if client_secret:
        out["client_secret"] = client_secret
    return out


def _restrict_windows_acl(path: Path) -> None:
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        import subprocess

        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", "%s:(R,W)" % user],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass


def save_oauth_session(doc: dict[str, Any], path: Path = OAUTH_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name == "nt":
        _restrict_windows_acl(path)
    bump_auth_generation()
    return path


def load_oauth_session(path: Path = OAUTH_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except json.JSONDecodeError:
        return {}


def oauth_session_metadata(path: Path = OAUTH_PATH) -> dict[str, Any]:
    """Public session facts only. Never includes tokens."""
    doc = load_oauth_session(path)
    present = bool(doc) and not doc.get("invalid") and bool(str(doc.get("access_token") or doc.get("refresh_token") or "").strip())
    return {
        "session_present": present,
        "scope": str(doc.get("scope") or "") if present else None,
        "has_refresh_token": bool(str(doc.get("refresh_token") or "").strip()) if present else False,
        "expires_at": doc.get("expires_at") if present else None,
        "PROP_EXECUTION": False,
    }


def invalidate_oauth_session(path: Path = OAUTH_PATH) -> None:
    doc = load_oauth_session(path)
    if not doc:
        bump_auth_generation()
        return
    doc["access_token"] = ""
    doc["refresh_token"] = ""
    doc["invalid"] = True
    save_oauth_session(doc, path)
    bump_auth_generation()


def _env_credentials() -> dict[str, str]:
    return {
        "access_token": os.environ.get("FUNDEDNEXT_MCP_ACCESS_TOKEN", "").strip(),
        "refresh_token": os.environ.get("FUNDEDNEXT_MCP_REFRESH_TOKEN", "").strip(),
        "client_id": os.environ.get("FUNDEDNEXT_MCP_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("FUNDEDNEXT_MCP_CLIENT_SECRET", "").strip(),
    }


def resolve_access_token(
    *,
    path: Path = OAUTH_PATH,
    now: Optional[float] = None,
    http_form: Optional[Callable[..., dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
    force_refresh: bool = False,
) -> Optional[str]:
    """Env credentials first, then local session file, else None."""
    clock = time.time() if now is None else now
    env = _env_credentials()
    if env["access_token"] and not force_refresh:
        return env["access_token"]
    file_doc = load_oauth_session(path)
    refresh = env["refresh_token"] or str(file_doc.get("refresh_token") or "").strip()
    client_id = env["client_id"] or str(file_doc.get("client_id") or "")
    client_secret = env["client_secret"] or str(file_doc.get("client_secret") or "")
    token = str(file_doc.get("access_token") or "").strip()
    expires = parse_expires_at(file_doc.get("expires_at"))
    if token and not force_refresh and (not expires or expires - 60 > clock) and not env["refresh_token"]:
        return token
    if env["refresh_token"] or refresh:
        try:
            fresh = refresh_access_token(
                env["refresh_token"] or refresh,
                client_id=client_id,
                client_secret=client_secret,
                metadata=metadata,
                http_form=http_form,
            )
        except OAuthError:
            invalidate_oauth_session(path)
            return None
        session = session_from_token_response(fresh, client_id=client_id, client_secret=client_secret)
        merged = dict(file_doc)
        merged.update(session)
        if fresh.get("refresh_token"):
            merged["refresh_token"] = fresh.get("refresh_token")
        save_oauth_session(merged, path)
        return str(session["access_token"])
    if token:
        invalidate_oauth_session(path)
    return None


class CallbackRejected(OAuthError):
    pass


class CallbackTimeout(OAuthError):
    pass


def wait_for_callback(
    server: HTTPServer,
    *,
    expected_state: str,
    timeout_sec: float = 180.0,
    ready: Optional[threading.Event] = None,
) -> str:
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            state = (qs.get("state") or [""])[0]
            code = (qs.get("code") or [""])[0]
            err = (qs.get("error") or [""])[0]
            if state != expected_state:
                result["error"] = "state_mismatch"
                self._html(400, "AITRADE rejected this callback (state mismatch).")
                return
            if err:
                result["error"] = "provider_error"
                self._html(400, "FundedNext authorization was not granted.")
                return
            if not code:
                result["error"] = "missing_code"
                self._html(400, "AITRADE rejected this callback (missing code).")
                return
            result["code"] = code
            self._html(200, "AITRADE authenticated. You can close this window.")

        def _html(self, status: int, message: str) -> None:
            body = ("<!doctype html><html><body><p>%s</p></body></html>" % message).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server.RequestHandlerClass = Handler
    if ready is not None:
        ready.set()
    server.timeout = 0.5
    deadline = time.time() + float(timeout_sec)
    while time.time() < deadline and "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("error") == "state_mismatch":
        raise CallbackRejected("state_mismatch")
    if result.get("error"):
        raise CallbackRejected(str(result.get("error")))
    if not result.get("code"):
        raise CallbackTimeout("callback_timeout")
    return str(result["code"])


def bind_localhost_callback() -> tuple[HTTPServer, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    server = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    redirect = "http://127.0.0.1:%s/callback" % port
    return server, redirect


def login_interactive(
    *,
    timeout_sec: float = 180.0,
    metadata: Optional[dict[str, Any]] = None,
    open_browser: Optional[Callable[[str], Any]] = webbrowser.open,
    http_json: Optional[Callable[..., dict[str, Any]]] = None,
    http_form: Optional[Callable[..., dict[str, Any]]] = None,
    path: Path = OAUTH_PATH,
) -> dict[str, Any]:
    meta = metadata or fetch_oauth_metadata()
    server, redirect_uri = bind_localhost_callback()
    try:
        client = register_public_client(redirect_uri, metadata=meta, http_json=http_json)
        verifier = generate_pkce_verifier()
        challenge = pkce_challenge_s256(verifier)
        state = generate_state()
        url = build_authorize_url(
            metadata=meta,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            scope=PREFERRED_SCOPE,
        )
        if open_browser:
            open_browser(url)
        print("Waiting for localhost callback. If the browser did not open, paste this authorization URL into a browser on this machine:", flush=True)
        print(url, flush=True)
        code = wait_for_callback(server, expected_state=state, timeout_sec=timeout_sec)
        tokens = exchange_authorization_code(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            client_id=client["client_id"],
            client_secret=client.get("client_secret") or "",
            metadata=meta,
            http_form=http_form,
            scope=PREFERRED_SCOPE,
        )
        granted = str(tokens.get("scope") or PREFERRED_SCOPE)
        session = session_from_token_response(
            tokens,
            client_id=client["client_id"],
            client_secret=client.get("client_secret") or "",
            fallback_scope=granted,
        )
        session["mcp_write_required"] = False
        session["granted_scope"] = granted
        save_oauth_session(session, path)
        return {
            "ok": True,
            "authenticated": True,
            "scope": granted,
            "mcp_write_required": False,
            "redirect_uri": redirect_uri,
            "storage": str(path),
            "PROP_EXECUTION": False,
        }
    finally:
        try:
            server.server_close()
        except Exception:
            pass
