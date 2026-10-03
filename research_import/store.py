"""Private local store for imported trading research.

One SQLite file plus a staging folder, both under the runtime folder the repository
already keeps out of git. Nothing here touches a paper book, the collector or any
order path. Rows hold the source, the consent record, the validated reading and the
user's own review state. `validated` is constrained to 0 by the schema itself, so no
code path, reader or request can mark an imported item validated.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .schema import NOTICE_VERSION, PROVIDER, REVIEW_STATES

STATUSES = ('pending_consent', 'processing', 'done', 'no_research_found', 'error', 'expired')
# Statuses from which the user may approve a model read. Approval is always for one item.
APPROVABLE = ('pending_consent', 'error')
ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
NOTE_LIMIT = 2000
WAITING = 'Waiting for your approval. Nothing has been sent anywhere.'
SEARCH_LIMIT = 200
MAX_ITEMS = 1000


class StoreError(Exception):
    """A request the store refuses. `status` is the HTTP status the route should use."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def utc_now():
    return datetime.now(timezone.utc)


def fingerprint(item):
    """Identifies exactly what an approval covers: this item, this source, this notice version."""
    text = f"{item['id']}|{item['source_key']}|{item['content_sha256'] or ''}|{NOTICE_VERSION}"
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def search_text(item, reading):
    parts = [item.get('source_url'), item.get('filename'), item.get('platform'), item.get('title')]
    if reading:
        strategy = reading.get('strategy') or {}
        parts += [reading.get('title'), (reading.get('creator') or {}).get('name'), (reading.get('creator') or {}).get('handle'),
                  reading.get('summary'), strategy.get('name'), strategy.get('type'), strategy.get('description'),
                  *reading.get('instruments', []), *reading.get('timeframes', []), *reading.get('missing_details', [])]
        for key in ('entry_rules', 'exit_rules', 'risk_rules'):
            parts += [r['text'] for r in reading.get(key, [])]
        parts += [f"{n['label']} {n['value']}" for n in reading.get('stated_numbers', [])]
        parts += [c['claim'] for c in reading.get('claimed_returns', [])]
    return ' '.join(str(p) for p in parts if p).lower()


