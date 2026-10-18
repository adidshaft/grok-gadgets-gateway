"""Sign-in by Client ID Metadata Document (GW-COMPAT-002 #176, hub ADR 014 §2).

An HTTPS URL as `client_id`: the gateway fetches the document with SSRF limits, checks it and
keeps it like a registered client. Dynamic registration keeps working beside it.
"""

import asyncio
import datetime
import json
import ssl

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from test_http import free_port
from test_oauth import Memory, list_devices, oauth_server, signed_in_client

from grok_gadgets_gateway import client_documents as cd
from grok_gadgets_gateway.oauth import approve, pending_requests

URL = "https://client.example.com/oauth/client.json"
CALLBACK = "https://client.example.com/callback"


def document(**changes):
    value = {
        "client_id": URL,
        "client_name": "Example client",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    value.update(changes)
    return {k: v for k, v in value.items() if v is not None}


def public(host):
    return ["93.184.215.14"]


class Network:
    """A fake HTTPS GET: records each request and answers from `pages`."""

    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    async def __call__(self, address, host, path):
        self.calls.append((address, host, path))
        answer = self.pages.get(path, (404, {"content-type": "text/plain"}, b"no"))
        if isinstance(answer, Exception):
            raise answer
        return answer


def page(value, status=200, content_type="application/json"):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return (status, {"content-type": content_type}, body)


def fetcher(network, resolver=public, **kwargs):
    return cd.DocumentFetcher(resolver=resolver, get=network, **kwargs)


# The URL


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://client.example.com/a/b/c.json",
        "https://client.example.com:443/oauth/client.json",
    ],
)
def test_document_urls(url):
    assert cd.is_document_url(url)
    cd.check_url(url)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://client.example.com/c.json", "https"),
        ("https://client.example.com/", "path"),
        ("https://client.example.com", "path"),
        ("https://client.example.com:8443/c.json", "port"),
        ("https://93.184.215.14/c.json", "name"),
        ("https://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]/c.json", "name"),
        ("https://user:pw@client.example.com/c.json", "userinfo"),
        ("https://client.example.com/c.json#x", "fragment"),
        ("https://client.example.com/c.json?x=1", "query"),
        ("https://localhost/c.json", "name"),
        ("https://my-mac.local/c.json", "name"),
        ("https://client.example.com/" + "a" * 2100, "long"),
    ],
)
def test_bad_document_urls(url, reason):
    with pytest.raises(cd.DocumentRejected) as exc:
        cd.check_url(url)
    assert exc.value.reason.startswith("url_")
    assert reason in exc.value.reason


def test_a_registered_client_id_is_not_a_document_url():
    assert not cd.is_document_url("6f1c0a6e-2b55-4b1e-9df4-9a6d8a8f0d11")
    assert not cd.is_document_url("http://client.example.com/c.json")


# Addresses (SSRF)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["192.168.1.20"],
        ["172.16.0.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["::1"],
        ["fe80::1"],
        ["fd00::1"],
        ["::ffff:10.0.0.1"],
        ["93.184.215.14", "10.0.0.5"],
        [],
    ],
)
async def test_private_and_special_addresses_are_refused(addresses):
    network = Network({"/oauth/client.json": page(document())})
    fetch = fetcher(network, resolver=lambda host: addresses)
    with pytest.raises(cd.DocumentRejected) as exc:
        await fetch.fetch(URL)
    assert exc.value.reason in ("address", "dns")
    assert network.calls == []


async def test_the_connection_goes_to_the_checked_address():
    network = Network({"/oauth/client.json": page(document())})
    info = await fetcher(network).fetch(URL)
    assert network.calls == [("93.184.215.14", "client.example.com", "/oauth/client.json")]
    assert info.client_id == URL
    assert info.client_name == "Example client"
    assert info.token_endpoint_auth_method == "none"
    assert [str(u) for u in info.redirect_uris] == [CALLBACK]


async def test_a_resolver_failure_is_refused():
    def broken(host):
        raise OSError("no such name")

    with pytest.raises(cd.DocumentRejected) as exc:
        await fetcher(Network(), resolver=broken).fetch(URL)
    assert exc.value.reason == "dns"


# The response


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (page(document(), status=302), "status"),
        (page(document(), status=404), "status"),
        (page(document(), content_type="text/html"), "content_type"),
        (page(b"{not json"), "json"),
        (page([1, 2]), "json"),
        (page(b" " * (cd.MAX_DOCUMENT_BYTES + 1)), "size"),
        (TimeoutError(), "timeout"),
        (ssl.SSLCertVerificationError("bad certificate"), "tls"),
        (OSError("refused"), "connect"),
    ],
)
async def test_bad_responses_are_refused(answer, reason):
    fetch = fetcher(Network({"/oauth/client.json": answer}))
    with pytest.raises(cd.DocumentRejected) as exc:
        await fetch.fetch(URL)
    assert exc.value.reason == reason


async def test_json_with_a_charset_is_accepted():
    answer = page(document(), content_type="application/json; charset=utf-8")
    assert (await fetcher(Network({"/oauth/client.json": answer})).fetch(URL)).client_id == URL


