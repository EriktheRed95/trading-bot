"""Which pasted links may be imported, and the one canonical form used for deduplication.

Accepted: public HTTPS links on YouTube, Instagram and TikTok, and direct HTTPS links to a
media file on a public host. Everything else is refused before any network request. This
module never touches the network; DNS and address checks for direct links are made by
safe_fetch at the moment a file would actually be fetched.
"""
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
MEDIA_EXTENSIONS = ('.mp4', '.m4v', '.mov', '.webm', '.mkv', '.jpg', '.jpeg', '.png', '.webp')
SUPPORTED = ('public HTTPS links to YouTube videos and Shorts, Instagram posts, reels and carousels, '
             'TikTok videos, or a direct HTTPS link to a video (mp4, m4v, mov, webm, mkv) or image (jpg, png, webp) file')

HOSTS = {
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'},
    'instagram': {'instagram.com', 'www.instagram.com', 'instagr.am', 'www.instagr.am'},
    'tiktok': {'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'},
}
_YOUTUBE_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_CODE = re.compile(r'^[A-Za-z0-9_-]{5,40}$')
_DIGITS = re.compile(r'^\d{6,25}$')
_INSTAGRAM_PATH = re.compile(r'^/(?:[A-Za-z0-9_.]{1,30}/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,40})/?$')
_TIKTOK_VIDEO = re.compile(r'^/@[A-Za-z0-9_.]{1,40}/video/(\d{6,25})/?$')
_TIKTOK_SHORT = re.compile(r'^/([A-Za-z0-9_-]{5,20})/?$')
_HOSTNAME = re.compile(r'^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$')
_BAD_SUFFIXES = ('.local', '.localhost', '.internal', '.home', '.lan', '.corp', '.intranet', '.localdomain', '.arpa', '.onion')


class UrlRejected(ValueError):
    """The link cannot be imported. The message is written for the user."""


@dataclass(frozen=True)
class Source:
    platform: str        # youtube | instagram | tiktok | direct
    kind: str            # video | short | post | carousel_or_video | media
    url: str             # canonical URL used for every later request
    key: str             # deduplication key, stable across URL spellings where the URL carries an id
    display: str


def public_hostname(host):
    """True for a syntactically public DNS name: ASCII labels, an alphabetic TLD, not an IP in any notation.

    An alphabetic TLD rules out dotted, decimal, hex and octal IP spellings, which some resolvers accept.
    Whether the name resolves to a public address is checked separately, when a file is fetched.
    """
    host = host.lower()
    return bool(_HOSTNAME.match(host)) and not host.endswith(_BAD_SUFFIXES)


def _split(raw):
    if not isinstance(raw, str):
        raise UrlRejected('Paste a link as text.')
    raw = raw.strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise UrlRejected(f'Paste a link of up to {MAX_URL_LENGTH} characters.')
    if not raw.isascii() or re.search(r'[\s\x00-\x1f\x7f\\]', raw):
        raise UrlRejected('The link contains spaces, control characters, backslashes or non-ASCII characters. '
                          'Copy the address again, using its punycode form for international domain names.')
    try:
        parts = urlsplit(raw)
        parts.port  # raises for a malformed port
    except ValueError as exc:
        raise UrlRejected('The link is not a valid web address.') from exc
    if parts.scheme != 'https':
        raise UrlRejected('Only HTTPS links can be imported.')
    if '@' in parts.netloc or parts.username or parts.password:
        raise UrlRejected('Links with embedded credentials are refused.')
    if parts.port not in (None, 443):
        raise UrlRejected('Links with a custom port are refused.')
    host = (parts.hostname or '').lower()
    if not host:
        raise UrlRejected('The link has no host name.')
    return parts, host


def public_https(raw):
    """(host, path-and-query) of an HTTPS URL on a public DNS name, or UrlRejected. No extension rule."""
    parts, host = _split(raw)
    if not public_hostname(host):
        raise UrlRejected('Only links on a public domain name can be fetched. IP addresses, localhost and internal names are refused.')
    return host, (parts.path or '/') + (f'?{parts.query}' if parts.query else '')


def classify(raw):
    """Canonical Source for a pasted link, or UrlRejected."""
    parts, host = _split(raw)
    path, query = parts.path, parse_qs(parts.query)

    if host in HOSTS['youtube']:
        if host == 'youtu.be':
            vid = path.strip('/').split('/')[0] if path.count('/') <= 2 else ''
            kind = 'video'
        elif path == '/watch':
            vid, kind = (query.get('v') or [''])[0], 'video'
        else:
            match = re.fullmatch(r'/(shorts|live|embed)/([A-Za-z0-9_-]{11})/?', path)
            vid, kind = (match.group(2) if match else ''), ('short' if match and match.group(1) == 'shorts' else 'video')
        if not _YOUTUBE_ID.match(vid or ''):
            raise UrlRejected('That YouTube link does not point at one video. Playlists and channels are not imported.')
        return Source('youtube', kind, f'https://www.youtube.com/watch?v={vid}', f'youtube:{vid}', f'YouTube video {vid}')

    if host in HOSTS['instagram']:
        match = _INSTAGRAM_PATH.match(path)
        if not match:
            raise UrlRejected('Only public Instagram post, reel and carousel links can be imported. Stories and profiles need a login.')
        segment, code = match.groups()
        segment = 'reel' if segment == 'reels' else segment
        return Source('instagram', 'carousel_or_video', f'https://www.instagram.com/{segment}/{code}/',
                      f'instagram:{code}', f'Instagram post {code}')

    if host in HOSTS['tiktok']:
        if '/photo/' in path:
            raise UrlRejected('TikTok photo carousels are not supported. Link a TikTok video instead.')
        if host in ('vm.tiktok.com', 'vt.tiktok.com'):
            match = _TIKTOK_SHORT.match(path)
            if not match:
                raise UrlRejected('That TikTok short link is not recognised.')
            code = match.group(1)
            return Source('tiktok', 'video', f'https://{host}/{code}/', f'tiktok-short:{code}', f'TikTok video {code}')
        match = _TIKTOK_VIDEO.match(path)
        if not match:
            raise UrlRejected('Only links to a single public TikTok video can be imported.')
        vid = match.group(1)
        return Source('tiktok', 'video', f'https://www.tiktok.com{path.rstrip("/")}', f'tiktok:{vid}', f'TikTok video {vid}')

    if not public_hostname(host):
        raise UrlRejected('Only links on a public domain name can be imported. IP addresses, localhost and internal names are refused.')
    if not path.lower().endswith(MEDIA_EXTENSIONS):
        raise UrlRejected(f'That address is not one of the supported sources. Supported: {SUPPORTED}.')
    canonical = urlunsplit(('https', host, path, parts.query, ''))
    digest = hashlib.sha256(canonical.encode('ascii')).hexdigest()[:40]
    return Source('direct', 'media', canonical, f'url:{digest}', f'{host}{path[-40:]}')
