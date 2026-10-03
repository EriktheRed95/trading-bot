"""Imported trading research: link and file safety, consent, schema, dedup, persistence, separation.

No test calls Gemini, yt-dlp or any real network address. The reader is a fake, the resolver and
transport of the downloader are fakes, servers bind an ephemeral loopback port, and every store
and paper database is a temporary one.
"""
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from paper_book import PaperBook
from research_import import files, routes, safe_fetch, schema, urls
from research_import.process import child_environment, run_bounded
from research_import.reader import GeminiSkillReader, Media, ReaderUnavailable, ReadFailure, ReadOutput
from research_import.service import ResearchImports
from research_import.store import ResearchStore, StoreError, fingerprint
from trading_app import Controller, env_port, make_server

ROOT = Path(__file__).resolve().parent
MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 40
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
SECRET = 'AIzaSyFAKE-KEY-FOR-TESTS-0123456789'
GOOD = {
    'is_trading_research': True, 'title': 'Opening range breakout', 'creator': {'name': 'Some Trader', 'handle': '@sometrader'},
    'summary': 'Trades the first 15 minutes of the session.',
    'strategy': {'name': 'ORB', 'type': 'breakout', 'description': 'Buy a break of the opening range.'},
    'instruments': ['SPY', 'QQQ'], 'timeframes': ['5 minute'],
    'entry_rules': [{'text': 'Buy above the 15 minute high', 'at': '01:10'}], 'exit_rules': ['Sell at the close'],
    'risk_rules': ['Stop under the range low'],
    'stated_numbers': [{'label': 'Range window', 'value': 15, 'unit': 'minutes'}],
    'claimed_returns': [{'claim': 'Made 300% last year', 'value': '300%', 'period': 'last year', 'basis': 'screenshot'}],
    'evidence_shown': 'screenshot_of_results', 'missing_details': ['Position size', 'Costs'], 'commercial_disclosures': ['Sells a course'],
    'validated': True, 'recommendation': 'BUY NOW', 'place_order': {'symbol': 'SPY'},
}


class FakeReader:
    """Stands in for the Gemini reader. Counts every read so tests can prove nothing was sent early."""

    def __init__(self, reply=None, configured=True, error=None):
        self.reply, self.configured, self.error, self.calls, self.gate = reply if reply is not None else GOOD, configured, error, [], None

    def capabilities(self):
        return {'configured': self.configured, 'provider': 'Google Gemini', 'api_key_present': self.configured,
                'missing': [] if self.configured else ['a Gemini API key in the server environment'], 'platforms': {}}

    def require(self, media):
        if not self.configured:
            raise ReaderUnavailable('Media reading is not set up on this server: missing a Gemini API key in the server environment.')

    def read(self, media, workdir):
        self.calls.append(media)
        assert Path(workdir).is_dir()
        (Path(workdir) / 'scratch.bin').write_bytes(b'x' * 10)
        if self.gate:
            self.gate.wait(10)
        if self.error:
            raise self.error
        return ReadOutput(self.reply if isinstance(self.reply, str) else json.dumps(self.reply), 'video')


class FakeFetcher:
    def __init__(self, body=MP4):
        self.body, self.calls = body, []

    def download(self, url, dest, *, max_bytes, allowed_types=None):
        self.calls.append(url)
        Path(dest).write_bytes(self.body)
        return {'url': url, 'bytes': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest(), 'content_type': 'video/mp4'}


def make_service(tmp, reader=None, fetcher=None, **kw):
    return ResearchImports(Path(tmp) / 'research-imports', reader=reader or FakeReader(), fetcher=fetcher or FakeFetcher(), **kw)


def upload(service, data=MP4, name='clip.mp4', ctype='video/mp4'):
    return service.add_upload(io.BytesIO(data), len(data), name, ctype)


def approve(service, item):
    return service.approve({'id': item['id'], 'acknowledged': True, 'fingerprint': item['consent']['fingerprint'],
                            'notice_version': item['consent']['notice_version']})


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


# ============================================================ links


class LinkRules(unittest.TestCase):
    def test_platform_links_reduce_to_one_canonical_key(self):
        same = ['https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=abc',
                'https://youtu.be/dQw4w9WgXcQ?si=xyz', 'https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123',
                'https://www.youtube.com/shorts/dQw4w9WgXcQ']
        self.assertEqual({urls.classify(u).key for u in same}, {'youtube:dQw4w9WgXcQ'})
        self.assertEqual({urls.classify(u).url for u in same}, {'https://www.youtube.com/watch?v=dQw4w9WgXcQ'})
        ig = [urls.classify(u) for u in ('https://www.instagram.com/p/CxYz_123/', 'https://instagram.com/reel/CxYz_123/?igsh=abc',
                                        'https://www.instagram.com/somebody/p/CxYz_123/', 'https://www.instagram.com/reels/CxYz_123')]
        self.assertEqual({s.key for s in ig}, {'instagram:CxYz_123'})
        tt = urls.classify('https://tiktok.com/@user.name/video/7312345678901234567?lang=en')
        self.assertEqual((tt.key, tt.platform), ('tiktok:7312345678901234567', 'tiktok'))
        self.assertEqual(urls.classify('https://vm.tiktok.com/ZMabc123/').key, 'tiktok-short:ZMabc123')

    def test_direct_media_links_need_a_public_host_and_a_media_extension(self):
        a = urls.classify('https://cdn.example.com/clips/demo.MP4?sig=1')
        self.assertEqual((a.platform, a.kind), ('direct', 'media'))
        self.assertEqual(a.key, urls.classify('https://cdn.example.com/clips/demo.MP4?sig=1').key)
        self.assertNotEqual(a.key, urls.classify('https://cdn.example.com/clips/other.mp4').key)
        for bad in ('https://cdn.example.com/page.html', 'https://cdn.example.com/', 'https://cdn.example.com/a.exe'):
            with self.assertRaises(urls.UrlRejected, msg=bad):
                urls.classify(bad)

    def test_hostile_and_unsupported_links_are_refused_before_any_request(self):
        bad = [
            'http://www.youtube.com/watch?v=dQw4w9WgXcQ', 'ftp://example.com/a.mp4', 'file:///C:/Windows/win.ini',
            'javascript:alert(1)', 'data:text/html,hi', '//example.com/a.mp4', 'example.com/a.mp4',
            'https://127.0.0.1/a.mp4', 'https://localhost/a.mp4', 'https://[::1]/a.mp4', 'https://2130706433/a.mp4',
            'https://0x7f.0.0.1/a.mp4', 'https://0177.0.0.1/a.mp4', 'https://169.254.169.254/latest/meta-data/a.mp4',
            'https://10.0.0.5/a.mp4', 'https://internal.corp/a.mp4', 'https://printer.local/a.mp4', 'https://host.internal/a.mp4',
            'https://user:pw@cdn.example.com/a.mp4', 'https://www.youtube.com@evil.example/watch?v=dQw4w9WgXcQ',
            'https://evil.example\\@www.youtube.com/watch?v=dQw4w9WgXcQ', 'https://cdn.example.com:8443/a.mp4',
            'https://cdn.example.com:80/a.mp4', 'https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ',
            'https://evil.example/www.youtube.com/watch?v=dQw4w9WgXcQ', 'https://notyoutube.com/watch?v=dQw4w9WgXcQ',
            'https://www.youtube.com/watch?v=short', 'https://www.youtube.com/playlist?list=PL1234567890',
            'https://www.youtube.com/@channel', 'https://www.instagram.com/stories/user/123/', 'https://www.instagram.com/someuser/',
            'https://www.tiktok.com/@user/photo/7312345678901234567', 'https://www.tiktok.com/@user',
            'https://cdn.example.com/a b.mp4', 'https://cdn.example.com/a\x00.mp4', 'https://cdn.exämple.com/a.mp4',
            'https://' + 'a' * 300 + '.com/a.mp4', 'https://example.com/' + 'a' * 2100 + '.mp4', '', '   ', None, 42,
        ]
        for raw in bad:
            with self.assertRaises(urls.UrlRejected, msg=repr(raw)[:80]):
                urls.classify(raw)

    def test_public_hostname_rejects_every_ip_spelling(self):
        for host in ('127.0.0.1', '2130706433', '0x7f000001', '017700000001', '::1', '1.2.3', 'localhost', 'a.local', 'x.internal', 'intranet'):
            self.assertFalse(urls.public_hostname(host), host)
        self.assertTrue(urls.public_hostname('cdn.example.com'))

    def test_message_names_what_is_supported(self):
        with self.assertRaises(urls.UrlRejected) as caught:
            urls.classify('https://cdn.example.com/page.html')
        self.assertIn('YouTube', str(caught.exception))


# ============================================================ SSRF-guarded fetch


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b'', chunks=None):
        self.status, self.headers = status, {k.lower(): v for k, v in (headers or {}).items()}
        self.chunks = list(chunks) if chunks is not None else ([body] if body else [])
        self.closed = False

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b''

    def close(self):
        self.closed = True


