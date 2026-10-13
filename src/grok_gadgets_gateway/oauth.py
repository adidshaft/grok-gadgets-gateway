"""Owner-approved OAuth sign-in for `serve --oauth` (gateway #71).

The gateway is its own OAuth 2.1 authorization server, built on the MCP SDK's auth routes:
protected-resource metadata, dynamic client registration, PKCE, refresh and revocation.
The sign-in page asks for nothing. It shows a short code, and the owner approves that code
on this computer with `grok-gadgets-gateway oauth approve CODE`. No password or token is
ever typed into a web page or a chat.

`serve` owns `state.json` and `requests/`; the CLI writes only `approved/` and `revoked/`,
so the two processes never write the same file. Tokens are stored as SHA-256 hashes.
"""

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse

from .operator import DEFAULT_TOKEN_NAME, TOKEN_NAME, OperatorError, atomic_write, config_dir

SCOPE = "gadgets"
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CODE_SECONDS = 300
REQUEST_SECONDS = 600
MAX_CLIENTS = 20
MAX_PENDING = 8
# Floods (hub ADR 008): a client with no live sign-in is pruned after an hour, and one
# client keeps at most two waiting sign-ins, so strangers cannot lock the owner out.
CLIENT_IDLE_SECONDS = 3600
PENDING_PER_CLIENT = 2
FLOOD_ALERT_SECONDS = 60
APPROVE_PATH = "/oauth/approve"
# No 0/O or 1/I, so a code read off a phone screen is typed correctly.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")


def oauth_dir():
    return config_dir() / "oauth"


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _code():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise OperatorError(f"{path} is damaged") from None


def _check_name(name):
    if not TOKEN_NAME.fullmatch(name or "") or name == DEFAULT_TOKEN_NAME:
        raise OperatorError("name: 1-64 letters, digits, '.', '_' or '-', and not 'default'")


def _normal_code(code):
    text = code.strip().upper().replace("-", "")
    if len(text) != 8 or any(c not in _ALPHABET for c in text):
        raise OperatorError(f"not a sign-in code: {code}")
    return f"{text[:4]}-{text[4:]}"


# --- CLI side: approve, deny, list and revoke without touching serve's own files. ---


def pending_requests(directory=None):
    folder = Path(directory or oauth_dir()) / "requests"
    now = time.time()
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        record = _read_json(path, None)
        if record and record.get("expires_at", 0) > now:
            found.append(record)
    return found


def approve(code, name, directory=None, deny=False):
    code = _normal_code(code)
    if not deny:
        _check_name(name)
    root = Path(directory or oauth_dir())
    if not (root / "requests" / f"{code}.json").is_file():
        raise OperatorError(f"no pending sign-in {code}; run: grok-gadgets-gateway oauth pending")
    decision = {"deny": True} if deny else {"name": name}
    decision["at"] = time.time()
    atomic_write(_private_dir(root / "approved") / code, json.dumps(decision).encode())
    return code


def revoke_name(name, directory=None):
    _check_name(name)
    root = Path(directory or oauth_dir())
    atomic_write(_private_dir(root / "revoked") / name, f"{time.time()}\n".encode())


def sign_ins(directory=None):
    """(name, client name, refresh expiry) for each live sign-in; never a token."""
    root = Path(directory or oauth_dir())
    state = _read_json(root / "state.json", {})
    clients = state.get("clients", {})
    now = time.time()
    rows = []
    for record in state.get("refresh", {}).values():
        if record["expires_at"] > now and not _revoked(root, record):
            client = clients.get(record["client_id"], {})
            rows.append((record["name"], client.get("client_name") or "-", record["expires_at"]))
    return sorted(rows)


def _revoked(root, record):
    try:
        revoked_at = float((root / "revoked" / record["name"]).read_text())
    except (FileNotFoundError, ValueError):
        return False
    return record["issued_at"] <= revoked_at


# --- serve side: the SDK provider and the approval page. ---


