"""Owner-approved OAuth sign-in for `serve --oauth` (gateway #71).

The gateway is its own OAuth 2.1 authorization server, built on the MCP SDK's auth routes:
protected-resource metadata, dynamic client registration, PKCE, refresh and revocation.
The sign-in page asks for nothing. It shows a short code, and the owner approves that code
on this computer with `grok-gadgets-gateway oauth approve CODE`. No password or token is
ever typed into a web page or a chat.

`serve` owns `state.json` and `requests/`; the CLI writes only `approved/` and `revoked/`,
so the two processes never write the same file. Tokens are stored as SHA-256 hashes.

In accounts mode (`serve --oauth --accounts`, hub ADR 011) the page also takes the code a
gadget shows, or a recovery code. Each token record then carries its account, and a refresh
copies it, so the refresh-token family carries the account; the `client_id` never does.
"""

import hashlib
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse, urlsplit

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

from . import sign_in_page
from .accounts import REQUEST_WRONG_MAX, ClaimRejected
from .client_documents import (
    DOCUMENT_SECONDS,
    FETCHES_PER_MINUTE,
    DocumentFetcher,
    DocumentRejected,
    is_document_url,
)
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
APPROVE_PATH = sign_in_page.APPROVE_PATH
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

    def __init__(
        self, issuer, directory=None, log=None, clock=time.time, accounts=None, documents=None
    ):
        self.issuer = issuer.rstrip("/")
        # Client ID Metadata Documents (#176): an HTTPS client_id is fetched, checked and kept
        # like a registered client. `documents` is replaced in tests.
        self.documents = documents if documents is not None else DocumentFetcher()
        self.fetches = deque()  # When recent document fetches started (FETCHES_PER_MINUTE).
        self.document_logged = {}  # Refusal reason -> when it was last logged.
        self.accounts = accounts
        self.root = _private_dir(Path(directory or oauth_dir()))
        for sub in ("requests", "approved", "revoked"):
            _private_dir(self.root / sub)
        self.log = log
        self.clock = clock
        self.state = _read_json(self.root / "state.json", {})
        for key in ("clients", "access", "refresh", "registered_at", "fetched_at"):
            self.state.setdefault(key, {})
        self.alerted_at = None
        self.expired_seen = set()  # Access-token digests already logged as expired (#211).
        self.requests = {}  # request id -> pending authorization (memory only)
        self.codes = {}  # authorization code -> AuthorizationCode (memory only)
        self.code_accounts = {}  # authorization code -> account (accounts mode)
        # Stale files from an earlier run name requests this process cannot finish.
        for path in [*(self.root / "requests").glob("*"), *(self.root / "approved").glob("*")]:
            path.unlink(missing_ok=True)
        if accounts is not None:
            # Migration (ADR 011 §11): live sign-ins from before accounts mode, such as the
            # operator's grok-bot, join the operator account and keep working.
            changed = False
            for kind in ("access", "refresh"):
                for record in self.state[kind].values():
                    if "account" not in record:
                        record["account"] = accounts.operator
                        changed = True
            if changed:
                self._save()

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
        known = OAuthClientInformationFull.model_validate(record) if record else None
        if not is_document_url(client_id):
            return known
        now = self.clock()
        if (
            known is not None
            and now - self.state["fetched_at"].get(client_id, 0) < DOCUMENT_SECONDS
        ):
            return known
        try:
            info = await self._fetch_document(client_id, now)
        except DocumentRejected as exc:
            self._document_refused(exc.reason, now)
            # A client already signed in keeps working while its document host is down.
            return known if known is not None and client_id in self._clients_in_use() else None
        self._prune_clients()
        if client_id not in self.state["clients"] and len(self.state["clients"]) >= MAX_CLIENTS:
            self._flood("clients_full")
            return None
        self.state["clients"][client_id] = info.model_dump(mode="json")
        self.state["registered_at"][client_id] = now
        self.state["fetched_at"][client_id] = now
        self._save()
        self._write(
            "oauth_client_document",
            client_name=info.client_name,
            client_host=urlsplit(client_id).hostname,
        )
        return info

    async def _fetch_document(self, url, now):
        while self.fetches and now - self.fetches[0] >= 60:
            self.fetches.popleft()
        if len(self.fetches) >= FETCHES_PER_MINUTE:
            self._flood("cimd_fetches")
            raise DocumentRejected("budget")
        self.fetches.append(now)
        return await self.documents.fetch(url)

    def _document_refused(self, reason, now):
        """One `oauth_cimd_rejected` line a minute for each reason; never the URL or document."""
        last = self.document_logged.get(reason)
        if last is None or now - last >= FLOOD_ALERT_SECONDS:
            self.document_logged[reason] = now
            self._write("oauth_cimd_rejected", reason=reason)

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
            self.state["fetched_at"].pop(client_id, None)
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
            "client_name": client.client_name,
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
            return _html(sign_in_page.ended_page("expired"), status=400)
        params = pending["params"]
        decision = _read_json(self.root / "approved" / pending["code"], None)
        if decision is None and self.accounts is not None:
            return self._code_page(request_id, pending)
        if decision is None:
            return _html(sign_in_page.waiting_page(pending.get("client_name"), pending["code"]))
        self._forget(request_id)
        if decision.get("deny"):
            self._write("oauth_sign_in_denied", code=pending["code"])
            target = construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            )
            return RedirectResponse(target, status_code=302, headers=_NO_STORE)
        account = self.accounts.operator if self.accounts is not None else None
        target = self._grant(pending, decision["name"], account, now)
        return RedirectResponse(target, status_code=302, headers=_NO_STORE)

    def _grant(self, pending, name, account, now):
        """Issue the authorization code for an approved request; returns the redirect URL."""
        params = pending["params"]
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
            subject=name,
        )
        if account is not None:
            self.code_accounts[code] = account
        self._write("oauth_sign_in_approved", code=pending["code"], token=name)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def _code_page(self, request_id, pending, error=None):
        tries = REQUEST_WRONG_MAX - pending.get("wrong", 0) if error else None
        return _html(
            sign_in_page.code_page(
                request_id, pending.get("client_name"), pending["code"], error, tries
            )
        )

    async def code_submitted(self, request):
        """POST /oauth/approve (accounts mode): check a claim or recovery code (ADR 011 §4)."""
        from .service import caller

        now = self.clock()
        self._expire(now)
        form = await request.form()
        request_id = str(form.get("request", ""))
        pending = self.requests.get(request_id)
        if pending is None or self.accounts is None:
            return _html(sign_in_page.ended_page("expired"), status=400)
        decision = _read_json(self.root / "approved" / pending["code"], None)
        text = str(form.get("code", "")).strip()
        if decision is not None or not text:
            if decision is None:
                return self._code_page(request_id, pending)
            return await self.approve_page(_QueryOnly(request_id))
        headers = {k.lower().encode(): v.encode() for k, v in request.headers.items()}
        who = caller(request.scope, headers)
        try:
            account, _ = self.accounts.sign_in_code(text, caller=who)
        except ClaimRejected as exc:
            pending["wrong"] = pending.get("wrong", 0) + 1
            if exc.limited or pending["wrong"] >= REQUEST_WRONG_MAX:
                self._forget(request_id)
                return _html(sign_in_page.ended_page("limited"), status=400)
            return self._code_page(request_id, pending, "wrong")
        self._forget(request_id)
        target = self._grant(pending, account, account, now)
        # Straight back to Grok Bot, as the operator approval does: a phone's sign-in sheet
        # finishes only on this redirect, never on a link. The person gets a recovery code
        # later from gadgets_share action=recovery_code (hub ADR 011 §6).
        return RedirectResponse(target, status_code=302, headers=_NO_STORE)

    async def load_authorization_code(self, client, authorization_code):
        value = self.codes.get(authorization_code)
        if value and value.client_id == client.client_id and value.expires_at > self.clock():
            return value
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        account = self.code_accounts.pop(authorization_code.code, None)
        return self._issue(client, authorization_code.subject, authorization_code.scopes, account)

    def _issue(self, client, name, scopes, account=None, event="oauth_token_issued"):
        now = self.clock()
        access = "gga_" + secrets.token_urlsafe(32)
        refresh = "ggr_" + secrets.token_urlsafe(32)
        base = {"name": name, "client_id": client.client_id, "scopes": scopes, "issued_at": now}
        if account is not None:
            base["account"] = account
        self.state["access"][_digest(access)] = {**base, "expires_at": now + ACCESS_SECONDS}
        self.state["refresh"][_digest(refresh)] = {
            **base,
            "expires_at": now + REFRESH_SECONDS,
            "access": _digest(access),
        }
        self._save()
        self._write(event, token=name, client_name=client.client_name)
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

    def _refused(self, kind, token, client=None):
        """Why a token is not live: `unknown` (never issued, used or pruned), `expired`,
        `revoked` or `client` (another client's token). Logs never hold a token."""
        record = self.state[kind].get(_digest(token))
        if record is None:
            return "unknown", None
        if record["expires_at"] <= self.clock():
            return "expired", record
        if _revoked(self.root, record):
            return "revoked", record
        if client is not None and record["client_id"] != client.client_id:
            return "client", record
        return None, record

    async def load_refresh_token(self, client, refresh_token):
        reason, record = self._refused("refresh", refresh_token, client)
        if reason is not None:
            # GW-OAUTH-REFRESH-001 (#211): show whether Grok Bot tried to refresh, and why not.
            self._write(
                "oauth_refresh_rejected",
                reason=reason,
                token=record["name"] if record else None,
                client_name=client.client_name,
            )
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
        return self._issue(
            client,
            record["name"],
            scopes or record["scopes"],
            record.get("account"),
            event="oauth_token_refreshed",
        )

    async def load_access_token(self, token):
        record = self._live("access", token)
        if record is None:
            reason, expired = self._refused("access", token)
            digest = _digest(token)
            if reason == "expired" and digest not in self.expired_seen:
                # Once per token: the next line in the log should be a refresh.
                self.expired_seen.add(digest)
                client = self.state["clients"].get(expired["client_id"]) or {}
                self._write(
                    "oauth_access_expired",
                    token=expired["name"],
                    client_name=client.get("client_name"),
                )
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

    async def verify_account(self, token):
        """The account of a live access token (accounts mode); None otherwise."""
        record = self._live("access", token) if token.startswith("gga_") else None
        return record.get("account") if record else None


class _QueryOnly:
    """The part of a request that approve_page reads, for a form post that finishes it."""

    def __init__(self, request_id):
        self.query_params = {"request": request_id}


_NO_STORE = sign_in_page.NO_STORE


def _html(text, status=200):
    return HTMLResponse(text, status_code=status, headers=sign_in_page.HEADERS)