class Net:
    """A resolver and transport that record what the fetcher asked for and never open a socket."""

    def __init__(self, dns, pages):
        self.dns, self.pages, self.resolved, self.requests = dns, pages, [], []

    def resolver(self, host):
        self.resolved.append(host)
        answer = self.dns(host, len(self.resolved)) if callable(self.dns) else self.dns[host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def transport(self, host, ip, target, timeout):
        self.requests.append((host, ip, target))
        return self.pages[(host, target)]


def fetcher(dns, pages, **kw):
    net = Net(dns, pages)
    return safe_fetch.SafeFetcher(resolver=net.resolver, transport=net.transport, **kw), net


class SafeFetchRules(Tmp):
    OK = {'Content-Type': 'video/mp4'}

    def test_private_and_special_addresses_are_never_public(self):
        for ip in ('127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254', '100.64.0.1', '0.0.0.0', '224.0.0.1',
                   '240.0.0.1', '::1', '::', 'fe80::1', 'fc00::1', 'fd12::1', 'ff02::1', '::ffff:127.0.0.1', '::ffff:10.0.0.1',
                   '64:ff9b::7f00:1', '2002:7f00:1::1', '2001:db8::1', 'not-an-ip', ''):
            self.assertFalse(safe_fetch.is_public_address(ip), ip)
        for ip in ('93.184.216.34', '8.8.8.8', '2606:4700:4700::1111'):
            self.assertTrue(safe_fetch.is_public_address(ip), ip)

    def test_successful_download_pins_the_validated_address(self):
        body = MP4 + b'rest'
        f, net = fetcher({'cdn.example.com': ['93.184.216.34']},
                         {('cdn.example.com', '/a.mp4?x=1'): FakeResponse(200, self.OK, chunks=[body[:20], body[20:]])})
        out = f.download('https://cdn.example.com/a.mp4?x=1', self.dir / 'a.mp4', max_bytes=1000)
        self.assertEqual((out['bytes'], out['sha256']), (len(body), hashlib.sha256(body).hexdigest()))
        self.assertEqual(net.requests, [('cdn.example.com', '93.184.216.34', '/a.mp4?x=1')])   # connects to the IP it validated
        self.assertEqual((self.dir / 'a.mp4').read_bytes(), body)

    def test_a_name_with_any_private_answer_is_refused_before_connecting(self):
        for answers in (['10.0.0.8'], ['93.184.216.34', '127.0.0.1'], ['::1'], ['::ffff:192.168.0.1'], ['169.254.169.254']):
            f, net = fetcher({'cdn.example.com': answers}, {})
            with self.assertRaises(safe_fetch.FetchBlocked) as caught:
                f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
            self.assertEqual(caught.exception.code, 'private_address', answers)
            self.assertEqual(net.requests, [])
            self.assertFalse((self.dir / 'a.mp4').exists())

    def test_dns_rebinding_cannot_swap_the_connection_target(self):
        # First answer public, every later answer private. One resolution feeds one pinned connection.
        f, net = fetcher(lambda host, n: ['93.184.216.34'] if n == 1 else ['127.0.0.1'],
                         {('cdn.example.com', '/a.mp4'): FakeResponse(200, self.OK, body=MP4)})
        f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
        self.assertEqual(net.resolved, ['cdn.example.com'])
        self.assertEqual(net.requests[0][1], '93.184.216.34')

    def test_unresolvable_name_is_a_clean_refusal(self):
        f, _ = fetcher({'cdn.example.com': OSError('no such host')}, {})
        with self.assertRaises(safe_fetch.FetchBlocked) as caught:
            f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
        self.assertEqual(caught.exception.code, 'dns')

    def test_every_redirect_hop_is_revalidated(self):
        for target, code in (('https://127.0.0.1/a.mp4', 'url'), ('http://cdn.example.com/a.mp4', 'url'),
                             ('https://evil.example:8443/a.mp4', 'url'), ('https://user@cdn.example.com/a.mp4', 'url'),
                             ('//10.0.0.1/a.mp4', 'url'), ('https://internal.example/a.mp4', 'private_address')):
            dns = {'cdn.example.com': ['93.184.216.34'], 'internal.example': ['10.9.9.9']}
            f, net = fetcher(dns, {('cdn.example.com', '/a.mp4'): FakeResponse(302, {'Location': target})})
            with self.assertRaises(safe_fetch.FetchBlocked) as caught:
                f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
            self.assertEqual(caught.exception.code, code, target)
            self.assertEqual(len(net.requests), 1, target)   # the forbidden hop was never requested

    def test_redirects_are_followed_a_few_times_with_relative_locations(self):
        pages = {('cdn.example.com', '/a.mp4'): FakeResponse(301, {'Location': '/b.mp4'}),
                 ('cdn.example.com', '/b.mp4'): FakeResponse(307, {'Location': 'https://files.example.org/c'}),
                 ('files.example.org', '/c'): FakeResponse(200, self.OK, body=MP4)}
        f, net = fetcher({'cdn.example.com': ['93.184.216.34'], 'files.example.org': ['93.184.216.35']}, pages)
        out = f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
        self.assertEqual(out['url'], 'https://files.example.org/c')
        self.assertEqual([r[0] for r in net.requests], ['cdn.example.com', 'cdn.example.com', 'files.example.org'])

    def test_redirect_loops_are_cut_off(self):
        pages = {('cdn.example.com', f'/{i}'): FakeResponse(302, {'Location': f'/{i + 1}'}) for i in range(10)}
        pages[('cdn.example.com', '/a.mp4')] = FakeResponse(302, {'Location': '/1'})
        f, net = fetcher({'cdn.example.com': ['93.184.216.34']}, pages)
        with self.assertRaises(safe_fetch.FetchBlocked) as caught:
            f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=1000)
        self.assertEqual(caught.exception.code, 'redirect')
        self.assertLessEqual(len(net.requests), safe_fetch.MAX_REDIRECTS + 1)

    def test_content_type_status_size_and_time_limits(self):
        def run(response, **kw):
            f, _ = fetcher({'cdn.example.com': ['93.184.216.34']}, {('cdn.example.com', '/a.mp4'): response}, **kw)
            with self.assertRaises(safe_fetch.FetchBlocked) as caught:
                f.download('https://cdn.example.com/a.mp4', self.dir / 'a.mp4', max_bytes=100)
            self.assertFalse((self.dir / 'a.mp4').exists(), 'a partial file must not survive a refusal')
            return caught.exception.code
        self.assertEqual(run(FakeResponse(200, {'Content-Type': 'text/html'}, body=b'<html>')), 'content_type')
        self.assertEqual(run(FakeResponse(404, self.OK, body=b'x')), 'status')
        self.assertEqual(run(FakeResponse(200, {**self.OK, 'Content-Length': '5000'}, body=b'x')), 'too_large')
        self.assertEqual(run(FakeResponse(200, self.OK, chunks=[b'x' * 60, b'x' * 60])), 'too_large')    # liar without Content-Length
        self.assertEqual(run(FakeResponse(200, self.OK, body=b'')), 'empty')
        ticks = iter(range(0, 1000, 100))
        self.assertEqual(run(FakeResponse(200, self.OK, chunks=[b'x'] * 5), total_seconds=150, clock=lambda: next(ticks)), 'timeout')

    def test_platform_hosts_and_bare_ips_cannot_be_the_first_url_either(self):
        f, net = fetcher({}, {})
        for url in ('https://127.0.0.1/a.mp4', 'http://cdn.example.com/a.mp4', 'https://localhost/a.mp4'):
            with self.assertRaises(safe_fetch.FetchBlocked):
                f.download(url, self.dir / 'x', max_bytes=10)
        self.assertEqual((net.resolved, net.requests), ([], []))


# ============================================================ uploads and staging


class UploadRules(Tmp):
    def test_type_size_and_signature_must_agree(self):
        for name, ctype, data, ok in (('a.mp4', 'video/mp4', MP4, True), ('a.MOV', 'video/quicktime', b'\x00\x00\x00\x08wide' + b'\0' * 20, True),
                                      ('a.png', 'image/png', PNG, True), ('a.jpg', 'image/jpeg', b'\xff\xd8\xff\xe0' + b'\0' * 30, True),
                                      ('a.webp', 'image/webp', b'RIFF\0\0\0\0WEBP' + b'\0' * 20, True),
                                      ('a.webm', 'video/webm', b'\x1a\x45\xdf\xa3' + b'\0' * 8 + b'webm' + b'\0' * 20, True),
                                      ('a.mkv', 'video/x-matroska', b'\x1a\x45\xdf\xa3' + b'\0' * 8 + b'matroska' + b'\0' * 20, True),
                                      ('a.mp4', 'application/octet-stream', MP4, True),
                                      ('a.mp4', 'video/mp4', PNG, False), ('a.png', 'image/png', MP4, False),
                                      ('a.webm', 'video/webm', b'\x1a\x45\xdf\xa3' + b'\0' * 8 + b'matroska', False),
                                      ('a.mp4', 'text/plain', MP4, False), ('a.mp4', 'image/png', MP4, False),
                                      ('a.exe', 'application/octet-stream', MP4, False), ('a.mp4.exe', 'video/mp4', MP4, False),
                                      ('noextension', 'video/mp4', MP4, False), ('a.svg', 'image/svg+xml', b'<svg/>', False),
                                      ('a.html', 'text/html', b'<html>', False), ('a.gif', 'image/gif', b'GIF89a', False)):
            staging = files.Staging(self.dir / 'staging')
            try:
                ext, _, _, _ = files.classify_name(name, ctype)
                staging.receive(io.BytesIO(data), len(data), ext)
                accepted = True
            except files.UploadRejected:
                accepted = False
            self.assertEqual(accepted, ok, (name, ctype))

    def test_filename_is_display_only_and_never_a_path(self):
        for raw in ('..\\..\\Windows\\evil.mp4', '../../etc/passwd.mp4', 'C%3A%5Cx%5C..%5Cy.mp4', 'a\x00b\r\n.mp4', 'con:<x>|?.mp4', '.' * 5 + 'a.mp4'):
            name = files.safe_display_name(raw)
            self.assertNotIn('/', name), self.assertNotIn('\\', name)
            self.assertTrue(all(ord(c) >= 32 for c in name))
            self.assertLessEqual(len(name), 120)
        svc = make_service(self.dir)
        item = upload(svc, name='..\\..\\Windows\\evil.mp4')['item']
        self.assertEqual(item['filename'], 'evil.mp4')
        staged = list((self.dir / 'research-imports' / 'staging').iterdir())
        self.assertEqual(len(staged), 1)
        self.assertRegex(staged[0].name, r'^[0-9a-f]{32}\.mp4$')        # name comes from a uuid, not the upload

    def test_short_or_broken_body_leaves_nothing_behind(self):
        staging = files.Staging(self.dir / 'staging')
        with self.assertRaises(files.UploadRejected):
            staging.receive(io.BytesIO(MP4[:10]), len(MP4), '.mp4')
        with self.assertRaises(files.UploadRejected):
            staging.receive(io.BytesIO(PNG), len(PNG), '.mp4')
        self.assertEqual(list((self.dir / 'staging').iterdir()), [])

    def test_a_trickling_upload_is_stopped_at_a_deadline_and_leaves_nothing(self):
        staging = files.Staging(self.dir / 'staging')
        ticks = iter(range(0, 10_000, 400))

        class Trickle(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 10))
        with self.assertRaises(files.UploadRejected) as caught:
            staging.receive(Trickle(MP4 * 4), len(MP4 * 4), '.mp4', clock=lambda: next(ticks))
        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(list((self.dir / 'staging').iterdir()), [])

    def test_concurrent_identical_uploads_keep_one_item_and_one_file(self):
        svc = make_service(self.dir)
        results, errors = [], []

        def send():
            try:
                results.append(upload(svc))
            except Exception as exc:   # pragma: no cover - reported by the assertion below
                errors.append(exc)
        threads = [threading.Thread(target=send) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r['duplicate'] for r in results), [False] + [True] * 5)
        self.assertEqual(len({r['item']['id'] for r in results}), 1)
        self.assertEqual(svc.staging.usage()[0], 1)

    def test_length_limits_are_enforced_before_reading_the_body(self):
        svc = make_service(self.dir)

        class Explodes:
            def read(self, n):
                raise AssertionError('the body must not be read for an over-limit upload')
        with self.assertRaises(StoreError) as caught:
            svc.add_upload(Explodes(), files.MAX_VIDEO_BYTES + 1, 'a.mp4', 'video/mp4')
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(StoreError) as caught:
            svc.add_upload(Explodes(), files.MAX_IMAGE_BYTES + 1, 'a.png', 'image/png')
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(StoreError) as caught:
            svc.add_upload(io.BytesIO(b''), 0, 'a.mp4', 'video/mp4')
        self.assertEqual(caught.exception.status, 411)
        with self.assertRaises(StoreError) as caught:
            svc.add_upload(io.BytesIO(b''), None, 'a.mp4', 'video/mp4')
        self.assertEqual(caught.exception.status, 411)

    def test_staging_area_is_bounded(self):
        svc = make_service(self.dir)
        for i in range(files.MAX_STAGED_FILES):
            upload(svc, MP4 + bytes([i]), f'c{i}.mp4')
        with self.assertRaises(StoreError) as caught:
            upload(svc, MP4 + b'extra', 'one-too-many.mp4')
        self.assertEqual(caught.exception.status, 409)
        with patch.object(files, 'MAX_STAGED_BYTES', 10):
            fresh = make_service(self.dir / 'other')
            with self.assertRaises(StoreError):
                upload(fresh)

    def test_orphans_and_expired_files_are_swept(self):
        clock = [datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)]
        svc = make_service(self.dir, clock=lambda: clock[0])
        item = upload(svc)['item']
        staged = Path(svc.store.get(item['id'])['staged_path'])
        orphan = staged.parent / ('f' * 32 + '.mp4')
        orphan.write_bytes(MP4)
        os.utime(orphan, (time.time() - 7200, time.time() - 7200))
        fresh_orphan = staged.parent / ('e' * 32 + '.part')
        fresh_orphan.write_bytes(b'x')
        svc.housekeeping()
        self.assertFalse(orphan.exists())
        self.assertTrue(fresh_orphan.exists(), 'a recent .part may belong to an upload in progress')
        self.assertTrue(staged.exists())
        clock[0] += timedelta(seconds=files.STAGING_TTL_SECONDS + 1)
        svc.housekeeping()
        self.assertFalse(staged.exists())
        self.assertEqual(svc.item(item['id'])['status'], 'expired')


