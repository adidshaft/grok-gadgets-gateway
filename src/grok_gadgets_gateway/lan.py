"""Opt-in device listeners: a local CA and the TLS contexts for the LAN and relay listeners.

The LAN listener binds one private address (hub ADR 005). The relay listener binds any
address and serves a public DNS name (hub ADR 009). The CA key never leaves the config
directory. Devices pin `ca.pem` at provisioning time.
"""

import datetime
import hashlib
import ipaddress
import os
import re
import socket
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .operator import OperatorError, atomic_write, config_dir

DEFAULT_LAN_PORT = 8767
DEFAULT_DEVICE_PORT = 8443
# Home-network ranges only. Shared (100.64/10), link-local, loopback and public are refused.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
CA_DAYS = 3650
SERVER_DAYS = 825
CIPHERS = "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384"
# An mDNS name such as my-mac.local (hub ADR 006): DNS labels, ending in .local.
LOCAL_NAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+local$")


# A public DNS name such as devices.example.net: two or more labels, a letter-led last label.
DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def is_local_name(host):
    return bool(LOCAL_NAME.match(str(host)))


def is_dns_name(host):
    """A public DNS name: not an mDNS .local name and not an address."""
    host = str(host)
    return bool(DNS_NAME.match(host)) and not is_local_name(host)


def _port(text, label):
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise OperatorError(f"{label} port must be a number from 1 to 65535")
    return int(text)


def parse_device_listen(text, default_port=DEFAULT_DEVICE_PORT):
    """`ADDRESS[:PORT]` -> (address, port) for the relay listener: any IP address of this host.

    IPv6 goes in brackets, for example [::]:8443. A name is refused: the listener binds an
    address, and the certificate carries the name.
    """
    text = str(text)
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise OperatorError("Write an IPv6 listen address as [ADDRESS]:PORT")
        port_text = rest[1:]
        has_port = bool(rest)
    else:
        host, has_port, port_text = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
        has_port = bool(has_port)
    port = _port(port_text, "Device listener") if has_port else default_port
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise OperatorError(
            "--device-listen takes an IP address of this host, for example 0.0.0.0:8443 or "
            "[::]:8443. The certificate from device-cert carries the public name."
        ) from None
    return str(address), port


def parse_gateway_address(text, default_port=DEFAULT_DEVICE_PORT):
    """`NAME[:PORT]` -> (name, port): where a Wi-Fi device connects to the gateway.

    A public DNS name (the relay), an mDNS name or a private IPv4 address (the LAN listener).
    """
    host, sep, port_text = str(text).partition(":")
    host = host.lower()
    port = _port(port_text, "Gateway") if sep else default_port
    if is_dns_name(host) or is_local_name(host):
        return host, port
    try:
        ip = ipaddress.IPv4Address(host)
    except ValueError:
        raise OperatorError(
            "--gateway must be a DNS name such as devices.example.net, an mDNS name such as "
            "my-mac.local, or one IPv4 address"
        ) from None
    if not any(ip in n for n in PRIVATE_NETWORKS):
        raise OperatorError(
            f"{ip} is a public address. Give the relay's DNS name, so the device can check "
            "the certificate against it"
        )
    return str(ip), port


def _resolve(host):
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def resolve_lan_host(host, resolver=None):
    """An mDNS name -> this computer's current private IPv4 address; an address -> itself."""
    if not is_local_name(host):
        return host
    try:
        addresses = (resolver or _resolve)(host)
    except OSError:
        raise OperatorError(
            f"cannot resolve {host}. Use this computer's mDNS name (macOS: scutil --get "
            "LocalHostName, then add .local), or an IPv4 address"
        ) from None
    private = [address for address in addresses if is_private(address)]
    if not private:
        raise OperatorError(f"{host} has no private home-network address right now")
    return private[0]


def lan_dir():
    return config_dir() / "lan"


