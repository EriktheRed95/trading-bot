"""SSRF-guarded download of a public media file.

Every rule is enforced before a socket is opened and again on every redirect hop:
HTTPS only, no credentials, port 443 only, a public DNS name (never an IP in any notation),
and every address the name resolves to must be globally routable. The connection is then
pinned to a validated address, so a resolver that answers differently a second time cannot
redirect the request to a private host. Redirects are followed by hand, at most three, and
each target is validated like the first URL. The body is streamed to disk with a hard size
cap, a content-type allowlist and a time limit, and removed again on any failure.

The transport and resolver are injectable so the rules can be tested without a network.
"""
import hashlib
import http.client
import ipaddress
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urljoin

from .urls import UrlRejected, public_https

MAX_REDIRECTS = 3
USER_AGENT = 'TradingResearchImport/1.0 (local; one file per approved request)'
DEFAULT_TYPES = ('video/mp4', 'video/quicktime', 'video/webm', 'video/x-matroska', 'video/x-m4v',
                 'image/jpeg', 'image/png', 'image/webp', 'application/octet-stream')


class FetchBlocked(Exception):
    """The fetch was refused by policy or failed. The message is written for the user."""

    def __init__(self, message, code='blocked'):
        super().__init__(message)
        self.code = code


def is_public_address(text):
    """True only for a globally routable unicast address. IPv4-mapped IPv6 is judged as IPv4."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None or ip.teredo is not None or ip in ipaddress.ip_network('64:ff9b::/96'):
            return False   # tunnelling forms can embed a private IPv4 address
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local
                                 or ip.is_private or ip.is_unspecified)


def default_resolver(host):
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a chosen IP address while still verifying the certificate for the host name."""

    def __init__(self, host, ip, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Response:
    def __init__(self, conn, resp):
        self._conn, self._resp = conn, resp
        self.status = resp.status
        self.headers = {k.lower(): v for k, v in resp.getheaders()}

    def read(self, n):
        return self._resp.read(n)

    def close(self):
        self._conn.close()


def pinned_transport(host, ip, target, timeout):
    """Default transport: one GET over HTTPS to `ip`, no automatic redirects or decompression."""
    conn = _Pinned(host, ip, timeout)
    try:
        conn.request('GET', target, headers={'Host': host, 'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity',
                                             'Accept': 'video/*,image/*,application/octet-stream;q=0.5'})
        return _Response(conn, conn.getresponse())
    except Exception:
        conn.close()
        raise


class SafeFetcher:
    def __init__(self, *, resolver=default_resolver, transport=pinned_transport, timeout=20, total_seconds=300,
                 clock=time.monotonic):
        self.resolver, self.transport, self.timeout = resolver, transport, timeout
        self.total_seconds, self.clock = total_seconds, clock

    def validate(self, raw):
        """Policy-check one URL, including each redirect target (syntax only); returns (host, request target).

        Content type and file signature decide whether the body is media, so a redirect target needs
        no file extension, but it must pass every host rule the first URL did.
        """
        try:
            return public_https(raw)
        except UrlRejected as exc:
            raise FetchBlocked(str(exc), 'url') from exc

    def resolve(self, host):
        try:
            addresses = list(self.resolver(host))
        except OSError as exc:
            raise FetchBlocked(f'The address {host} could not be resolved.', 'dns') from exc
        if not addresses:
            raise FetchBlocked(f'The address {host} did not resolve.', 'dns')
        for address in addresses:
            if not is_public_address(address):
                raise FetchBlocked('That address resolves to a non-public network location and was refused.', 'private_address')
        return addresses[0]

    def download(self, url, dest, *, max_bytes, allowed_types=DEFAULT_TYPES):
        """Fetch `url` into `dest`. Returns {url, bytes, sha256, content_type}. Raises FetchBlocked."""
        dest = Path(dest)
        started = self.clock()
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            host, target = self.validate(current)
            ip = self.resolve(host)
            try:
                response = self.transport(host, ip, target, self.timeout)
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise FetchBlocked('The file could not be downloaded from that address.', 'network') from exc
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location or hop == MAX_REDIRECTS:
                        raise FetchBlocked('The address redirected too many times or without a destination.', 'redirect')
                    current = urljoin(f'https://{host}{target}', location)
                    continue
                if response.status != 200:
                    raise FetchBlocked(f'The address answered with status {response.status}.', 'status')
                ctype = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if ctype not in allowed_types:
                    raise FetchBlocked('The address did not return a supported video or image file.', 'content_type')
                declared = response.headers.get('content-length', '')
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchBlocked(f'The file is larger than the {max_bytes // (1024 * 1024)} MB limit.', 'too_large')
                return self._stream(response, dest, url=current, ctype=ctype, max_bytes=max_bytes, started=started)
            finally:
                response.close()
        raise FetchBlocked('The address redirected too many times.', 'redirect')

    def _stream(self, response, dest, *, url, ctype, max_bytes, started):
        digest, size = hashlib.sha256(), 0
        try:
            with open(dest, 'wb') as out:
                while True:
                    if self.clock() - started > self.total_seconds:
                        raise FetchBlocked('The download took too long and was stopped.', 'timeout')
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchBlocked(f'The file is larger than the {max_bytes // (1024 * 1024)} MB limit.', 'too_large')
                    digest.update(chunk)
                    out.write(chunk)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            dest.unlink(missing_ok=True)
            raise FetchBlocked('The download was interrupted.', 'network') from exc
        except FetchBlocked:
            dest.unlink(missing_ok=True)
            raise
        if size == 0:
            dest.unlink(missing_ok=True)
            raise FetchBlocked('The address returned an empty file.', 'empty')
        return {'url': url, 'bytes': size, 'sha256': digest.hexdigest(), 'content_type': ctype}
