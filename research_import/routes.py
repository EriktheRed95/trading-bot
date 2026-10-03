"""HTTP glue for the dashboard server: plain functions from a request to (status, payload).

The server (trading_app.py) does the request authentication (loopback Host and client, Origin,
per-process token); these functions only route and translate errors. They hold no state and
reach no trading object.
"""
import logging
import re
from urllib.parse import parse_qs

from .store import ID_PATTERN, StoreError

PREFIX = '/api/research-import'
MAX_JSON_BYTES = 8192
DRAIN_LIMIT = 1024 * 1024
LOG = logging.getLogger('research_import')
_ITEM = re.compile(r'^/api/research-import/items/([0-9a-f]{32})$')


def owns(path):
    return path == PREFIX or path.startswith(PREFIX + '/')


def _guard(call):
    try:
        return 200, call()
    except StoreError as exc:
        return exc.status, {'error': str(exc)}
    except Exception as exc:   # never echo internals to the browser
        LOG.warning('research import route failed (%s)', type(exc).__name__)
        return 500, {'error': 'The request could not be completed.'}


def handle_get(service, path, query):
    """GET routes. `query` is the raw query string."""
    if service is None:
        return 200, {'enabled': False}
    if path == f'{PREFIX}/status':
        return _guard(service.status)
    if path == f'{PREFIX}/items':
        params = parse_qs(query)
        first = lambda name: (params.get(name) or [''])[0][:200]
        return _guard(lambda: {'items': service.items(first('q'), first('status'), first('review'))})
    match = _ITEM.match(path)
    if match:
        return _guard(lambda: service.item(match.group(1)))
    return 404, {'error': 'Not found'}


def handle_post(service, path, payload):
    """JSON POST routes."""
    if service is None:
        return 404, {'error': 'Research import is not enabled.'}
    if path == f'{PREFIX}/link':
        if not isinstance(payload.get('url'), str):
            return 400, {'error': 'Paste a link.'}
        return _guard(lambda: service.add_link(payload['url']))
    if path == f'{PREFIX}/approve':
        return _guard(lambda: service.approve(payload))
    if path == f'{PREFIX}/review':
        item_id = payload.get('id')
        if not isinstance(item_id, str) or not ID_PATTERN.match(item_id):
            return 400, {'error': 'Unknown item.'}
        return _guard(lambda: service.review(item_id, payload.get('state'), payload.get('note')))
    if path == f'{PREFIX}/delete':
        item_id = payload.get('id')
        if not isinstance(item_id, str) or not ID_PATTERN.match(item_id):
            return 400, {'error': 'Unknown item.'}
        return _guard(lambda: service.delete(item_id))
    return 404, {'error': 'Not found'}


class _Counted:
    """Counts what the service actually read, so a refused upload can be drained by exactly the unread rest."""

    def __init__(self, stream):
        self.stream, self.consumed = stream, 0

    def read(self, n):
        data = self.stream.read(n)
        self.consumed += len(data)
        return data


def drain(stream, length, limit=DRAIN_LIMIT):
    """Read and discard an unread request body of at most `limit` bytes.

    Replying and closing with unread data makes the client see a connection reset instead of
    the explanation, so a refused request's small body is consumed first. A larger body is left
    alone (never waited for): that client gets the reset, which is why the page checks file
    sizes before sending.
    """
    left = max(length, 0)
    if left > limit:
        return
    try:
        while left > 0:
            chunk = stream.read(min(64 * 1024, left))
            if not chunk:
                return
            left -= len(chunk)
    except OSError:
        return


def handle_upload(service, headers, rfile):
    """Raw-body upload: Content-Length and X-Filename headers, file bytes as the body."""
    declared = str(headers.get('Content-Length', ''))
    length = int(declared) if declared.isdigit() else None
    if service is None:
        drain(rfile, length or 0)
        return 404, {'error': 'Research import is not enabled.'}
    if length is None:
        return 411, {'error': 'The upload needs a Content-Length.'}
    counted = _Counted(rfile)
    status, payload = _guard(lambda: service.add_upload(counted, length, headers.get('X-Filename', ''), headers.get('Content-Type', '')))
    if status != 200:
        drain(rfile, length - counted.consumed)
    return status, payload
