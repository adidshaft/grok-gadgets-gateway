"""Preflight for a relay's device name: a DNS-only record, and the relay CA on the device port.

A proxied (orange-cloud) Cloudflare record answers with Cloudflare's own addresses, and
Cloudflare proxies some HTTPS ports, 8443 among them. A Wi-Fi device would then meet
Cloudflare's certificate instead of the relay's, and every CA pin fails. This check refuses
such a name before a board is provisioned against it.
"""

import ipaddress
import socket
import ssl

from .operator import OperatorError

# Published at https://www.cloudflare.com/ips-v4 and /ips-v6 (read 14 October 2026).
CLOUDFLARE_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def resolve_all(name):
    infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_cloudflare_proxy(address):
    ip = ipaddress.ip_address(address)
    return any(ip.version == n.version and ip in n for n in CLOUDFLARE_PROXY_NETWORKS)


def check_dns(name, resolver=resolve_all):
    """Refuse a name that does not resolve or that resolves to a Cloudflare proxy address."""
    try:
        addresses = resolver(name)
    except OSError:
        raise OperatorError(f"{name} does not resolve. Create a DNS-only record for it") from None
    if not addresses:
        raise OperatorError(f"{name} does not resolve. Create a DNS-only record for it")
    if any(is_cloudflare_proxy(address) for address in addresses):
        raise OperatorError(
            f"{name} resolves to a Cloudflare proxy address: its record is proxied. Set the "
            "record to DNS only (grey cloud). Devices connect straight to the relay, and a "
            "proxied record breaks every device's CA pin"
        )
    return addresses


def tls_handshake(name, port, ca_pem, timeout=5.0):
    """Connect as a device does: TLS, the relay CA as the only trust anchor, the name checked."""
    context = ssl.create_default_context(cadata=ca_pem)
    with (
        socket.create_connection((name, port), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=name),
    ):
        return True


def check_tls(name, port, ca_pem, handshake=tls_handshake):
    try:
        handshake(name, port, ca_pem)
    except ssl.SSLCertVerificationError:
        raise OperatorError(
            f"{name}:{port} answers with a certificate the relay CA did not issue. Check the "
            "record is DNS only and --ca is the relay's lan/ca.pem"
        ) from None
    except (OSError, ssl.SSLError):
        raise OperatorError(
            f"no relay device listener answers on {name}:{port}. Check the port: relays set "
            "up before 14 October 2026 use 8443, so pass NAME:8443"
        ) from None


def relay_check(name, port, ca_pem, resolver=None, handshake=None):
    """Both checks; returns the lines to print. Raises OperatorError on the first failure."""
    addresses = check_dns(name, resolver or resolve_all)
    lines = [f"ok  {name} is a DNS-only record ({len(addresses)} address(es), no proxy)"]
    check_tls(name, port, ca_pem, handshake or tls_handshake)
    lines.append(f"ok  {name}:{port} presents a certificate from the relay CA")
    return lines


# --- the full preflight (GW-BUILD-003, hub ADR 013 §1) ---


def ca_fingerprint(ca_pem):
    """SHA-256 of the relay CA, as `AB:CD:…`, to compare with `lan-ca show` on the host."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(ca_pem.encode())
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def check_port(name, port, timeout=5.0):
    try:
        socket.create_connection((name, port), timeout=timeout).close()
    except OSError:
        return False, (
            f"FAIL nothing accepts TCP on {name}:{port}. Open the port in the host firewall "
            f"(ufw allow {port}/tcp) and in the provider's firewall"
        )
    return True, f"ok  {name}:{port} is open"


def check_refuses_http(name, port, timeout=3.0):
    """A device listener never answers HTTP; a proxy or web server in front of it does."""
    try:
        with socket.create_connection((name, port), timeout=timeout) as sock:
            sock.sendall(b"GET / HTTP/1.0\r\nHost: " + name.encode() + b"\r\n\r\n")
            try:
                reply = sock.recv(16)
            except OSError:
                reply = b""
    except OSError:
        return False, f"FAIL cannot connect to {name}:{port} to check it refuses HTTP"
    if reply.startswith(b"HTTP/"):
        return False, (
            f"FAIL {name}:{port} answers HTTP: a proxy or web server is in front of the device "
            "listener. Point a DNS-only record straight at the relay host"
        )
    return True, f"ok  {name}:{port} refuses plain HTTP"


def check_mcp(url, timeout=10.0):
    """`/mcp` asks for a sign-in (401) and its protected-resource metadata answers (200)."""
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    parts = urlsplit(url)
    metadata = f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}"

    def status(request):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status
        except HTTPError as exc:
            return exc.code
        except (URLError, OSError):
            return None

    meta = status(Request(metadata))
    post = status(
        Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    )
    if meta == 200 and post == 401:
        return True, f"ok  {url} answers 401 without a sign-in, and its sign-in metadata 200"
    return False, (
        f"FAIL {url}: metadata {meta}, POST {post} (want 200 and 401). Check the tunnel routes "
        "the name to http://127.0.0.1:8766 and serve runs with --oauth"
    )


def full_check(name, port, ca_pem, mcp_url=None, resolver=None, handshake=None, fingerprint=None):
    """Every preflight item as (ok, line); later checks still run after a failure."""
    results = []
    try:
        addresses = check_dns(name, resolver or resolve_all)
        results.append(
            (True, f"ok  {name} is a DNS-only record ({len(addresses)} address(es), no proxy)")
        )
    except OperatorError as exc:
        results.append((False, f"FAIL {exc}"))
    results.append(check_port(name, port))
    try:
        check_tls(name, port, ca_pem, handshake or tls_handshake)
        print_ = (fingerprint or ca_fingerprint)(ca_pem)
        results.append(
            (True, f"ok  {name}:{port} presents a certificate from the relay CA (SHA-256 {print_})")
        )
    except OperatorError as exc:
        results.append((False, f"FAIL {exc}"))
    results.append(check_refuses_http(name, port))
    if mcp_url:
        results.append(check_mcp(mcp_url))
    return results