# ============================================================ schema


class SchemaRules(unittest.TestCase):
    def test_reading_is_normalised_and_claims_are_labelled_as_source_claims(self):
        reading = schema.validate_reading(GOOD)
        self.assertIs(reading['validated'], False)
        self.assertEqual(reading['creator'], {'name': 'Some Trader', 'handle': 'sometrader'})
        self.assertEqual(reading['entry_rules'], [{'text': 'Buy above the 15 minute high', 'at': '01:10'}])
        self.assertEqual(reading['exit_rules'], [{'text': 'Sell at the close', 'at': ''}])
        self.assertEqual(reading['stated_numbers'][0]['value'], '15')              # numbers are kept as stated text
        claim = reading['claimed_returns'][0]
        self.assertTrue(claim['source_claim'])
        self.assertEqual(claim['label'], 'Source claim, unverified')
        self.assertEqual(claim['basis'], 'screenshot')
        for forbidden in ('recommendation', 'place_order', 'action', 'verified', 'signal'):
            self.assertNotIn(forbidden, reading)

    def test_reader_cannot_assert_validation_or_inject_fields(self):
        reading = schema.validate_reading({**GOOD, 'validated': True, 'evidence_tier': 'VALIDATED', 'verified': True})
        self.assertIs(reading['validated'], False)
        self.assertNotIn('evidence_tier', reading)
        self.assertEqual(set(reading), {'title', 'creator', 'summary', 'strategy', 'instruments', 'timeframes', 'entry_rules', 'exit_rules',
                                        'risk_rules', 'stated_numbers', 'claimed_returns', 'evidence_shown', 'missing_details',
                                        'commercial_disclosures', 'validated'})

    def test_untrusted_types_are_coerced_or_dropped_never_trusted(self):
        hostile = {'is_trading_research': True, 'title': ['x'], 'summary': {'a': 1}, 'strategy': 'trend',
                   'instruments': 'SPY', 'entry_rules': [1, None, {'nope': 1}, {'text': '<script>alert(1)</script>'}, ['x']],
                   'exit_rules': 'sell', 'claimed_returns': [5, {'claim': ''}, {'claim': 'x' * 5000, 'basis': 'made up'}],
                   'stated_numbers': [{'label': 'a'}, {'value': '1'}, {'label': 'rsi', 'value': float('nan')}],
                   'evidence_shown': 'ultra', 'missing_details': [None, 7, 'real'], 'risk_rules': {}}
        reading = schema.validate_reading(hostile)
        self.assertEqual(reading['title'], '')
        self.assertEqual(reading['strategy'], {'name': '', 'type': 'unclear', 'description': ''})
        self.assertEqual(reading['instruments'], [])
        self.assertEqual([r['text'] for r in reading['entry_rules']], ['1', '<script>alert(1)</script>'])   # kept as inert text
        self.assertEqual(reading['exit_rules'], [])
        self.assertEqual(len(reading['claimed_returns'][0]['claim']), 300)
        self.assertEqual(reading['claimed_returns'][0]['basis'], 'unspecified')
        self.assertEqual(reading['stated_numbers'], [])
        self.assertEqual(reading['evidence_shown'], 'none')
        self.assertEqual(reading['missing_details'], ['7', 'real'])

    def test_lists_and_strings_are_capped(self):
        big = {**GOOD, 'instruments': [f'T{i}' for i in range(500)], 'entry_rules': ['r' * 9000] * 500, 'summary': 's' * 99999,
               'stated_numbers': [{'label': 'l', 'value': str(i)} for i in range(500)], 'claimed_returns': [{'claim': 'c'}] * 500}
        reading = schema.validate_reading(big)
        self.assertEqual(len(reading['instruments']), 30)
        self.assertEqual((len(reading['entry_rules']), len(reading['entry_rules'][0]['text'])), (40, 500))
        self.assertEqual(len(reading['summary']), 1500)
        self.assertEqual((len(reading['stated_numbers']), len(reading['claimed_returns'])), (50, 20))
        self.assertLess(len(json.dumps(reading)), schema.MAX_RESULT_BYTES)

    def test_control_characters_are_stripped(self):
        reading = schema.validate_reading({**GOOD, 'title': 'a\x00b\x07c\x1bd\u0085'})
        self.assertNotRegex(reading['title'], r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

    def test_not_research_and_malformed_answers(self):
        self.assertIsNone(schema.validate_reading({'is_trading_research': False, 'strategy': {'description': 'x'}}))
        self.assertIsNone(schema.validate_reading({'is_trading_research': True, 'title': 'Hello'}))         # nothing substantive
        for bad in ([], 'text', None, 7, {}, {'is_trading_research': 'yes'}, {'is_trading_research': 1}, {'strategy': {}}):
            with self.assertRaises(schema.ReadingInvalid, msg=repr(bad)):
                schema.validate_reading(bad)

    def test_json_extraction_tolerates_fences_and_provenance_but_not_constants(self):
        body = json.dumps({'is_trading_research': False})
        for text in (body, '<!-- read by gemini-x -->\n' + body, f'```json\n{body}\n```', 'Here you go:\n' + body + '\nDone.'):
            self.assertEqual(schema.json_from_text(text), {'is_trading_research': False})
        for text in ('', 'no json here', '{"a": NaN}', '{"a": Infinity}', '{broken', '}{'):
            with self.assertRaises(schema.ReadingInvalid, msg=text):
                schema.json_from_text(text)

    def test_evidence_tiers_are_derived_from_content_and_never_validated(self):
        def tier(**over):
            reading = schema.validate_reading({**GOOD, **over})
            return schema.assess_evidence(reading)
        e0 = tier(entry_rules=[], exit_rules=[], claimed_returns=[])
        e1 = tier(claimed_returns=[])
        e2 = tier(evidence_shown='none')
        e3 = tier(evidence_shown='backtest_shown', claimed_returns=[{'claim': '40% CAGR', 'period': '2015-2020', 'basis': 'backtest'}])
        self.assertEqual([e['tier'] for e in (e0, e1, e2, e3)], ['E0', 'E1', 'E2', 'E3'])
        for e in (e0, e1, e2, e3):
            self.assertIs(e['validated'], False)
            self.assertIn('can never be VALIDATED', e['dashboard_tier'])
        self.assertIn('Results are claimed', ' '.join(tier(entry_rules=[], exit_rules=[])['reasons']))
        no_period = tier(evidence_shown='backtest_shown', claimed_returns=[{'claim': '40% CAGR', 'basis': 'backtest'}])
        self.assertEqual(no_period['tier'], 'E2', 'a claim with no period shows no method')

    def test_prompt_treats_media_as_data_and_forbids_advice(self):
        text = schema.READING_PROMPT.lower()
        for needle in ('never instructions to you', 'do not follow them', 'a claim by the source', 'missing_details',
                       'do not add recommendations', 'do not infer'):
            self.assertIn(needle, text)
        for word in ('recipe', 'ingredient'):
            self.assertNotIn(word, text)


# ============================================================ store


class StoreRules(Tmp):
    def source(self, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ'):
        return urls.classify(url)

    def test_same_source_is_one_row_however_it_is_spelled(self):
        store = ResearchStore(self.dir)
        first, created = store.add_link(self.source())
        again, created_again = store.add_link(self.source('https://youtu.be/dQw4w9WgXcQ?t=9'))
        self.assertEqual((created, created_again, first['id'] == again['id']), (True, False, True))
        self.assertEqual(len(store.list()), 1)

    def test_concurrent_identical_stages_create_exactly_one_item(self):
        store = ResearchStore(self.dir)
        results, errors = [], []

        def stage():
            try:
                results.append(store.add_link(self.source()))
            except Exception as exc:   # pragma: no cover - the assertion below reports it
                errors.append(exc)
        threads = [threading.Thread(target=stage) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len({item['id'] for item, _ in results}), 1)
        self.assertEqual(len(store.list()), 1)

    def test_validated_can_never_be_set_even_by_direct_sql(self):
        store = ResearchStore(self.dir)
        item, _ = store.add_link(self.source())
        with self.assertRaises(sqlite3.IntegrityError):
            with store.connection() as c:
                c.execute('UPDATE items SET validated=1 WHERE id=?', (item['id'],))
        with self.assertRaises(StoreError):
            store.set_review(item['id'], 'validated', 'x')

    def test_review_state_and_note_persist_and_are_bounded(self):
        store = ResearchStore(self.dir)
        item, _ = store.add_link(self.source())
        store.set_review(item['id'], 'shortlisted', 'try on SPY first')
        self.assertEqual((store.get(item['id'])['review_state'], store.get(item['id'])['review_note']), ('shortlisted', 'try on SPY first'))
        store.set_review(item['id'], 'reviewed', None)
        self.assertEqual(store.get(item['id'])['review_note'], 'try on SPY first')        # a missing note keeps the old one
        for bad_state, bad_note in (('bogus', 'x'), ('new', 'x' * 2001), ('new', ['x'])):
            with self.assertRaises(StoreError):
                store.set_review(item['id'], bad_state, bad_note)

    def test_search_requires_every_term_and_escapes_wildcards(self):
        store = ResearchStore(self.dir)
        a, _ = store.add_link(self.source('https://www.youtube.com/watch?v=aaaaaaaaaaa'))
        b, _ = store.add_link(self.source('https://www.youtube.com/watch?v=bbbbbbbbbbb'))
        store.begin_processing(a['id'], fingerprint(a))
        reading = schema.validate_reading(GOOD)
        store.finish(a['id'], 'done', 'ok', reading=reading, result={'reading': reading}, retryable=False)
        found = lambda q, **kw: [i['id'] for i in store.list(q, **kw)]
        self.assertEqual(found('breakout spy'), [a['id']])
        self.assertEqual(found('BREAKOUT   spy'), [a['id']])
        self.assertEqual(found('breakout tsla'), [])
        self.assertEqual(found('300%'), [a['id']])
        self.assertEqual(found('%'), [a['id']])       # a literal percent sign, not a wildcard for everything
        self.assertEqual(found('%'), found('300%'))
        self.assertEqual(found('_'), [])
        self.assertEqual(found('aaaaaaaaaaa'), [a['id']])
        self.assertEqual(found('', status='pending_consent'), [b['id']])
        self.assertEqual(found('', status='done'), [a['id']])
        with self.assertRaises(StoreError):
            store.list(status='nonsense')
        with self.assertRaises(StoreError):
            store.list(review='nonsense')

    def test_records_persist_across_a_restart_and_processing_is_recovered(self):
        store = ResearchStore(self.dir)
        link, _ = store.add_link(self.source())
        store.begin_processing(link['id'], fingerprint(link))
        self.assertEqual(ResearchStore(self.dir).get(link['id'])['status'], 'processing')
        staged = self.dir / 'staging'
        staged.mkdir()
        video = staged / ('a' * 32 + '.mp4')
        video.write_bytes(MP4)
        up, _ = store.add_upload('a' * 32, key='file:abc', filename='v.mp4', media_type='video/mp4', size=len(MP4), sha256='abc',
                                 staged_path=video, expires_at='2999-01-01T00:00:00+00:00')
        store.begin_processing(up['id'], fingerprint(up))
        reopened = ResearchStore(self.dir)
        stale = reopened.recover()
        self.assertEqual(stale, [str(video)])
        self.assertEqual(reopened.get(link['id'])['status'], 'error')
        self.assertIn('Approve it again', reopened.get(link['id'])['message'])
        self.assertEqual(reopened.get(up['id'])['status'], 'expired')

    def test_delete_removes_content_but_keeps_the_consent_audit(self):
        store = ResearchStore(self.dir)
        item, _ = store.add_link(self.source())
        store.begin_processing(item['id'], fingerprint(item))
        with self.assertRaises(StoreError) as caught:
            store.delete(item['id'])
        self.assertEqual(caught.exception.status, 409)
        store.finish(item['id'], 'error', 'x')
        store.delete(item['id'])
        with self.assertRaises(StoreError):
            store.get(item['id'])
        audit = store.consents(item['id'])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['fingerprint'], fingerprint(item))

    def test_library_size_is_bounded_but_duplicates_still_resolve(self):
        store = ResearchStore(self.dir)
        with patch('research_import.store.MAX_ITEMS', 3):
            for i in range(3):
                store.add_link(self.source(f'https://www.youtube.com/watch?v={str(i) * 11}'))
            with self.assertRaises(StoreError) as caught:
                store.add_link(self.source('https://www.youtube.com/watch?v=zzzzzzzzzzz'))
            self.assertEqual(caught.exception.status, 409)
            again, created = store.add_link(self.source('https://www.youtube.com/watch?v=00000000000'))
            self.assertFalse(created)
            self.assertEqual(len(store.list()), 3)

    def test_ids_are_validated(self):
        store = ResearchStore(self.dir)
        for bad in ('', '../x', 'A' * 32, "1' OR '1'='1", None, 5, 'f' * 31):
            with self.assertRaises(StoreError):
                store.get(bad)

    def test_default_store_location_is_the_gitignored_runtime_folder(self):
        ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('/runtime/', ignore)
        source = (ROOT / 'trading_app.py').read_text(encoding='utf-8')
        self.assertIn("ResearchImports(args.state.parent/'research-imports')", source)
        self.assertEqual(str(Path('runtime') / 'research-imports').replace('\\', '/').split('/')[0], 'runtime')


# ============================================================ consent and workflow


class ConsentWorkflow(Tmp):
    def test_staging_sends_nothing_and_fetches_nothing(self):
        reader, fetcher_ = FakeReader(), FakeFetcher()
        svc = make_service(self.dir, reader, fetcher_)
        link = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        direct = svc.add_link('https://cdn.example.com/a.mp4')
        file = upload(svc)
        self.assertEqual([x['item']['status'] for x in (link, direct, file)], ['pending_consent'] * 3)
        self.assertEqual((reader.calls, fetcher_.calls), ([], []))
        self.assertEqual(svc.store.consents(), [])
        self.assertIn('Nothing has been sent', link['item']['message'])

    def test_notice_names_the_exact_item(self):
        svc = make_service(self.dir)
        file = upload(svc, name='my strategy.mp4')['item']
        self.assertIn('my strategy.mp4', file['consent']['notice'])
        self.assertIn(file['content_sha256_prefix'], file['consent']['notice'])
        link = svc.add_link('https://www.instagram.com/reel/CxYz_123/')['item']
        self.assertIn('https://www.instagram.com/reel/CxYz_123/', link['consent']['notice'])
        self.assertIn('this one item only', link['consent']['notice'])
        self.assertNotEqual(file['consent']['fingerprint'], link['consent']['fingerprint'])

    def test_approval_needs_explicit_true_and_the_items_own_fingerprint(self):
        reader = FakeReader()
        svc = make_service(self.dir, reader)
        a = svc.add_link('https://www.youtube.com/watch?v=aaaaaaaaaaa')['item']
        b = svc.add_link('https://www.youtube.com/watch?v=bbbbbbbbbbb')['item']
        ok = {'id': a['id'], 'acknowledged': True, 'fingerprint': a['consent']['fingerprint'], 'notice_version': schema.NOTICE_VERSION}
        bad_bodies = [
            {**ok, 'acknowledged': 'true'}, {**ok, 'acknowledged': 1}, {**ok, 'acknowledged': None}, {**ok, 'acknowledged': False},
            {k: v for k, v in ok.items() if k != 'acknowledged'},
            {**ok, 'fingerprint': b['consent']['fingerprint']},            # approval given for a different item
            {**ok, 'fingerprint': 'f' * 64}, {**ok, 'fingerprint': None}, {**ok, 'fingerprint': ['x']},
            {**ok, 'notice_version': 'old'}, {**ok, 'id': 'f' * 32}, {**ok, 'id': None},
            {**ok, 'ids': [a['id'], b['id']]},                              # no bulk approval
            {'ids': [a['id'], b['id']], 'acknowledged': True}, {**ok, 'all': True}, [], 'approve', None,
        ]
        for body in bad_bodies:
            with self.assertRaises(StoreError, msg=repr(body)[:90]):
                svc.approve(body)
        self.assertEqual((reader.calls, svc.store.consents()), ([], []))
        self.assertEqual(svc.item(a['id'])['status'], 'pending_consent')
        approve(svc, a)
        svc.wait()
        self.assertEqual(len(reader.calls), 1)                              # exactly the one approved item
        self.assertEqual(svc.item(b['id'])['status'], 'pending_consent')    # its neighbour was untouched

    def test_one_approval_is_spent_by_one_read_and_every_retry_asks_again(self):
        reader = FakeReader(error=ReadFailure('The video reader could not finish.'))
        svc = make_service(self.dir, reader)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        approve(svc, item)
        svc.wait()
        failed = svc.item(item['id'])
        self.assertEqual((failed['status'], failed['attempts'], failed['message']), ('error', 1, 'The video reader could not finish.'))
        stale = {'id': item['id'], 'acknowledged': True, 'fingerprint': item['consent']['fingerprint'], 'notice_version': schema.NOTICE_VERSION}
        self.assertEqual(len(svc.store.consents(item['id'])), 1)
        reader.error = None
        self.assertIn('consent', failed)                                     # the UI must show the notice again
        svc.approve(stale)                                                   # a fresh, explicit approval runs once more
        svc.wait()
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(len(svc.store.consents(item['id'])), 2)
        self.assertEqual(svc.item(item['id'])['status'], 'done')
        with self.assertRaises(StoreError) as caught:                        # a finished item cannot be re-run by replaying it
            svc.approve(stale)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(reader.calls), 2)

    def test_unconfigured_reader_blocks_approval_and_records_no_consent(self):
        reader = FakeReader(configured=False)
        svc = make_service(self.dir, reader)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        with self.assertRaises(StoreError) as caught:
            approve(svc, item)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('not set up', str(caught.exception))
        self.assertEqual((reader.calls, svc.store.consents(), svc.item(item['id'])['status']), ([], [], 'pending_consent'))

    def test_only_one_item_is_read_at_a_time_and_the_loser_records_no_consent(self):
        reader = FakeReader()
        reader.gate = threading.Event()
        svc = make_service(self.dir, reader)
        a = svc.add_link('https://www.youtube.com/watch?v=aaaaaaaaaaa')['item']
        b = svc.add_link('https://www.youtube.com/watch?v=bbbbbbbbbbb')['item']
        approve(svc, a)
        with self.assertRaises(StoreError) as caught:
            approve(svc, b)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual([c['item_id'] for c in svc.store.consents()], [a['id']])
        self.assertEqual(svc.item(b['id'])['status'], 'pending_consent')
        reader.gate.set()
        svc.wait()

    def test_a_changed_item_invalidates_an_earlier_fingerprint(self):
        svc = make_service(self.dir)
        item = upload(svc)['item']
        path = svc.store.get(item['id'])['staged_path']
        with svc.store.connection() as c:
            c.execute('UPDATE items SET content_sha256=? WHERE id=?', ('0' * 64, item['id']))
        with self.assertRaises(StoreError) as caught:
            approve(svc, item)
        self.assertIn('not for this item as it stands', str(caught.exception))
        self.assertTrue(Path(path).exists())

    def test_successful_read_stores_a_labelled_unvalidated_result(self):
        reader = FakeReader()
        svc = make_service(self.dir, reader)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        approve(svc, item)
        svc.wait()
        done = svc.item(item['id'])
        self.assertEqual(done['status'], 'done')
        self.assertIs(done['validated'], False)
        result = done['result']
        self.assertIs(result['reading']['validated'], False)
        self.assertEqual(result['evidence']['tier'], 'E2')
        self.assertIs(result['evidence']['validated'], False)
        self.assertEqual(result['reading']['claimed_returns'][0]['label'], 'Source claim, unverified')
        self.assertIn('not validated', result['label'])
        self.assertEqual((done['title'], done['creator']), ('Opening range breakout', 'sometrader'))
        self.assertEqual(done['consents'][0]['outcome'], 'done')
        self.assertNotIn('consent', done)                                    # nothing left to approve
        self.assertEqual([i['id'] for i in svc.items('breakout')], [item['id']])

    def test_no_research_invalid_output_and_failures_are_reported_not_imported(self):
        cases = [({'is_trading_research': False}, 'no_research_found', 'nothing was imported'),
                 ('not json at all', 'error', 'did not return a JSON'), ({'is_trading_research': 'maybe'}, 'error', 'did not say'),
                 (GOOD | {'is_trading_research': True, 'strategy': {}, 'entry_rules': [], 'exit_rules': [], 'risk_rules': [],
                          'claimed_returns': []}, 'no_research_found', 'nothing was imported')]
        for reply, status, text in cases:
            with tempfile.TemporaryDirectory() as tmp:
                svc = make_service(tmp, FakeReader(reply))
                item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
                approve(svc, item)
                svc.wait()
                got = svc.item(item['id'])
                self.assertEqual(got['status'], status, reply)
                self.assertIn(text, got['message'])
                self.assertNotIn('result', got)

    def test_unexpected_failures_do_not_leak_internals(self):
        reader = FakeReader(error=RuntimeError(f'boom at C:\\Users\\erik9\\secret.txt with {SECRET}'))
        svc = make_service(self.dir, reader)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        with self.assertLogs('research_import', 'WARNING') as logged:
            approve(svc, item)
            svc.wait()
        got = svc.item(item['id'])
        self.assertEqual(got['status'], 'error')
        self.assertNotIn('erik9', json.dumps(got))
        self.assertNotIn(SECRET, json.dumps(got))
        self.assertNotIn(SECRET, ' '.join(logged.output))                     # the server log names the exception type only
        self.assertNotIn('erik9', ' '.join(logged.output))
        self.assertIn('RuntimeError', ' '.join(logged.output))

    def test_temp_files_are_removed_after_success_and_failure(self):
        for error in (None, ReadFailure('nope'), RuntimeError('x')):
            with tempfile.TemporaryDirectory() as tmp:
                reader = FakeReader(error=error)
                svc = make_service(tmp, reader)
                item = upload(svc)['item']
                staged = Path(svc.store.get(item['id'])['staged_path'])
                self.assertTrue(staged.exists())
                approve(svc, item)
                svc.wait()
                self.assertFalse(staged.exists(), 'the uploaded file must not outlive its read')
                self.assertEqual(list(svc.work.iterdir()), [], 'the per-read work folder must be removed')
                self.assertEqual(list(svc.staging.root.iterdir()), [])
                self.assertEqual(reader.calls[0].path, str(staged))
                self.assertFalse(svc.staging.usage()[0])

    def test_direct_link_is_downloaded_only_after_approval_through_the_guarded_fetcher(self):
        reader, fetcher_ = FakeReader(), FakeFetcher()
        svc = make_service(self.dir, reader, fetcher_)
        item = svc.add_link('https://cdn.example.com/a.mp4')['item']
        self.assertEqual(fetcher_.calls, [])
        approve(svc, item)
        svc.wait()
        self.assertEqual(fetcher_.calls, ['https://cdn.example.com/a.mp4'])
        self.assertEqual((reader.calls[0].kind, reader.calls[0].family), ('file', 'video'))
        self.assertEqual(svc.item(item['id'])['status'], 'done')
        self.assertEqual(list(svc.work.iterdir()), [])

    def test_downloaded_file_must_match_its_claimed_type(self):
        svc = make_service(self.dir, FakeReader(), FakeFetcher(body=b'<html>not a video</html>' + b'\0' * 40))
        item = svc.add_link('https://cdn.example.com/a.mp4')['item']
        approve(svc, item)
        svc.wait()
        self.assertEqual(svc.item(item['id'])['status'], 'error')
        self.assertIn('not the media type', svc.item(item['id'])['message'])

    def test_fetch_refusal_is_reported_and_reader_never_runs(self):
        class Refuses:
            def download(self, *a, **k):
                raise safe_fetch.FetchBlocked('That address resolves to a non-public network location and was refused.', 'private_address')
        reader = FakeReader()
        svc = make_service(self.dir, reader, Refuses())
        item = svc.add_link('https://cdn.example.com/a.mp4')['item']
        approve(svc, item)
        svc.wait()
        self.assertEqual((svc.item(item['id'])['status'], reader.calls), ('error', []))
        self.assertIn('non-public', svc.item(item['id'])['message'])

    def test_dedup_of_links_and_uploads_keeps_one_item_and_one_file(self):
        svc = make_service(self.dir)
        first = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        second = svc.add_link('https://youtu.be/dQw4w9WgXcQ?t=5')
        self.assertEqual((first['duplicate'], second['duplicate'], first['item']['id'] == second['item']['id']), (False, True, True))
        one = upload(svc, MP4, 'a.mp4')
        renamed = upload(svc, MP4, 'different-name.mp4')
        other = upload(svc, MP4 + b'1', 'b.mp4')
        self.assertEqual((one['duplicate'], renamed['duplicate'], other['duplicate']), (False, True, False))
        self.assertEqual(one['item']['id'], renamed['item']['id'])
        self.assertEqual(svc.staging.usage()[0], 2)                          # the duplicate's copy was discarded
        self.assertEqual(len(svc.items()), 3)

    def test_duplicate_of_a_finished_item_does_not_reset_or_reread_it(self):
        reader = FakeReader()
        svc = make_service(self.dir, reader)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        approve(svc, item)
        svc.wait()
        again = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1')
        self.assertTrue(again['duplicate'])
        self.assertEqual((again['item']['status'], len(reader.calls)), ('done', 1))
        self.assertNotIn('consent', again['item'])

    def test_failed_or_expired_upload_can_be_staged_again_under_the_same_identity(self):
        reader = FakeReader(error=ReadFailure('nope'))
        svc = make_service(self.dir, reader)
        first = upload(svc)['item']
        approve(svc, first)
        svc.wait()
        self.assertEqual(svc.item(first['id'])['status'], 'error')
        self.assertNotIn('consent', svc.item(first['id']))                   # the file is gone, so there is nothing to approve
        again = upload(svc)
        self.assertEqual((again['duplicate'], again['item']['id']), (False, first['id']))
        self.assertEqual(again['item']['status'], 'pending_consent')
        self.assertIn('consent', again['item'])
        self.assertEqual(len(svc.items()), 1)

    def test_restart_marks_interrupted_reads_and_keeps_pending_items(self):
        reader = FakeReader()
        reader.gate = threading.Event()
        svc = make_service(self.dir, reader)
        link = svc.add_link('https://www.youtube.com/watch?v=aaaaaaaaaaa')['item']
        pending = svc.add_link('https://www.youtube.com/watch?v=bbbbbbbbbbb')['item']
        approve(svc, link)
        reopened = ResearchImports(self.dir / 'research-imports', reader=FakeReader(), fetcher=FakeFetcher())
        self.assertEqual(reopened.item(link['id'])['status'], 'error')
        self.assertEqual(reopened.item(pending['id'])['status'], 'pending_consent')
        self.assertEqual(reopened.item(link['id'])['attempts'], 1)
        reader.gate.set()
        svc.wait()

    def test_review_and_delete_through_the_service(self):
        svc = make_service(self.dir)
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        reviewed = svc.review(item['id'], 'shortlisted', 'design an experiment later')
        self.assertEqual((reviewed['review_state'], reviewed['review_note'], reviewed['validated']), ('shortlisted', 'design an experiment later', False))
        with self.assertRaises(StoreError):
            svc.review(item['id'], 'validated', '')
        up = upload(svc)['item']
        staged = Path(svc.store.get(up['id'])['staged_path'])
        svc.delete(up['id'])
        self.assertFalse(staged.exists())
        with self.assertRaises(StoreError):
            svc.item(up['id'])