class ResearchStore:
    def __init__(self, root, clock=utc_now):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.path = self.root / 'research-imports.sqlite3'
        self.clock = clock
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS items(
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK(kind IN ('link','upload')),
              source_key TEXT NOT NULL UNIQUE,
              platform TEXT, source_url TEXT, filename TEXT, media_type TEXT, size_bytes INTEGER, content_sha256 TEXT,
              staged_path TEXT, staged_expires_at TEXT,
              status TEXT NOT NULL CHECK(status IN ('pending_consent','processing','done','no_research_found','error','expired')),
              message TEXT, retryable INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
              review_state TEXT NOT NULL DEFAULT 'new' CHECK(review_state IN ('new','reviewed','shortlisted','dismissed')),
              review_note TEXT,
              validated INTEGER NOT NULL DEFAULT 0 CHECK(validated=0),
              title TEXT, creator TEXT, result_json TEXT, search_text TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, processed_at TEXT);
            CREATE INDEX IF NOT EXISTS items_status ON items(status);
            CREATE TABLE IF NOT EXISTS consents(
              id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, source_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
              notice_version TEXT NOT NULL, provider TEXT NOT NULL, granted_at TEXT NOT NULL, outcome TEXT);''')

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def _stamp(self):
        return self.clock().isoformat()

    # ---------------------------------------------------------------- reads
    def get(self, item_id):
        if not isinstance(item_id, str) or not ID_PATTERN.match(item_id):
            raise StoreError('Unknown item.', 404)
        with self.connection() as c:
            row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise StoreError('Unknown item.', 404)
        return dict(row)

    def by_key(self, key):
        with self.connection() as c:
            row = c.execute('SELECT * FROM items WHERE source_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def list(self, query='', status='', review='', limit=SEARCH_LIMIT):
        """Items newest first. Every search term must appear somewhere in the item's text."""
        clauses, args = [], []
        for term in str(query or '').lower().split()[:8]:
            escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            args.append(f'%{escaped}%')
        if status:
            if status not in STATUSES:
                raise StoreError('Unknown status filter.')
            clauses.append('status=?')
            args.append(status)
        if review:
            if review not in REVIEW_STATES:
                raise StoreError('Unknown review filter.')
            clauses.append('review_state=?')
            args.append(review)
        sql = 'SELECT * FROM items' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY created_at DESC, id LIMIT ?'
        with self.connection() as c:
            return [dict(r) for r in c.execute(sql, (*args, max(1, min(int(limit), SEARCH_LIMIT))))]

    def counts(self):
        with self.connection() as c:
            rows = c.execute('SELECT status, count(*) n FROM items GROUP BY status').fetchall()
        return {r['status']: r['n'] for r in rows}

    def staged_paths(self):
        with self.connection() as c:
            return [r[0] for r in c.execute('SELECT staged_path FROM items WHERE staged_path IS NOT NULL')]

    # ---------------------------------------------------------------- writes
    def _room(self, c):
        if c.execute('SELECT count(*) FROM items').fetchone()[0] >= MAX_ITEMS:
            raise StoreError(f'The import library holds {MAX_ITEMS} items. Delete some before adding more.', 409)

    def add_link(self, source):
        """(item, created). An existing item for the same source is returned untouched."""
        now = self._stamp()
        item_id = uuid.uuid4().hex
        try:
            with self.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                if not c.execute('SELECT 1 FROM items WHERE source_key=?', (source.key,)).fetchone():
                    self._room(c)
                c.execute('''INSERT INTO items(id,kind,source_key,platform,source_url,status,message,created_at,updated_at,search_text)
                             VALUES(?,?,?,?,?,?,?,?,?,?)''',
                          (item_id, 'link', source.key, source.platform, source.url, 'pending_consent',
                           WAITING, now, now,
                           search_text({'source_url': source.url, 'platform': source.platform}, None)))
        except sqlite3.IntegrityError:
            return self.by_key(source.key), False
        return self.get(item_id), True

    def add_upload(self, item_id, *, key, filename, media_type, size, sha256, staged_path, expires_at):
        """(item, created). A duplicate of content already held returns the existing item.

        An expired or failed upload of the same content is re-staged in place, so it keeps one identity.
        """
        now = self._stamp()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT * FROM items WHERE source_key=?', (key,)).fetchone()
            if existing and existing['status'] not in ('expired', 'error'):
                return dict(existing), False
            if existing:
                item_id = existing['id']
                c.execute("""UPDATE items SET filename=?,media_type=?,size_bytes=?,staged_path=?,staged_expires_at=?,status='pending_consent',
                             message=?,updated_at=? WHERE id=?""",
                          (filename, media_type, size, str(staged_path), expires_at, WAITING, now, item_id))
            else:
                self._room(c)
                c.execute("""INSERT INTO items(id,kind,source_key,platform,filename,media_type,size_bytes,content_sha256,staged_path,
                             staged_expires_at,status,message,created_at,updated_at,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (item_id, 'upload', key, 'upload', filename, media_type, size, sha256, str(staged_path), expires_at,
                           'pending_consent', WAITING, now, now, search_text({'filename': filename, 'platform': 'upload'}, None)))
        return self.get(item_id), True

    def begin_processing(self, item_id, presented_fingerprint):
        """Record explicit consent for this one item and mark it processing, atomically.

        The presented fingerprint must equal the one computed from the stored item, so a
        consent given for anything else (another item, an older notice, a replaced file) fails.
        """
        now = self._stamp()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise StoreError('Unknown item.', 404)
            item = dict(row)
            if item['status'] not in APPROVABLE:
                raise StoreError(f"This item is {item['status'].replace('_', ' ')} and cannot be approved now.", 409)
            if item['kind'] == 'upload' and not (item['staged_path'] and Path(item['staged_path']).is_file()):
                raise StoreError('The staged file is gone. Upload it again.', 409)
            if not isinstance(presented_fingerprint, str) or not hmac.compare_digest(presented_fingerprint, fingerprint(item)):
                raise StoreError('That approval is not for this item as it stands now. Review the notice again.', 409)
            c.execute('INSERT INTO consents(item_id,source_key,fingerprint,notice_version,provider,granted_at) VALUES(?,?,?,?,?,?)',
                      (item_id, item['source_key'], presented_fingerprint, NOTICE_VERSION, PROVIDER, now))
            c.execute("UPDATE items SET status='processing',message=?,attempts=attempts+1,updated_at=? WHERE id=?",
                      ('Approved. Reading the source with Gemini…', now, item_id))
        return self.get(item_id)

    def finish(self, item_id, status, message, *, reading=None, result=None, retryable=True, clear_staged=True):
        if status not in ('done', 'no_research_found', 'error', 'expired'):
            raise ValueError(status)
        now = self._stamp()
        item = self.get(item_id)
        title = (reading or {}).get('title') or None
        creator = ((reading or {}).get('creator') or {}).get('handle') or ((reading or {}).get('creator') or {}).get('name') or None
        with self.connection() as c:
            c.execute('''UPDATE items SET status=?,message=?,retryable=?,title=?,creator=?,result_json=?,search_text=?,
                         staged_path=CASE WHEN ? THEN NULL ELSE staged_path END,
                         staged_expires_at=CASE WHEN ? THEN NULL ELSE staged_expires_at END,
                         updated_at=?,processed_at=? WHERE id=?''',
                      (status, message, int(retryable), title, creator, json.dumps(result) if result else None,
                       search_text({**item, 'title': title}, reading), int(clear_staged), int(clear_staged), now, now, item_id))
            c.execute('UPDATE consents SET outcome=? WHERE id=(SELECT max(id) FROM consents WHERE item_id=?)', (status, item_id))
        # The staged file this call released, so the caller can delete it even if it never learned the path.
        return item['staged_path'] if clear_staged else None

    def set_review(self, item_id, state, note):
        if state not in REVIEW_STATES:
            raise StoreError('Review state must be one of: ' + ', '.join(REVIEW_STATES) + '.')
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_LIMIT):
            raise StoreError(f'A note can be up to {NOTE_LIMIT} characters.')
        item = self.get(item_id)
        with self.connection() as c:
            c.execute('UPDATE items SET review_state=?,review_note=?,updated_at=? WHERE id=?',
                      (state, (note if note is not None else item['review_note']), self._stamp(), item_id))
        return self.get(item_id)

    def delete(self, item_id):
        """Remove an item and its content; the consent audit rows stay. Returns the staged path to delete."""
        item = self.get(item_id)
        if item['status'] == 'processing':
            raise StoreError('This item is being read. Wait for it to finish.', 409)
        with self.connection() as c:
            c.execute('DELETE FROM items WHERE id=?', (item_id,))
        return item['staged_path']

    def expire_staged(self, now=None):
        """Mark upload items whose staged file is past its expiry or missing. Returns the paths to delete."""
        now = (now or self.clock()).isoformat()
        stale = []
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT id,staged_path,staged_expires_at FROM items WHERE kind='upload' AND status='pending_consent'").fetchall():
                gone = not (row['staged_path'] and Path(row['staged_path']).is_file())
                if gone or (row['staged_expires_at'] and row['staged_expires_at'] <= now):
                    c.execute("UPDATE items SET status='expired',staged_path=NULL,staged_expires_at=NULL,message=?,updated_at=? WHERE id=?",
                              ('The staged file expired or was removed. Upload it again to continue.', now, row['id']))
                    if row['staged_path']:
                        stale.append(row['staged_path'])
        return stale

    def recover(self, cause='The server restarted during the read.'):
        """Settle items still marked processing when no read can be running.

        Called at startup and whenever the service holds its read lock with nothing in flight, so an
        item can never stay 'processing' forever. A link becomes a retryable error; an upload's staged
        file is dropped and it must be uploaded again. Neither retries by itself: a new read needs a
        new approval. Returns the staged paths the caller should delete.
        """
        stale = []
        now = self._stamp()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT id,kind,staged_path FROM items WHERE status='processing'").fetchall():
                if row['kind'] == 'upload':
                    c.execute("UPDATE items SET status='expired',staged_path=NULL,staged_expires_at=NULL,message=?,updated_at=? WHERE id=?",
                              (f'{cause} Upload the file again and approve it again.', now, row['id']))
                    if row['staged_path']:
                        stale.append(row['staged_path'])
                else:
                    c.execute("UPDATE items SET status='error',message=?,updated_at=? WHERE id=?",
                              (f'{cause} Approve it again to retry.', now, row['id']))
                c.execute("UPDATE consents SET outcome='interrupted' WHERE outcome IS NULL AND id=(SELECT max(id) FROM consents WHERE item_id=?)",
                          (row['id'],))
        return stale

    def abort_start(self, item_id, message):
        """The approved read could not even start, so nothing was sent anywhere.

        Puts the item back to waiting for approval with its staged file kept. The approval is spent
        (its consent row is marked not_started), so reading it needs a new, explicit approval.
        Returns True if the item was put back.
        """
        now = self._stamp()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            moved = c.execute("UPDATE items SET status='pending_consent',message=?,updated_at=? WHERE id=? AND status='processing'",
                              (message, now, item_id)).rowcount
            if moved:
                c.execute("UPDATE consents SET outcome='not_started' WHERE outcome IS NULL AND id=(SELECT max(id) FROM consents WHERE item_id=?)",
                          (item_id,))
        return bool(moved)

    def consents(self, item_id=None):
        with self.connection() as c:
            if item_id:
                rows = c.execute('SELECT * FROM consents WHERE item_id=? ORDER BY id', (item_id,)).fetchall()
            else:
                rows = c.execute('SELECT * FROM consents ORDER BY id').fetchall()
        return [dict(r) for r in rows]
