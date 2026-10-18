"""Client ID Metadata Documents (GW-COMPAT-002 #176, hub ADR 014 §2).

MCP 2026-07-28 prefers an HTTPS URL as `client_id` to dynamic registration. The authorization
server fetches the JSON document at that URL to learn the client's name and redirect URIs.
Fetching a URL that a stranger chose is a server-side request, so every fetch here is limited:

- HTTPS on port 443 to a public DNS name; no address literal, user info, query or fragment.
- Every resolved address must be public; the connection goes to the address that was checked,
  and TLS still verifies the certificate for the name.
- At most 5 s and 16 KB, no redirects, no proxy from the environment, `application/json` only.
- The document's `client_id` must equal the URL; public clients only (`none`), code flow only.

The owner's approval or the gadget's claim code still decides every sign-in. The document's
`client_name` is self-declared, and the sign-in page says so.
"""

import asyncio
import inspect
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit

from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ValidationError

from .lan import is_dns_name

MAX_URL = 2048
MAX_DOCUMENT_BYTES = 16384
FETCH_SECONDS = 5
DOCUMENT_SECONDS = 600  # A good document is used for 10 minutes, then fetched again.
FETCHES_PER_MINUTE = 10
MAX_NAME = 200
MAX_REDIRECTS = 10
GRANTS = ("authorization_code", "refresh_token")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class DocumentRejected(Exception):
    """Why a document was refused, as a short reason for the log (never the document)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def is_document_url(client_id):
    """True when a `client_id` asks for a metadata document rather than a registration."""
    if not isinstance(client_id, str) or not client_id.startswith("https://"):
        return False
    return urlsplit(client_id).path not in ("", "/")


def check_url(url):
    """(host, path) for a document URL that may be fetched; DocumentRejected otherwise."""
    if len(url) > MAX_URL:
        raise DocumentRejected("url_long")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DocumentRejected("url_https")
    if "@" in parts.netloc:
        raise DocumentRejected("url_userinfo")
    if parts.fragment or "#" in url:
        raise DocumentRejected("url_fragment")
    if parts.query or "?" in url:
        raise DocumentRejected("url_query")
    try:
        port = parts.port
    except ValueError:
        raise DocumentRejected("url_port") from None
    if port not in (None, 443):
        raise DocumentRejected("url_port")
    host = (parts.hostname or "").lower()
    if not is_dns_name(host):
        raise DocumentRejected("url_name")
    if parts.path in ("", "/"):
        raise DocumentRejected("url_path")
    return host, parts.path


def is_public(address):
    """True for a globally routable unicast address (an IPv4-mapped IPv6 address unwrapped)."""
    try:
        ip = ipaddress.ip_address(str(address).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def https_getter(verify=True, port=443):
    """GET https://ADDRESS:PORT/PATH with Host and TLS name HOST: (status, headers, body).

    The body is cut one byte past the size limit. No redirect is followed and no proxy from
    the environment is used. TLS failures raise ssl.SSLError and timeouts TimeoutError.
    """
    import httpx2

    async def get(address, host, path):
        literal = f"[{address}]" if ":" in address else address
        headers = {
            "Host": host if port == 443 else f"{host}:{port}",
            "Accept": "application/json",
            "User-Agent": "grok-gadgets-gateway",
        }
        try:
            async with (
                httpx2.AsyncClient(
                    verify=verify, follow_redirects=False, trust_env=False, timeout=FETCH_SECONDS
                ) as http,
                http.stream(
                    "GET",
                    f"https://{literal}:{port}{path}",
                    headers=headers,
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        break
                return (
                    response.status_code,
                    dict(response.headers),
                    bytes(body)[: MAX_DOCUMENT_BYTES + 1],
                )
        except httpx2.TimeoutException:
            raise TimeoutError from None
        except httpx2.TransportError as exc:
            cause = exc.__cause__ or exc.__context__
            while cause is not None and not isinstance(cause, ssl.SSLError):
                cause = cause.__cause__ or cause.__context__
            if cause is not None:
                raise cause from None
            raise OSError(str(exc)) from None

    return get


class DocumentFetcher:
    """Fetch and check one Client ID Metadata Document. `resolver` and `get` are for tests."""

    def __init__(self, resolver=None, get=None):
        self.resolver = resolver or _resolve
        self.get = get or https_getter()

    async def _addresses(self, host):
        try:
            found = self.resolver(host)
            if inspect.isawaitable(found):
                found = await found
        except OSError:
            raise DocumentRejected("dns") from None
        found = list(found or [])
        if not found:
            raise DocumentRejected("dns")
        if not all(is_public(address) for address in found):
            raise DocumentRejected("address")
        return found

    async def fetch(self, url):
        host, path = check_url(url)
        address = (await self._addresses(host))[0]
        try:
            status, headers, body = await asyncio.wait_for(
                self.get(address, host, path), FETCH_SECONDS
            )
        except TimeoutError:
            raise DocumentRejected("timeout") from None
        except ssl.SSLError:
            raise DocumentRejected("tls") from None
        except OSError:
            raise DocumentRejected("connect") from None
        if status != 200:
            raise DocumentRejected("status")
        headers = {key.lower(): value for key, value in headers.items()}
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise DocumentRejected("content_type")
        if len(body) > MAX_DOCUMENT_BYTES:
            raise DocumentRejected("size")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise DocumentRejected("json") from None
        if not isinstance(value, dict):
            raise DocumentRejected("json")
        return client_from_document(url, value)


def _redirect_ok(uri):
    if not isinstance(uri, str) or len(uri) > MAX_URL:
        return False
    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK


def client_from_document(url, value):
    """The client a document describes; DocumentRejected when it breaks a rule."""
    from .oauth import SCOPE

    if value.get("client_id") != url:
        raise DocumentRejected("client_id")
    if "client_secret" in value:
        raise DocumentRejected("client_secret")
    if value.get("token_endpoint_auth_method", "none") != "none":
        raise DocumentRejected("auth_method")
    grants = value.get("grant_types", list(GRANTS))
    if (
        not isinstance(grants, list)
        or "authorization_code" not in grants
        or any(grant not in GRANTS for grant in grants)
    ):
        raise DocumentRejected("grant_types")
    if value.get("response_types", ["code"]) != ["code"]:
        raise DocumentRejected("response_types")
    uris = value.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= MAX_REDIRECTS
        or not all(_redirect_ok(uri) for uri in uris)
    ):
        raise DocumentRejected("redirect_uris")
    name = value.get("client_name")
    if name is not None and (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_NAME
        or not name.isprintable()
    ):
        raise DocumentRejected("client_name")
    try:
        return OAuthClientInformationFull(
            client_id=url,
            client_name=name,
            redirect_uris=uris,
            grant_types=grants,
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=SCOPE,
        )
    except ValidationError:
        raise DocumentRejected("invalid") from None