# ============================================================ reader boundary


class ReaderBoundary(Tmp):
    def reader(self, env, runs, **kw):
        def run(args, **options):
            runs.append((list(args), options))
            return kw.get('outcome', lambda a, o: {'code': 0, 'terminated': None, 'stdout': '', 'stderr': ''})(list(args), options)
        (self.dir / 'read_b.py').write_text('# fake reader', encoding='utf-8')
        (self.dir / 'slide_reader.py').write_text('# fake slides', encoding='utf-8')
        env = {'TRADING_RESEARCH_READER': str(self.dir / 'read_b.py'), 'TRADING_RESEARCH_SLIDE_READER': str(self.dir / 'slide_reader.py'),
               'TRADING_RESEARCH_DOWNLOADER': 'fake-dl', **env}
        return GeminiSkillReader(env, run=run, which=lambda name: 'C:/tools/fake-dl.exe' if name == 'fake-dl' else None)

    def test_capabilities_are_booleans_and_never_carry_the_key_or_a_path(self):
        runs = []
        caps = self.reader({'GEMINI_API_KEY': SECRET}, runs).capabilities()
        text = json.dumps(caps)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(str(self.dir), text)
        self.assertNotIn('\\', text)
        self.assertTrue(caps['configured'] and caps['api_key_present'])
        missing = self.reader({}, runs).capabilities()
        self.assertFalse(missing['configured'])
        self.assertIn('Gemini API key', ' '.join(missing['missing']))
        self.assertEqual(runs, [])

    def test_child_environment_hands_the_key_only_to_the_reader(self):
        env = {'PATH': 'p', 'GEMINI_API_KEY': SECRET, 'GOOGLE_API_KEY': SECRET + '2', 'AWS_SECRET_ACCESS_KEY': 'x', 'SCHWAB_TOKEN': 'y', 'HOME': 'h'}
        plain = child_environment(env)
        self.assertEqual(set(plain) & {'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'SCHWAB_TOKEN'}, set())
        with_key = child_environment(env, True)
        self.assertEqual(with_key['GEMINI_API_KEY'], SECRET)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', with_key)
        self.assertNotIn('SCHWAB_TOKEN', with_key)

    def test_youtube_goes_to_the_reader_as_a_fixed_argument_list(self):
        runs = []

        def outcome(args, options):
            Path(args[args.index('--out') + 1]).write_text(json.dumps(GOOD), encoding='utf-8')
            return {'code': 0, 'terminated': None, 'stdout': '', 'stderr': ''}
        reader = self.reader({'GEMINI_API_KEY': SECRET}, runs, outcome=outcome)
        work = self.dir / 'work'
        work.mkdir()
        out = reader.read(Media('link', 'youtube', url='https://www.youtube.com/watch?v=dQw4w9WgXcQ'), work)
        self.assertEqual(out.method, 'video')
        args, options = runs[0]
        self.assertEqual(args[2], 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        self.assertIsInstance(args, list)
        self.assertEqual(options['env']['GEMINI_API_KEY'], SECRET)
        self.assertNotIn('recipe', (work / 'reading-prompt.md').read_text(encoding='utf-8').lower())
        self.assertEqual(len(runs), 1)                                        # no download for YouTube: Gemini fetches it

    def test_tiktok_download_runs_without_the_key_then_the_reader_runs_with_it(self):
        runs = []

        def outcome(args, options):
            if args[0] == 'C:/tools/fake-dl.exe':
                Path(args[args.index('-o') + 1].replace('%(ext)s', 'mp4')).write_bytes(MP4)
            else:
                Path(args[args.index('--out') + 1]).write_text('{}', encoding='utf-8')
            return {'code': 0, 'terminated': None, 'stdout': '', 'stderr': ''}
        reader = self.reader({'GEMINI_API_KEY': SECRET}, runs, outcome=outcome)
        work = self.dir / 'work'
        work.mkdir()
        reader.read(Media('link', 'tiktok', url='https://www.tiktok.com/@u/video/7312345678901234567'), work)
        (download_args, download_opts), (read_args, read_opts) = runs
        self.assertNotIn('GEMINI_API_KEY', download_opts['env'])
        self.assertEqual(read_opts['env']['GEMINI_API_KEY'], SECRET)
        for flag in ('--ignore-config', '--no-playlist', '--max-filesize', '--max-downloads'):
            self.assertIn(flag, download_args)
        self.assertNotIn('--cookies', ' '.join(download_args))
        self.assertNotIn('--cookies-from-browser', ' '.join(download_args))
        self.assertEqual(download_opts['max_file_bytes'], 80 * 1024 * 1024)
        self.assertTrue(read_args[2].endswith('media.mp4'))

    def test_terminated_tools_and_missing_tools_are_failures_not_results(self):
        work = self.dir / 'work'
        work.mkdir()
        runs = []
        for reason in ('timeout', 'output_limit', 'file_limit'):
            reader = self.reader({'GEMINI_API_KEY': SECRET}, runs, outcome=lambda a, o, r=reason: {'code': 0, 'terminated': r, 'stdout': '', 'stderr': ''})
            with self.assertRaises(ReadFailure) as caught:
                reader.read(Media('link', 'youtube', url='https://www.youtube.com/watch?v=dQw4w9WgXcQ'), work)
            self.assertIn('Nothing was imported', str(caught.exception))

        def gone(args, options):
            raise FileNotFoundError('python')
        with self.assertRaises(ReaderUnavailable):
            self.reader({'GEMINI_API_KEY': SECRET}, runs, outcome=gone).read(Media('link', 'youtube', url='https://www.youtube.com/watch?v=dQw4w9WgXcQ'), work)
        with self.assertRaises(ReaderUnavailable):
            self.reader({}, runs).read(Media('link', 'youtube', url='https://www.youtube.com/watch?v=dQw4w9WgXcQ'), work)

    def test_carousel_needs_equal_positive_counts(self):
        work = self.dir / 'work'
        work.mkdir()

        def bridge(counts, status='complete', kind='slides'):
            answer = {'kind': kind, 'status': status, 'expectedCount': counts[0], 'observedCount': counts[1], 'modelReportedSlides': counts[2],
                      'body': json.dumps(GOOD)}
            return lambda a, o: {'code': 0, 'terminated': None, 'stdout': json.dumps(answer), 'stderr': ''}
        media = Media('link', 'instagram', url='https://www.instagram.com/p/CxYz_123/')
        ok = self.reader({'GEMINI_API_KEY': SECRET}, [], outcome=bridge((3, 3, 3))).read(media, work)
        self.assertEqual(ok.method, 'slides')
        for counts, status in (((3, 3, 2), 'complete'), ((3, 2, 2), 'complete'), ((3, 3, 4), 'complete'), ((0, 0, 0), 'complete'),
                               ((3, 3, None), 'complete'), ((True, True, True), 'complete'), ((3, 3, 3), 'partial'), ((3, 3, 3), 'failed')):
            with self.assertRaises(ReadFailure, msg=(counts, status)):
                self.reader({'GEMINI_API_KEY': SECRET}, [], outcome=bridge(counts, status)).read(media, work)

    def test_instagram_video_falls_through_to_download_and_video_read(self):
        runs = []

        def outcome(args, options):
            if len(args) == 2 and args[1].endswith('slides_bridge.py'):
                return {'code': 0, 'terminated': None, 'stdout': json.dumps({'kind': 'video'}), 'stderr': ''}
            if args[0] == 'C:/tools/fake-dl.exe':
                Path(args[args.index('-o') + 1].replace('%(ext)s', 'mp4')).write_bytes(MP4)
            else:
                Path(args[args.index('--out') + 1]).write_text('{}', encoding='utf-8')
            return {'code': 0, 'terminated': None, 'stdout': '', 'stderr': ''}
        work = self.dir / 'work'
        work.mkdir()
        out = self.reader({'GEMINI_API_KEY': SECRET}, runs, outcome=outcome).read(
            Media('link', 'instagram', url='https://www.instagram.com/reel/CxYz_123/'), work)
        self.assertEqual(out.method, 'video')
        self.assertEqual(len(runs), 3)
        self.assertNotIn('GEMINI_API_KEY', runs[1][1]['env'])
        self.assertIn('GEMINI_API_KEY', runs[0][1]['env'])
        self.assertNotIn(SECRET, ' '.join(runs[0][0]))                        # request travels on stdin, never in argv

    def test_uploaded_image_uses_the_slide_reader_with_its_path_on_stdin(self):
        runs = []
        answer = {'kind': 'slides', 'status': 'complete', 'expectedCount': 1, 'observedCount': 1, 'modelReportedSlides': 1, 'body': '{}'}
        work = self.dir / 'work'
        work.mkdir()
        out = self.reader({'GEMINI_API_KEY': SECRET}, runs,
                          outcome=lambda a, o: {'code': 0, 'terminated': None, 'stdout': json.dumps(answer), 'stderr': ''}).read(
            Media('file', 'upload', path=str(self.dir / 'shot.png'), family='image'), work)
        self.assertEqual(out.method, 'slides')
        request = json.loads(runs[0][1]['input_text'])
        self.assertEqual((request['mode'], request['path']), ('image', str(self.dir / 'shot.png')))
        self.assertIn('SLIDES_READ', (work / 'slides-prompt.md').read_text(encoding='utf-8'))


class BoundedProcess(Tmp):
    def test_timeout_kills_the_tree_and_is_marked_terminated(self):
        start = time.monotonic()
        out = run_bounded([sys.executable, '-c', 'import time; time.sleep(60)'], env=child_environment(os.environ), timeout=1)
        self.assertEqual(out['terminated'], 'timeout')
        self.assertLess(time.monotonic() - start, 30)

    def test_runaway_output_is_capped_and_terminated(self):
        out = run_bounded([sys.executable, '-c', 'import sys\nwhile True: sys.stdout.write("x"*65536); sys.stdout.flush()'],
                          env=child_environment(os.environ), timeout=30, max_output=200_000)
        self.assertEqual(out['terminated'], 'output_limit')
        self.assertLessEqual(len(out['stdout']), 300_000)

    def test_growing_download_is_stopped_at_the_file_cap(self):
        code = 'import time\nf=open("big.bin","wb")\nwhile True:\n    f.write(b"x"*100000); f.flush(); time.sleep(0.05)'
        out = run_bounded([sys.executable, '-c', code], env=child_environment(os.environ), cwd=str(self.dir), timeout=30, max_file_bytes=500_000)
        self.assertEqual(out['terminated'], 'file_limit')

    def test_normal_run_returns_output_and_passes_stdin_without_a_shell(self):
        out = run_bounded([sys.executable, '-c', 'import sys; print(sys.stdin.read().upper())'], env=child_environment(os.environ),
                          input_text='hello; echo injected')
        self.assertEqual((out['code'], out['terminated'], out['stdout'].strip()), (0, None, 'HELLO; ECHO INJECTED'))

    def test_the_provider_key_is_absent_from_a_plain_child(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': SECRET, 'GOOGLE_API_KEY': SECRET}):
            out = run_bounded([sys.executable, '-c', 'import os; print(sorted(k for k in os.environ if "API_KEY" in k))'],
                              env=child_environment(os.environ))
        self.assertEqual(out['stdout'].strip(), '[]')

    def test_missing_executable_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_bounded([str(self.dir / 'no-such-tool.exe')], env={})


class SlidesBridge(Tmp):
    def fake_slide_reader(self):
        folder = self.dir / 'skill'
        folder.mkdir()
        (folder / 'slide_reader.py').write_text('''
import hashlib
from pathlib import Path
from types import SimpleNamespace
READ_B_DIR = None
class SlideError(Exception): pass
def import_local_image(path, dest_dir, slug, index, source_url, max_bytes=1):
    data = Path(path).read_bytes()
    if not data.startswith(b"\\x89PNG"): raise SlideError("not an image")
    return SimpleNamespace(index=index, path=Path(path))
def gemini_read(files, prompt, *, read_b=None):
    assert "SLIDES_READ" in prompt
    return SimpleNamespace(header={"SLIDES_READ": str(len(files))}, body="{\\"is_trading_research\\": false}")
def _int_or_none(v): return int(v) if v is not None else None
''', encoding='utf-8')
        (folder / 'read_b.py').write_text('', encoding='utf-8')
        return folder

    def run_bridge(self, request, env=None):
        script = ROOT / 'research_import' / 'slides_bridge.py'
        return subprocess.run([sys.executable, '-B', str(script)], input=json.dumps(request), capture_output=True, text=True, timeout=60,
                              env={**child_environment(os.environ), **(env or {})})

    def test_uploaded_image_is_read_through_the_installed_helper(self):
        folder = self.fake_slide_reader()
        png = self.dir / 'shot.png'
        png.write_bytes(PNG)
        prompt = self.dir / 'prompt.md'
        prompt.write_text(schema.READING_PROMPT + schema.SLIDES_INSTRUCTION, encoding='utf-8')
        out = self.run_bridge({'mode': 'image', 'path': str(png), 'out_dir': str(self.dir / 'out'), 'slide_reader': str(folder / 'slide_reader.py'),
                               'read_b_dir': str(folder), 'prompt_file': str(prompt)})
        answer = json.loads(out.stdout)
        self.assertEqual((answer['kind'], answer['expectedCount'], answer['observedCount'], answer['modelReportedSlides']), ('slides', 1, 1, 1))
        self.assertEqual(json.loads(answer['body']), {'is_trading_research': False})

    def test_bridge_failures_expose_no_paths_or_details(self):
        folder = self.fake_slide_reader()
        prompt = self.dir / 'prompt.md'
        prompt.write_text('p', encoding='utf-8')
        bad = self.dir / 'notes.png'
        bad.write_bytes(b'not a png')
        out = self.run_bridge({'mode': 'image', 'path': str(bad), 'out_dir': str(self.dir / 'out'), 'slide_reader': str(folder / 'slide_reader.py'),
                               'read_b_dir': str(folder), 'prompt_file': str(prompt)})
        answer = json.loads(out.stdout)
        self.assertEqual(answer['kind'], 'error')
        self.assertNotIn(str(self.dir), out.stdout)
        crash = self.run_bridge({'mode': 'image'})
        self.assertEqual(json.loads(crash.stdout)['kind'], 'error')

    def test_only_public_instagram_post_urls_are_accepted_by_the_bridge(self):
        folder = self.fake_slide_reader()
        prompt = self.dir / 'prompt.md'
        prompt.write_text('p', encoding='utf-8')
        for url in ('https://evil.example/p/abc12345/', 'http://www.instagram.com/p/abc12345/', 'https://www.instagram.com/p/abc12345/?x=1',
                    'https://www.instagram.com.evil.example/p/abc12345/'):
            out = self.run_bridge({'mode': 'url', 'url': url, 'out_dir': str(self.dir / 'out'), 'slide_reader': str(folder / 'slide_reader.py'),
                                   'read_b_dir': str(folder), 'prompt_file': str(prompt)})
            self.assertEqual(json.loads(out.stdout)['kind'], 'error', url)


# ============================================================ HTTP surface


class Http(Tmp):
    def setUp(self):
        super().setUp()
        self.book = PaperBook(self.dir / 'paper.sqlite3')
        self.reader = FakeReader()
        self.service = make_service(self.dir / 'state', self.reader)
        self.server = make_server(Controller(self.book, research=self.service), 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        page = self.call('GET', '/', raw=True)[2].decode()
        self.token = page.split("const token='")[1].split("'")[0]
        self.origin = f'http://127.0.0.1:{self.port}'

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            conn.putrequest(method, path, skip_host=True)
            sent = {'Host': f'127.0.0.1:{self.port}', **(headers or {})}
            if body is not None and 'Content-Length' not in sent:
                sent['Content-Length'] = str(len(body))
            for k, v in sent.items():
                if v is not None:
                    conn.putheader(k, v)
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data if raw else json.loads(data or b'null')
        finally:
            conn.close()

    def post(self, path, payload, **over):
        headers = {'Content-Type': 'application/json', 'Origin': self.origin, 'X-Paper-Token': self.token, **over}
        return self.call('POST', path, json.dumps(payload).encode(), headers)

    def put_file(self, data=MP4, name='clip.mp4', ctype='video/mp4', **over):
        headers = {'Content-Type': ctype, 'X-Filename': name, 'Origin': self.origin, 'X-Paper-Token': self.token, **over}
        return self.call('POST', '/api/research-import/upload', data, headers)

    def test_server_binds_loopback_on_a_nonproduction_port(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertNotEqual(self.port, 8791)

    def test_reads_need_a_loopback_host_and_no_cross_site_fetch(self):
        self.assertEqual(self.call('GET', '/api/research-import/status')[0], 200)
        self.assertEqual(self.call('GET', '/api/research-import/items')[0], 200)
        self.assertEqual(self.call('GET', '/api/research-import/status', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('GET', '/api/research-import/status', headers={'Host': f'evil.example:{self.port}'})[0], 403)
        self.assertEqual(self.call('GET', '/api/research-import/status', headers={'Host': None})[0], 403)
        self.assertEqual(self.call('GET', '/api/research-import/items', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.call('GET', '/api/research-import/items', headers={'Sec-Fetch-Site': 'same-site'})[0], 403)
        self.assertEqual(self.call('GET', '/api/research-import/items', headers={'Sec-Fetch-Site': 'same-origin'})[0], 200)
        self.assertEqual(self.call('GET', '/api/research-import/nope')[0], 404)

    def test_writes_need_token_and_this_apps_own_origin(self):
        payload = {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}
        good = {'Content-Type': 'application/json'}
        body = json.dumps(payload).encode()
        cases = [
            {**good},                                                          # nothing
            {**good, 'Origin': self.origin},                                   # no token
            {**good, 'X-Paper-Token': self.token},                             # no Origin: not a same-origin browser request
            {**good, 'X-Paper-Token': 'wrong', 'Origin': self.origin},
            {**good, 'X-Paper-Token': self.token, 'Origin': 'http://evil.example'},
            {**good, 'X-Paper-Token': self.token, 'Origin': 'http://127.0.0.1:1'},
            {**good, 'X-Paper-Token': self.token, 'Origin': 'null'},
            {**good, 'X-Paper-Token': self.token, 'Origin': self.origin, 'Sec-Fetch-Site': 'cross-site'},
            {**good, 'X-Paper-Token': self.token, 'Origin': self.origin, 'Host': 'evil.example'},
            {**good, 'X-Paper-Token': 'é' * 3, 'Origin': self.origin},          # a non-ASCII token must be a clean refusal, not a crash
        ]
        for headers in cases:
            self.assertEqual(self.call('POST', '/api/research-import/link', body, headers)[0], 403, headers)
        self.assertEqual(self.service.store.list(), [])
        status, _, got = self.post('/api/research-import/link', payload)
        self.assertEqual((status, got['duplicate'], got['item']['status']), (200, False, 'pending_consent'))
        self.assertEqual(self.reader.calls, [])

    def test_unauthorised_upload_never_reaches_the_staging_area(self):
        for headers in ({'X-Paper-Token': None}, {'Origin': None}, {'Origin': 'http://evil.example'}):
            status, _, _ = self.put_file(**headers)
            self.assertEqual(status, 403, headers)
        self.assertEqual(self.service.staging.usage(), (0, 0))

    def test_json_routes_validate_content_type_and_size(self):
        headers = {'Origin': self.origin, 'X-Paper-Token': self.token}
        self.assertEqual(self.call('POST', '/api/research-import/link', b'{"url": "x"}', {**headers, 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call('POST', '/api/research-import/link', b'not json', {**headers, 'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.call('POST', '/api/research-import/link', b'[]', {**headers, 'Content-Type': 'application/json'})[0], 400)
        big = json.dumps({'url': 'https://example.com/' + 'a' * 9000}).encode()
        self.assertEqual(self.call('POST', '/api/research-import/link', big, {**headers, 'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.post('/api/research-import/link', {'url': 5})[0], 400)
        self.assertEqual(self.post('/api/research-import/link', {'url': 'https://127.0.0.1/x.mp4'})[0], 400)
        self.assertEqual(self.post('/api/research-import/unknown', {})[0], 404)
        # The paper routes keep their original 1 KB body limit.
        self.assertEqual(self.call('POST', '/api/pause', b'{"paused": true, "pad": "' + b'x' * 2000 + b'"}',
                                   {**headers, 'Content-Type': 'application/json'})[0], 400)

    def test_upload_limits_types_and_signatures_over_http(self):
        self.assertEqual(self.put_file()[0], 200)
        status, _, got = self.put_file(MP4, 'clip.mp4')
        self.assertEqual((status, got['duplicate']), (200, True))
        self.assertEqual(self.put_file(PNG, 'shot.png', 'image/png')[0], 200)
        self.assertEqual(self.put_file(b'MZ' + b'\0' * 40, 'run.exe', 'application/octet-stream')[0], 415)
        self.assertEqual(self.put_file(PNG, 'fake.mp4', 'video/mp4')[0], 415)
        self.assertEqual(self.put_file(MP4, 'clip.mp4', 'text/html')[0], 415)
        self.assertEqual(self.put_file(b'', 'clip.mp4')[0], 411)
        self.assertEqual(self.call('POST', '/api/research-import/upload', MP4, {'Content-Type': 'video/mp4', 'X-Filename': 'n.mp4', 'Origin': self.origin,
                                                                              'X-Paper-Token': self.token, 'Content-Length': 'abc'})[0], 411)
        status, _, got = self.call('POST', '/api/research-import/upload', b'', {'Content-Type': 'video/mp4', 'X-Filename': 'big.mp4', 'Origin': self.origin,
                                                                               'X-Paper-Token': self.token, 'Content-Length': str(files.MAX_VIDEO_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.service.staging.usage()[0], 2)

    def test_full_consent_flow_over_http_and_the_key_never_reaches_the_browser(self):
        env_reader = GeminiSkillReader({'GEMINI_API_KEY': SECRET})
        self.service.reader = env_reader
        status, _, caps = self.call('GET', '/api/research-import/status')
        self.assertNotIn(SECRET, json.dumps(caps))
        self.service.reader = self.reader
        _, _, staged = self.post('/api/research-import/link', {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'})
        item = staged['item']
        bad = self.post('/api/research-import/approve', {'id': item['id'], 'acknowledged': False, 'fingerprint': item['consent']['fingerprint'],
                                                          'notice_version': item['consent']['notice_version']})
        self.assertEqual(bad[0], 400)
        self.assertEqual(self.reader.calls, [])
        ok = self.post('/api/research-import/approve', {'id': item['id'], 'acknowledged': True, 'fingerprint': item['consent']['fingerprint'],
                                                         'notice_version': item['consent']['notice_version']})
        self.assertEqual((ok[0], ok[2]['status']), (200, 'processing'))
        self.service.wait()
        _, _, done = self.call('GET', f"/api/research-import/items/{item['id']}")
        self.assertEqual((done['status'], done['validated'], done['result']['reading']['validated']), ('done', False, False))
        blob = json.dumps(done)
        self.assertNotIn('staged_path', blob)
        self.assertNotIn(str(self.dir), blob)
        _, _, found = self.call('GET', '/api/research-import/items?q=breakout&review=new&status=done')
        self.assertEqual([i['id'] for i in found['items']], [item['id']])
        reviewed = self.post('/api/research-import/review', {'id': item['id'], 'state': 'shortlisted', 'note': 'test on paper first'})
        self.assertEqual(reviewed[2]['review_state'], 'shortlisted')
        self.assertEqual(self.post('/api/research-import/review', {'id': item['id'], 'state': 'validated'})[0], 400)
        self.assertEqual(self.post('/api/research-import/delete', {'id': item['id']})[0], 200)
        self.assertEqual(self.call('GET', f"/api/research-import/items/{item['id']}")[0], 404)
        self.assertEqual(self.post('/api/research-import/delete', {'id': '../../x'})[0], 400)

    def test_page_ships_the_tab_but_no_secret_and_only_the_process_token(self):
        page = self.call('GET', '/', raw=True)[2].decode()
        self.assertIn('data-tab="imports"', page)
        self.assertIn('Research imports', page)
        self.assertNotIn(SECRET, page)
        self.assertNotIn('GEMINI', page.upper().replace('GOOGLE GEMINI', ''))
        self.assertEqual(page.count('__TOKEN__'), 0)

    def test_disabled_research_is_a_clean_answer(self):
        server = make_server(Controller(self.book), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            conn.request('GET', '/api/research-import/status')
            self.assertEqual(json.loads(conn.getresponse().read()), {'enabled': False})
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_port_override_is_validated(self):
        with patch.dict(os.environ, {'TRADING_APP_PORT': '18791'}):
            self.assertEqual(env_port(), 18791)
        with patch.dict(os.environ, {'TRADING_APP_PORT': '0'}):
            self.assertEqual(env_port(), 0)
        for bad in ('abc', '-1', '70000', '8791.5', ''):
            with patch.dict(os.environ, {'TRADING_APP_PORT': bad}), self.assertRaises(SystemExit):
                env_port()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TRADING_APP_PORT', None)
            self.assertEqual(env_port(), 8791)


class LaunchedServer(Tmp):
    """The real main() with research import on, hidden, loopback, ephemeral port, temporary state."""

    def test_main_wires_the_store_under_the_state_folder_and_serves_the_routes(self):
        state = self.dir / 'state' / 'paper.sqlite3'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        env = {**child_environment(os.environ), 'TRADING_APP_PORT': '0'}
        env.pop('GEMINI_API_KEY', None)
        proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'trading_app.py'), '--no-browser', '--no-scheduler', '--no-research-helpers',
                                 '--state', str(state)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ROOT),
                                text=True, creationflags=flags)
        try:
            line = proc.stdout.readline()
            self.assertIn('http://127.0.0.1:', line)
            port = int(line.split('http://127.0.0.1:')[1].split()[0].rstrip(','))
            self.assertNotEqual(port, 8791)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            conn.request('GET', '/api/research-import/status')
            caps = json.loads(conn.getresponse().read())
            conn.close()
            self.assertTrue(caps['enabled'])
            self.assertFalse(caps['capabilities']['configured'])
            self.assertTrue((self.dir / 'state' / 'research-imports' / 'research-imports.sqlite3').exists())
        finally:
            proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                pipe.close()


# ============================================================ separation from the trading engine


FORBIDDEN_IMPORTS = {'paper_book', 'trading_engine', 'collector', 'market_lab', 'stock_experiments', 'active_experiment', 'strategy_c',
                     'strategy_config', 'build_dashboard', 'research_desk', 'execution_model', 'system_execution_client',
                     'system_strategy_evaluator', 'algo_stocks', 'algo_crypto', 'algo_forex', 'backtest_engine', 'journal', 'live_picks',
                     'trading_app', 'yfinance', 'pandas', 'numpy', 'requests', 'schwab'}
ENGINE_FILES = ['paper_book.py', 'trading_engine.py', 'collector.py', 'market_lab.py', 'stock_experiments.py', 'active_experiment.py',
                'strategy_c.py', 'strategy_config.py', 'execution_model.py', 'research_desk.py', 'research_roles.py', 'research_ledger.py',
                'system_execution_client.py', 'system_strategy_evaluator.py', 'algo_stocks.py', 'algo_crypto.py', 'algo_forex.py',
                'backtest_engine.py', 'build_dashboard.py', 'main.py', 'dashboard.py', 'live_picks.py', 'journal.py', 'breadth_gate.py']


def imported_modules(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split('.')[0])
    return names


class SeparationFromTradingEngine(Tmp):
    def test_importer_code_imports_no_trading_code_and_no_market_data_library(self):
        for path in sorted((ROOT / 'research_import').glob('*.py')):
            self.assertEqual(imported_modules(path) & FORBIDDEN_IMPORTS, set(), path.name)

    def test_no_engine_module_imports_the_importer(self):
        for name in ENGINE_FILES:
            if (ROOT / name).exists():
                self.assertNotIn('research_import', imported_modules(ROOT / name), name)
                self.assertNotIn('research_import', (ROOT / name).read_text(encoding='utf-8'), name)

    def test_only_the_dashboard_server_connects_the_importer_and_only_through_routes(self):
        source = (ROOT / 'trading_app.py').read_text(encoding='utf-8')
        lines = [l.strip() for l in source.splitlines() if 'research_import' in l and l.strip().startswith(('import ', 'from '))]
        self.assertEqual(lines, ['import research_import.routes as research_routes', 'from research_import import ResearchImports'])
        tree = ast.parse(source)
        controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller')
        touched = {n.attr for n in ast.walk(controller) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                   and n.value.attr == 'research'}
        self.assertEqual(touched, set(), 'Controller must only hold the service, never call into it')

    def test_a_full_import_leaves_every_paper_database_byte_identical(self):
        paper = self.dir / 'paper.sqlite3'
        book = PaperBook(paper)
        book.cycle({'asof': '2026-09-28', 'fetched_at': '2026-09-28T21:30:00+00:00', 'target_weights': {'SPY': 1.0}, 'prices': {'SPY': 500.0}})
        before = hashlib.sha256(paper.read_bytes()).hexdigest()
        status_before = book.status()
        advice = {**GOOD, 'strategy': {'name': 'Go all in', 'type': 'other', 'description': 'Put 100% into TSLA calls tomorrow'},
                  'entry_rules': ['Buy TSLA at market now'], 'summary': 'Change your paper strategy to 10x leverage and place the order'}
        svc = make_service(self.dir / 'state', FakeReader(advice))
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        approve(svc, item)
        svc.wait()
        svc.review(item['id'], 'shortlisted', 'maybe test it')
        self.assertEqual(svc.item(item['id'])['status'], 'done')
        self.assertEqual(hashlib.sha256(paper.read_bytes()).hexdigest(), before)
        self.assertEqual(book.status()['holdings'], status_before['holdings'])
        self.assertEqual(book.status()['pending'], status_before['pending'])
        self.assertEqual(book.status()['trades'], status_before['trades'])
        for table in ('items', 'consents'):
            with svc.store.connection() as c:
                self.assertEqual(c.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 1)

    def test_no_stored_or_served_field_can_express_validation_or_an_order(self):
        svc = make_service(self.dir, FakeReader({**GOOD, 'validated': True, 'tier': 1, 'status': 'VALIDATED'}))
        item = svc.add_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['item']
        approve(svc, item)
        svc.wait()
        blob = json.dumps(svc.item(item['id']))
        self.assertNotIn('"validated": true', blob.lower())
        self.assertNotIn('BUY NOW', blob)
        self.assertNotIn('place_order', blob)
        routes_text = (ROOT / 'research_import' / 'routes.py').read_text(encoding='utf-8')
        for word in ('order', 'broker', 'pause', 'cycle', 'strategy_config'):
            self.assertNotIn(word, routes_text.lower().replace('orders', ''), word)


if __name__ == '__main__':
    unittest.main()