# The document


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"client_id": "https://other.example.com/oauth/client.json"}, "client_id"),
        ({"client_id": URL + "x"}, "client_id"),
        ({"redirect_uris": None}, "redirect_uris"),
        ({"redirect_uris": []}, "redirect_uris"),
        ({"redirect_uris": ["http://client.example.com/cb"]}, "redirect_uris"),
        ({"redirect_uris": ["javascript:alert(1)"]}, "redirect_uris"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "auth_method"),
        ({"client_secret": "s3cret-value-1234"}, "client_secret"),
        ({"grant_types": ["client_credentials"]}, "grant_types"),
        ({"response_types": ["token"]}, "response_types"),
        ({"client_name": "x" * 201}, "client_name"),
    ],
)
async def test_bad_documents_are_refused(changes, reason):
    fetch = fetcher(Network({"/oauth/client.json": page(document(**changes))}))
    with pytest.raises(cd.DocumentRejected) as exc:
        await fetch.fetch(URL)
    assert exc.value.reason == reason


async def test_a_loopback_redirect_and_defaults_are_accepted():
    value = document(redirect_uris=["http://127.0.0.1:33418/callback"])
    for key in ("grant_types", "response_types", "token_endpoint_auth_method"):
        value.pop(key)
    info = await fetcher(Network({"/oauth/client.json": page(value)})).fetch(URL)
    assert [str(u) for u in info.redirect_uris] == ["http://127.0.0.1:33418/callback"]
    assert info.token_endpoint_auth_method == "none"


# The real HTTPS GET, against a local server with its own CA


def _certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "client.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("client.example.com")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem, key_path = directory / "cert.pem", directory / "key.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pem, key_path


async def https_server(tmp_path, respond):
    pem, key = _certificate(tmp_path)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pem, key)
    seen = []

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        seen.append(request.decode())
        writer.write(respond(request))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1], pem, seen


def http_response(status, body, content_type="application/json", extra=""):
    return (
        f"HTTP/1.1 {status} X\r\ncontent-type: {content_type}\r\ncontent-length: {len(body)}\r\n"
        f"{extra}connection: close\r\n\r\n"
    ).encode() + body


async def test_the_https_get_names_the_host_and_checks_its_certificate(tmp_path):
    body = json.dumps(document()).encode()
    server, port, pem, seen = await https_server(tmp_path, lambda _: http_response(200, body))
    try:
        get = cd.https_getter(verify=ssl.create_default_context(cafile=str(pem)), port=port)
        status, _, data = await get("127.0.0.1", "client.example.com", "/oauth/client.json")
        assert (status, json.loads(data)) == (200, document())
        assert "host: client.example.com" in seen[0].lower()
        # The system trust store does not know this certificate: refused.
        with pytest.raises(ssl.SSLError):
            await cd.https_getter(port=port)("127.0.0.1", "client.example.com", "/c.json")
        # A name the certificate does not hold: refused.
        with pytest.raises(ssl.SSLError):
            await get("127.0.0.1", "other.example.com", "/c.json")
    finally:
        server.close()


async def test_the_https_get_never_follows_a_redirect_and_stops_reading_early(tmp_path):
    def respond(request):
        if b"/big" in request:
            return http_response(200, b"x" * 100_000)
        return http_response(302, b"", extra="location: http://169.254.169.254/\r\n")

    server, port, pem, seen = await https_server(tmp_path, respond)
    try:
        get = cd.https_getter(verify=ssl.create_default_context(cafile=str(pem)), port=port)
        status, _, _ = await get("127.0.0.1", "client.example.com", "/oauth/client.json")
        assert status == 302 and len(seen) == 1
        status, _, data = await get("127.0.0.1", "client.example.com", "/big")
        assert len(data) == cd.MAX_DOCUMENT_BYTES + 1
    finally:
        server.close()


# The provider: cache, refresh and floods


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def provider(tmp_path, network, clock=None, log=None):
    from grok_gadgets_gateway.oauth import OwnerApprovedOAuth

    return OwnerApprovedOAuth(
        "http://localhost:1",
        tmp_path / "oauth",
        log=log,
        clock=clock or Clock(),
        documents=fetcher(network),
    )


class Lines:
    def __init__(self):
        self.lines = []

    def write(self, record):
        self.lines.append(record)


async def test_a_document_is_kept_for_ten_minutes_then_fetched_again(tmp_path):
    network = Network({"/oauth/client.json": page(document())})
    clock = Clock()
    log = Lines()
    oauth = provider(tmp_path, network, clock, log)
    first = await oauth.get_client(URL)
    assert first.client_name == "Example client"
    clock.now += 599
    await oauth.get_client(URL)
    assert len(network.calls) == 1
    network.pages["/oauth/client.json"] = page(document(client_name="Renamed client"))
    clock.now += 2
    assert (await oauth.get_client(URL)).client_name == "Renamed client"
    assert len(network.calls) == 2
    fetched = [line for line in log.lines if line["event"] == "oauth_client_document"]
    assert fetched[0]["client_name"] == "Example client"
    assert fetched[0]["client_host"] == "client.example.com"
    assert all("redirect" not in json.dumps(line) for line in fetched)
    # It is stored with the registered clients, so a restart keeps it.
    state = json.loads((tmp_path / "oauth" / "state.json").read_text())
    assert URL in state["clients"]