class OwnerApprovedOAuth:
    """OAuthAuthorizationServerProvider whose only consent step is the owner's CLI approval."""

    def __init__(self, issuer, directory=None, log=None, clock=time.time):
        self.issuer = issuer.rstrip("/")
        self.root = _private_dir(Path(directory or oauth_dir()))
        for sub in ("requests", "approved", "revoked"):
            _private_dir(self.root / sub)
        self.log = log
        self.clock = clock
        self.state = _read_json(self.root / "state.json", {})
        for key in ("clients", "access", "refresh", "registered_at"):
            self.state.setdefault(key, {})
        self.alerted_at = None
        self.requests = {}  # request id -> pending authorization (memory only)
        self.codes = {}  # authorization code -> AuthorizationCode (memory only)
        # Stale files from an earlier run name requests this process cannot finish.
        for path in [*(self.root / "requests").glob("*"), *(self.root / "approved").glob("*")]:
            path.unlink(missing_ok=True)

    def _write(self, event, **fields):
        if self.log is not None:
            self.log.write({"event": event, **fields})

    def _save(self):
        now = self.clock()
        for key in ("access", "refresh"):
            self.state[key] = {k: v for k, v in self.state[key].items() if v["expires_at"] > now}
        atomic_write(self.root / "state.json", json.dumps(self.state, indent=1).encode())

    async def get_client(self, client_id):
        record = self.state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    def _flood(self, reason):
        """One `oauth_flood` alert line a minute while a cap is hit."""
        now = self.clock()
        if self.alerted_at is None or now - self.alerted_at >= FLOOD_ALERT_SECONDS:
            self.alerted_at = now
            self._write("oauth_flood", reason=reason)

    def _clients_in_use(self):
        now = self.clock()
        return {
            record["client_id"]
            for kind in ("access", "refresh")
            for record in self.state[kind].values()
            if record["expires_at"] > now and not _revoked(self.root, record)
        }

    def _prune_clients(self):
        """Forget clients with no live sign-in: idle ones, then the oldest when the list is full."""
        now = self.clock()
        in_use = self._clients_in_use()
        registered = self.state["registered_at"]
        waiting = {request["client_id"] for request in self.requests.values()}
        # Oldest first; a client in the middle of a sign-in goes last.
        idle = sorted(
            (client_id in waiting, registered.get(client_id, 0), client_id)
            for client_id in self.state["clients"]
            if client_id not in in_use
        )
        removed = [c for busy, at, c in idle if not busy and now - at >= CLIENT_IDLE_SECONDS]
        spare = len(self.state["clients"]) - len(removed) - (MAX_CLIENTS - 1)
        removed += [c for _, _, c in idle if c not in removed][: max(spare, 0)]
        for client_id in removed:
            self.state["clients"].pop(client_id, None)
            registered.pop(client_id, None)
        if spare > 0:
            self._flood("clients_full")
        return removed

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            local = parsed.scheme == "http" and parsed.hostname in _LOOPBACK
            if parsed.scheme != "https" and not local:
                raise RegistrationError("invalid_redirect_uri", "redirect URIs must use https")
        self._prune_clients()
        if len(self.state["clients"]) >= MAX_CLIENTS:
            # Every client holds a live sign-in; the owner revokes one to make room.
            self._flood("clients_full")
            raise RegistrationError("invalid_client_metadata", "too many registered clients")
        self.state["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self.state["registered_at"][client_info.client_id] = self.clock()
        self._save()
        self._write("oauth_client_registered", client_name=client_info.client_name)

    async def authorize(self, client, params):
        now = self.clock()
        self._expire(now)
        # Drop the oldest waiting sign-in instead of refusing a new one: the owner approves
        # by code, so a stranger's waiting requests never block the owner's own.
        mine = [r for r, v in self.requests.items() if v["client_id"] == client.client_id]
        while len(mine) >= PENDING_PER_CLIENT:
            self._forget(mine.pop(0))
        if len(self.requests) >= MAX_PENDING:
            self._flood("pending_full")
            while len(self.requests) >= MAX_PENDING:
                self._forget(next(iter(self.requests)))
        request_id = secrets.token_urlsafe(32)
        code = _code()
        redirect_host = urlparse(str(params.redirect_uri)).hostname
        self.requests[request_id] = {
            "code": code,
            "client_id": client.client_id,
            "params": params,
            "expires_at": now + REQUEST_SECONDS,
        }
        record = {
            "code": code,
            "client_name": client.client_name,
            "redirect_host": redirect_host,
            "expires_at": now + REQUEST_SECONDS,
        }
        atomic_write(self.root / "requests" / f"{code}.json", json.dumps(record).encode())
        self._write(
            "oauth_sign_in_requested",
            code=code,
            client_name=client.client_name,
            redirect_host=redirect_host,
        )
        return f"{self.issuer}{APPROVE_PATH}?request={request_id}"

    def _expire(self, now):
        for request_id, request in list(self.requests.items()):
            if request["expires_at"] <= now:
                self._forget(request_id)
        for code, value in list(self.codes.items()):
            if value.expires_at <= now:
                del self.codes[code]

    def _forget(self, request_id):
        request = self.requests.pop(request_id, None)
        if request:
            (self.root / "requests" / f"{request['code']}.json").unlink(missing_ok=True)
            (self.root / "approved" / request["code"]).unlink(missing_ok=True)

    async def approve_page(self, request):
        """GET /oauth/approve: wait for the owner's CLI decision, then finish the redirect."""
        now = self.clock()
        self._expire(now)
        request_id = request.query_params.get("request", "")
        pending = self.requests.get(request_id)
        if pending is None:
            return _page(
                "This sign-in has expired or is unknown. Start the sign-in again from Grok Bot.",
                status=400,
            )
        params = pending["params"]
        decision = _read_json(self.root / "approved" / pending["code"], None)
        if decision is None:
            return _page(
                "Approve this sign-in on the computer that runs the gateway:"
                f"<pre>grok-gadgets-gateway oauth approve {pending['code']} --name grok-bot</pre>"
                f"Code <strong>{pending['code']}</strong>. This page continues by itself. "
                "If you did not start this sign-in, do nothing; it expires in 10 minutes.",
                refresh=True,
            )
        self._forget(request_id)
        if decision.get("deny"):
            self._write("oauth_sign_in_denied", code=pending["code"])
            target = construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            )
            return RedirectResponse(target, status_code=302, headers=_NO_STORE)
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=now + CODE_SECONDS,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=decision["name"],
        )
        self._write("oauth_sign_in_approved", code=pending["code"], token=decision["name"])
        target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(target, status_code=302, headers=_NO_STORE)

    async def load_authorization_code(self, client, authorization_code):
        value = self.codes.get(authorization_code)
        if value and value.client_id == client.client_id and value.expires_at > self.clock():
            return value
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client, authorization_code.subject, authorization_code.scopes)

    def _issue(self, client, name, scopes):
        now = self.clock()
        access = "gga_" + secrets.token_urlsafe(32)
        refresh = "ggr_" + secrets.token_urlsafe(32)
        base = {"name": name, "client_id": client.client_id, "scopes": scopes, "issued_at": now}
        self.state["access"][_digest(access)] = {**base, "expires_at": now + ACCESS_SECONDS}
        self.state["refresh"][_digest(refresh)] = {
            **base,
            "expires_at": now + REFRESH_SECONDS,
            "access": _digest(access),
        }
        self._save()
        self._write("oauth_token_issued", token=name, client_name=client.client_name)
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _live(self, kind, token):
        record = self.state[kind].get(_digest(token))
        if record is None or record["expires_at"] <= self.clock():
            return None
        if _revoked(self.root, record):
            return None
        return record

    async def load_refresh_token(self, client, refresh_token):
        record = self._live("refresh", refresh_token)
        if record is None or record["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
            subject=record["name"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        record = self.state["refresh"].pop(_digest(refresh_token.token), None)
        if record is None:
            raise TokenError("invalid_grant", "refresh token is not valid")
        self.state["access"].pop(record.get("access"), None)
        return self._issue(client, record["name"], scopes or record["scopes"])

    async def load_access_token(self, token):
        record = self._live("access", token)
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
            subject=record["name"],
        )

    async def revoke_token(self, token):
        digest = _digest(token.token)
        refresh = self.state["refresh"].pop(digest, None)
        if refresh:
            self.state["access"].pop(refresh.get("access"), None)
        if self.state["access"].pop(digest, None) is not None:
            for key, value in list(self.state["refresh"].items()):
                if value.get("access") == digest:
                    del self.state["refresh"][key]
        self._save()
        self._write("oauth_token_revoked", token=getattr(token, "subject", None))

    async def verify_name(self, token):
        """The approved name for a live access token, for BearerAuth; None otherwise."""
        record = self._live("access", token) if token.startswith("gga_") else None
        return record["name"] if record else None


_NO_STORE = {"Cache-Control": "no-store"}
_HEADERS = {
    **_NO_STORE,
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Referrer-Policy": "no-referrer",
}


def _page(body, status=200, refresh=False):
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    text = (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{meta}<title>Grok Gadgets sign-in</title>"
        "<style>body{font:16px system-ui;margin:2em auto;max-width:34em;padding:0 1em}"
        "pre{white-space:pre-wrap;background:#eee;padding:.6em}</style></head>"
        f"<body><h1>Grok Gadgets sign-in</h1><p>{body}</p></body></html>"
    )
    return HTMLResponse(text, status_code=status, headers=_HEADERS)