def is_private(address):
    """True for an IPv4 address inside the home-network ranges."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return isinstance(ip, ipaddress.IPv4Address) and any(ip in n for n in PRIVATE_NETWORKS)


def parse_lan_address(text, default_port=DEFAULT_LAN_PORT):
    """`ADDRESS[:PORT]` -> (address, port): one private IPv4 address, or an mDNS NAME.local."""
    host, sep, port_text = str(text).partition(":")
    port = _port(port_text, "LAN") if sep else default_port
    if is_local_name(host.lower()):
        return host.lower(), port
    try:
        ip = ipaddress.IPv4Address(host)
    except ValueError:
        raise OperatorError(
            "LAN address must be this computer's mDNS name (for example my-mac.local) or one "
            "IPv4 address of this computer (for example 192.168.1.20)"
        ) from None
    if not any(ip in n for n in PRIVATE_NETWORKS):
        raise OperatorError(
            f"{ip} is not a private home-network address (10/8, 172.16/12 or 192.168/16); "
            "the device listener never binds all, loopback, shared or public addresses"
        )
    return str(ip), port


def _paths(directory):
    directory = Path(directory) if directory else lan_dir()
    return (
        directory,
        directory / "ca.pem",
        directory / "ca.key",
        directory / "server.pem",
        directory / "server.key",
    )


def _device_paths(directory):
    directory = Path(directory) if directory else lan_dir()
    return directory / "device.pem", directory / "device.key"


def _key_bytes(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _load_ca(directory):
    _, ca_pem, ca_key, _, _ = _paths(directory)
    try:
        cert = x509.load_pem_x509_certificate(ca_pem.read_bytes())
        key = serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
    except FileNotFoundError:
        raise OperatorError("No local CA yet. Run: grok-gadgets-gateway init") from None
    except (OSError, ValueError):
        raise OperatorError(f"Local CA in {ca_pem.parent} cannot be read") from None
    return cert, key


def create_ca(directory=None, *, rotate=False, now=None):
    """Create the local CA if missing (or replace it). Returns the CA path or None if kept."""
    directory, ca_pem, ca_key, server_pem, server_key = _paths(directory)
    if ca_pem.exists() and ca_key.exists() and not rotate:
        return None
    now = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Grok Gadgets local"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Grok Gadgets local device CA"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    atomic_write(ca_key, _key_bytes(key))
    atomic_write(ca_pem, cert.public_bytes(serialization.Encoding.PEM))
    # A listener certificate from an old CA would never verify again.
    for stale in (server_pem, server_key, *_device_paths(directory)):
        if rotate and stale.exists():
            stale.unlink()
    return ca_pem


def _issue(directory, subject, names, pem_path, key_path, now=None):
    ca_cert, ca_key = _load_ca(directory)
    now = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        # mbedTLS 2.28 on the device matches the host string against CN and DNS names only.
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(names),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    atomic_write(key_path, _key_bytes(key))
    atomic_write(pem_path, cert.public_bytes(serialization.Encoding.PEM))
    return pem_path


def issue_server_cert(address, directory=None, *, now=None):
    """Issue the listener certificate for one private address or mDNS name, signed by the CA.

    A name certificate holds only the name, so it stays valid when the address changes.
    """
    address, _ = parse_lan_address(address)
    directory, _, _, server_pem, server_key = _paths(directory)
    names = (
        [x509.DNSName(address)]
        if is_local_name(address)
        else [x509.IPAddress(ipaddress.IPv4Address(address)), x509.DNSName(address)]
    )
    return _issue(directory, address, names, server_pem, server_key, now)


def issue_device_cert(name, directory=None, *, now=None):
    """Issue the relay listener certificate for one public DNS name (hub ADR 009).

    It sits beside the LAN certificate, so one gateway can run both listeners.
    """
    name = str(name).lower()
    if not is_dns_name(name):
        raise OperatorError(
            f"{name} is not a public DNS name. Use the relay's name, for example "
            "devices.example.net; for my-mac.local or a LAN address use lan-cert"
        )
    directory = Path(directory) if directory else lan_dir()
    pem, key = _device_paths(directory)
    return _issue(directory, name, [x509.DNSName(name)], pem, key, now)


def ca_pem_text(directory=None):
    _, ca_pem, _, _, _ = _paths(directory)
    try:
        return ca_pem.read_text(encoding="ascii")
    except FileNotFoundError:
        raise OperatorError("No local CA yet. Run: grok-gadgets-gateway init") from None


def read_ca_file(path):
    """A CA certificate from another gateway, such as a relay set up on its own host."""
    try:
        text = Path(path).read_text(encoding="ascii")
        cert = x509.load_pem_x509_certificate(text.encode())
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except FileNotFoundError:
        raise OperatorError(f"{path} does not exist") from None
    except (OSError, UnicodeError, ValueError, x509.ExtensionNotFound):
        raise OperatorError(f"{path} is not a PEM CA certificate") from None
    if not is_ca:
        raise OperatorError(f"{path} is a server certificate; give the relay's ca.pem")
    return text


def ca_fingerprint(directory=None):
    """SHA-256 of the CA certificate (DER), as colon-separated hex."""
    cert = x509.load_pem_x509_certificate(ca_pem_text(directory).encode())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def server_context(address, directory=None):
    """TLS 1.2+ server context for the listener; the certificate must name `address`."""
    directory, _, _, server_pem, server_key = _paths(directory)
    try:
        cert = x509.load_pem_x509_certificate(server_pem.read_bytes())
    except FileNotFoundError:
        raise OperatorError(
            f"No LAN certificate yet. Run: grok-gadgets-gateway lan-cert {address}"
        ) from None
    except (OSError, ValueError):
        raise OperatorError(f"LAN certificate in {directory} cannot be read") from None
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        named = [str(ip) for ip in names.get_values_for_type(x509.IPAddress)]
        if is_local_name(address):
            named = names.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        named = []
    if address not in named:
        raise OperatorError(
            f"The LAN certificate does not name {address}. "
            f"Run: grok-gadgets-gateway lan-cert {address}"
        )
    if os.stat(server_key).st_mode & 0o077:
        raise OperatorError("LAN key file must have mode 0600")
    return _tls_context(server_pem, server_key)


def _tls_context(pem, key):
    if os.stat(key).st_mode & 0o077:
        raise OperatorError(f"Key file {key.name} must have mode 0600")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(CIPHERS)
    context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_RENEGOTIATION
    context.verify_mode = ssl.CERT_NONE  # Devices authenticate with their enroll token.
    context.load_cert_chain(pem, key)
    return context


def device_cert_name(directory=None):
    """The DNS name in the relay listener certificate, or None if there is none yet."""
    pem, _ = _device_paths(directory)
    try:
        cert = x509.load_pem_x509_certificate(pem.read_bytes())
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return None
    values = names.get_values_for_type(x509.DNSName)
    return values[0] if values else None


def device_context(directory=None):
    """TLS context for the relay listener, from the certificate that device-cert wrote."""
    pem, key = _device_paths(directory)
    if not pem.is_file() or not key.is_file():
        raise OperatorError(
            "No relay certificate yet. Run: grok-gadgets-gateway device-cert devices.example.net "
            "(your relay's DNS name)"
        )
    try:
        return _tls_context(pem, key)
    except OperatorError:
        raise
    except (ssl.SSLError, ValueError):
        raise OperatorError(f"Relay certificate in {pem.parent} cannot be read") from None