async def test_a_refused_document_logs_once_a_minute_and_signs_nobody_in(tmp_path):
    network = Network({"/oauth/client.json": page(document(client_id="https://x.example.com/c"))})
    clock = Clock()
    log = Lines()
    oauth = provider(tmp_path, network, clock, log)
    for _ in range(3):
        assert await oauth.get_client(URL) is None
    clock.now += 61
    assert await oauth.get_client(URL) is None
    rejected = [line for line in log.lines if line["event"] == "oauth_cimd_rejected"]
    assert [line["reason"] for line in rejected] == ["client_id", "client_id"]
    assert "x.example.com" not in json.dumps(rejected)


async def test_fetches_have_a_budget(tmp_path):
    network = Network()
    clock = Clock()
    log = Lines()
    oauth = provider(tmp_path, network, clock, log)
    for n in range(cd.FETCHES_PER_MINUTE + 5):
        await oauth.get_client(f"https://client.example.com/c{n}.json")
    assert len(network.calls) == cd.FETCHES_PER_MINUTE
    assert any(line.get("reason") == "cimd_fetches" for line in log.lines)
    clock.now += 61
    await oauth.get_client("https://client.example.com/again.json")
    assert len(network.calls) == cd.FETCHES_PER_MINUTE + 1


async def test_a_signed_in_client_survives_an_outage_of_its_document_host(tmp_path):
    network = Network({"/oauth/client.json": page(document())})
    clock = Clock()
    oauth = provider(tmp_path, network, clock)
    info = await oauth.get_client(URL)
    oauth._issue(info, "grok-bot", ["gadgets"])  # A live sign-in for this client.
    network.pages["/oauth/client.json"] = OSError("down")
    clock.now += 700
    assert (await oauth.get_client(URL)).client_id == URL
    # Without a live sign-in the stale document is not used.
    other = provider(tmp_path / "second", Network({"/oauth/client.json": page(document())}))
    await other.get_client(URL)
    other.documents.get.pages["/oauth/client.json"] = OSError("down")
    other.clock.now += 700
    assert await other.get_client(URL) is None


async def test_registration_still_works_beside_documents(tmp_path):
    port = free_port()
    folder = tmp_path / "oauth"
    network = Network({"/oauth/client.json": page(document(redirect_uris=[CALLBACK]))})
    async with oauth_server(tmp_path, port, client_documents=fetcher(network)) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        async with httpx2.AsyncClient() as plain:
            server = (
                await plain.get(f"http://localhost:{port}/.well-known/oauth-authorization-server")
            ).json()
        assert server["client_id_metadata_document_supported"] is True
        assert server["registration_endpoint"].endswith("/register")
        auth, storage = signed_in_client(url, folder)
        await list_devices(url, auth)
        assert storage.client.client_id != URL  # Registered, not a document.
    assert network.calls == []


# The whole sign-in with the SDK's own client, by document


async def test_a_scripted_client_signs_in_by_document_and_refreshes(tmp_path):
    from urllib.parse import parse_qs, urlparse

    from mcp.shared.auth import AuthorizationCodeResult

    port = free_port()
    folder = tmp_path / "oauth"
    callback = "http://localhost:9/callback"
    network = Network({"/oauth/client.json": page(document(redirect_uris=[callback]))})
    async with oauth_server(tmp_path, port, client_documents=fetcher(network)) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        result = {}
        pages = []

        async def browser(authorization_url):
            assert "client_id=https%3A%2F%2Fclient.example.com" in authorization_url
            async with httpx2.AsyncClient(follow_redirects=False) as http:
                first = await http.get(authorization_url)
                assert first.status_code == 302
                approve_page = first.headers["location"]
                waiting = await http.get(approve_page)
                pages.append(waiting.text)
                code = pending_requests(folder)[0]["code"]
                approve(code, "scripted", folder)
                done = await http.get(approve_page)
                query = parse_qs(urlparse(done.headers["location"]).query)
                result.update({k: v[0] for k, v in query.items()})

        async def get_code():
            return AuthorizationCodeResult(code=result["code"], state=result.get("state"))

        storage = Memory()
        metadata = OAuthClientMetadata(client_name="ignored", redirect_uris=[callback])
        auth = OAuthClientProvider(
            url, metadata, storage, browser, get_code, client_metadata_url=URL
        )
        await list_devices(url, auth)
        assert storage.client.client_id == URL
        assert storage.tokens.refresh_token.startswith("ggr_")
        # The page shows the name the document gives, as self-declared.
        assert "Example client" in pages[0]
        # Refresh by document client.
        async with httpx2.AsyncClient() as http:
            refreshed = await http.post(
                f"http://localhost:{port}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": storage.tokens.refresh_token,
                    "client_id": URL,
                },
            )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["access_token"].startswith("gga_")
    assert len(network.calls) == 1
