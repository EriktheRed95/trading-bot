"""Import workflow: stage, approve one item, read, validate, store, review.

Nothing is sent to an outside service until the user approves one specific item, and the
approval covers that item alone. Staging a link or file only validates it and records it
as pending. This module imports no trading code: it cannot change a paper strategy, reach
a paper book, queue an order or mark anything validated.
"""
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from . import files
from .reader import GeminiSkillReader, Media, ReaderUnavailable, ReadFailure
from .safe_fetch import FetchBlocked, SafeFetcher
from .schema import NOTICE_VERSION, PROVIDER, ReadingInvalid, assess_evidence, json_from_text, validate_reading
from .store import APPROVABLE, ResearchStore, StoreError, fingerprint, utc_now
from .urls import SUPPORTED, UrlRejected, classify

LOG = logging.getLogger('research_import')
APPROVAL_KEYS = {'id', 'acknowledged', 'fingerprint', 'notice_version'}
IMPORT_LABEL = ('Imported source material: unverified and not validated. It changes no paper strategy, '
                'places no order and is not a finding until you test it yourself.')
WORK_AGE_SECONDS = 3600


def media_family(name):
    return files.TYPES.get(os.path.splitext(name.lower())[1], ('video',))[0]


class ResearchImports:
    def __init__(self, root, *, reader=None, fetcher=None, clock=utc_now, env=None):
        self.root = Path(root)
        self.clock = clock
        self.store = ResearchStore(self.root, clock)
        self.staging = files.Staging(self.root / 'staging')
        self.work = self.root / 'work'
        self.work.mkdir(parents=True, exist_ok=True)
        self.reader = reader or GeminiSkillReader(env)
        self.fetcher = fetcher or SafeFetcher()
        self._busy = threading.Lock()
        self._staging_lock = threading.Lock()   # one upload at a time keeps the staging limits exact
        self._worker = None
        self._drop(self.store.recover())
        self.housekeeping()

    # ------------------------------------------------------------ upkeep
    def _drop(self, paths):
        for path in paths:
            self.staging.remove(path)

    def reap_stuck(self):
        """Settle any item still 'processing' while no read is running. Returns how many were settled.

        A read marks its item processing only while holding the read lock, so if the lock can be taken
        here nothing legitimate is in flight. This is the backstop for a failure that left the item
        unsettled (for example the store itself failing at the moment a read ended).
        """
        if not self._busy.acquire(blocking=False):
            return 0
        try:
            if not self.store.counts().get('processing'):
                return 0
            stale = self.store.recover('The read did not finish and its state could not be saved.')
            self._drop(stale)
            return max(1, len(stale))
        finally:
            self._busy.release()

    def housekeeping(self):
        """Expire staged files, remove orphans and leftover work folders. Safe to call often."""
        try:
            self.reap_stuck()
        except Exception as exc:   # upkeep must never break a listing; the next call tries again
            LOG.warning('research import could not settle stuck items (%s)', type(exc).__name__)
        self._drop(self.store.expire_staged())
        self.staging.sweep(self.store.staged_paths())
        now = time.time()
        for entry in self.work.iterdir():
            if entry.is_dir() and now - entry.stat().st_mtime > WORK_AGE_SECONDS and not self._busy.locked():
                shutil.rmtree(entry, ignore_errors=True)

    # ------------------------------------------------------------ reading state
    def status(self):
        caps = self.reader.capabilities()
        return {'enabled': True, 'mode': 'IMPORTED RESEARCH · NEVER CHANGES A PAPER STRATEGY OR PLACES AN ORDER',
                'capabilities': caps, 'provider': PROVIDER, 'notice_version': NOTICE_VERSION, 'label': IMPORT_LABEL,
                'supported': SUPPORTED,
                'limits': {'video_mb': files.MAX_VIDEO_BYTES // files.MB, 'image_mb': files.MAX_IMAGE_BYTES // files.MB,
                           'staged_files': files.MAX_STAGED_FILES, 'staging_hours': files.STAGING_TTL_SECONDS // 3600,
                           'upload_types': files.SUPPORTED_TEXT},
                'counts': self.store.counts(), 'busy': self._busy.locked()}

    def items(self, query='', status='', review=''):
        self.housekeeping()
        return [self.public(i) for i in self.store.list(query, status, review)]

    def item(self, item_id):
        return self.public(self.store.get(item_id), detail=True)

    def public(self, item, detail=False):
        """The browser's view of an item: no paths, no stored file names on disk, consent notice only when approvable."""
        out = {k: item[k] for k in ('id', 'kind', 'platform', 'source_url', 'filename', 'media_type', 'size_bytes', 'status', 'message',
                                    'retryable', 'attempts', 'review_state', 'review_note', 'title', 'creator', 'created_at',
                                    'updated_at', 'processed_at')}
        out['validated'] = False
        out['content_sha256_prefix'] = (item['content_sha256'] or '')[:12] or None
        out['staged_expires_at'] = item['staged_expires_at']
        if item['status'] in APPROVABLE and not (item['kind'] == 'upload' and item['status'] == 'error'):
            out['consent'] = {'required': True, 'notice': self.notice(item), 'fingerprint': fingerprint(item),
                              'notice_version': NOTICE_VERSION, 'provider': PROVIDER}
        if detail and item['result_json']:
            out['result'] = json.loads(item['result_json'])
        if detail:
            out['consents'] = [{'granted_at': c['granted_at'], 'provider': c['provider'], 'notice_version': c['notice_version'],
                                'outcome': c['outcome']} for c in self.store.consents(item['id'])]
        return out

    def notice(self, item):
        if item['kind'] == 'upload':
            what = (f"this file, {item['filename']} ({item['size_bytes'] / 1_048_576:.1f} MB, fingerprint "
                    f"{(item['content_sha256'] or '')[:12]}), from this computer")
        elif item['platform'] == 'direct':
            what = f"the media file at {item['source_url']}, which this server downloads first,"
        else:
            what = f"the public video or images at {item['source_url']}"
        return (f"Send {what} to {PROVIDER}? The server reads it with the configured local reader using this computer's "
                f"API key, which may use API allowance. This approval covers this one item only; every other item needs its own. "
                f"Nothing is sent until you approve. The result is stored privately as unverified source material and changes no "
                f"paper strategy.")

    # ------------------------------------------------------------ staging
    def add_link(self, raw):
        try:
            source = classify(raw)
        except UrlRejected as exc:
            raise StoreError(str(exc), 400) from exc
        item, created = self.store.add_link(source)
        return {'item': self.public(item), 'duplicate': not created}

    def add_upload(self, stream, length, filename, content_type):
        try:
            ext, family, media, limit = files.classify_name(filename, content_type)
            if not isinstance(length, int) or length <= 0:
                raise files.UploadRejected('The upload needs a Content-Length and cannot be empty.', 411)
            if length > limit:
                raise files.UploadRejected(f'That {family} is larger than the {limit // files.MB} MB limit.', 413)
            with self._staging_lock:
                self.housekeeping()
                self.staging.check_room(length)
                path, sha, item_id = self.staging.receive(stream, length, ext)
                expires = (self.clock() + timedelta(seconds=files.STAGING_TTL_SECONDS)).isoformat()
                try:
                    item, created = self.store.add_upload(item_id, key=f'file:{sha}', filename=files.safe_display_name(filename) or f'upload{ext}',
                                                          media_type=media, size=length, sha256=sha, staged_path=path, expires_at=expires)
                except BaseException:
                    self.staging.remove(path)
                    raise
                if not created:
                    self.staging.remove(path)   # same content is already held: keep one copy and one identity
        except files.UploadRejected as exc:
            raise StoreError(str(exc), exc.status) from exc
        return {'item': self.public(item), 'duplicate': not created}

    # ------------------------------------------------------------ approval
    def _probe(self, item):
        if item['kind'] == 'upload':
            return Media('file', 'upload', path=item['staged_path'] or '', family=media_family(item['filename'] or ''))
        if item['platform'] == 'direct':
            return Media('file', 'direct', family=media_family(item['source_url'].split('?')[0]))
        return Media('link', item['platform'], url=item['source_url'])

    def approve(self, body):
        """Consent for exactly one item, then start its read. Nothing is recorded if the read cannot start."""
        if not isinstance(body, dict) or set(body) - APPROVAL_KEYS:
            raise StoreError('Approve one item at a time: send its id, the fingerprint you were shown and acknowledged=true.')
        if body.get('acknowledged') is not True:
            raise StoreError('Explicit approval for this item is required.')
        if body.get('notice_version') != NOTICE_VERSION:
            raise StoreError('The approval notice changed. Reload and review it again.', 409)
        item = self.store.get(body.get('id'))
        if item['status'] not in APPROVABLE:
            raise StoreError(f"This item is {item['status'].replace('_', ' ')} and cannot be approved now.", 409)
        try:
            self.reader.require(self._probe(item))
        except ReaderUnavailable as exc:
            raise StoreError(str(exc), 409) from exc
        if not self._busy.acquire(blocking=False):
            raise StoreError('Another item is being read. Wait for it to finish.', 409)
        try:
            claimed = self.store.begin_processing(item['id'], body.get('fingerprint'))
        except BaseException:
            self._busy.release()
            raise
        # From here the lock belongs to the worker, which releases it when it ends. If the worker cannot
        # even start, nothing was sent anywhere: put the item back and release the lock ourselves.
        try:
            worker = threading.Thread(target=self._process, args=(claimed['id'],), name='research-import', daemon=True)
            worker.start()
        except BaseException as exc:
            LOG.warning('research import could not start its worker (%s)', type(exc).__name__)
            try:
                self.store.abort_start(claimed['id'], 'The read could not be started, so nothing was sent. Approve it again to retry.')
            except Exception as inner:   # reap_stuck settles it on the next listing
                LOG.warning('research import could not restore an item after a failed start (%s)', type(inner).__name__)
            finally:
                self._busy.release()
            raise StoreError('The read could not be started, so nothing was sent. Approve it again to retry.', 503) from exc
        self._worker = worker
        return self.public(claimed)

    def wait(self, timeout=30):
        worker = self._worker
        if worker:
            worker.join(timeout)
        return not (worker and worker.is_alive())

    # ------------------------------------------------------------ worker
    def _settle(self, item_id, status, message, **kw):
        """Record how a read ended. If that cannot be saved, try once to record a plain error; never raise.

        Returns the staged file path the store released (or None), so cleanup can remove it.
        """
        try:
            return self.store.finish(item_id, status, message, **kw)
        except Exception as exc:
            LOG.warning('research import could not save a read result (%s)', type(exc).__name__)
        if status != 'error':
            try:
                return self.store.finish(item_id, 'error', 'The result could not be saved. No research was imported.', retryable=True)
            except Exception as exc:
                LOG.warning('research import could not save a read failure (%s)', type(exc).__name__)
        return None   # left processing; reap_stuck settles it as soon as the store works again

    def _process(self, item_id):
        """One approved read. Everything, including setup, is inside the boundary that ends by
        removing this item's own files and releasing the read lock, whatever fails."""
        workdir = staged = released = None
        try:
            try:
                item = self.store.get(item_id)
                staged = item.get('staged_path')
                workdir = Path(tempfile.mkdtemp(prefix='read-', dir=self.work))
                media = self._acquire(item, workdir)
                output = self.reader.read(media, workdir)
                reading = validate_reading(json_from_text(output.text))
                if reading is None:
                    released = self._settle(item_id, 'no_research_found',
                                            'No trading method or claim was found in this source, so nothing was imported.', retryable=False)
                else:
                    result = {'reading': reading, 'evidence': assess_evidence(reading), 'read_method': output.method, 'provider': PROVIDER,
                              'processed_at': self.clock().isoformat(), 'notice_version': NOTICE_VERSION, 'label': IMPORT_LABEL,
                              'source': {'platform': item['platform'], 'url': item['source_url'], 'filename': item['filename'],
                                         'creator': reading['creator']}}
                    released = self._settle(item_id, 'done', 'Read complete. Review the extraction; it is unverified source material.',
                                            reading=reading, result=result, retryable=False)
            except (ReaderUnavailable, ReadFailure, ReadingInvalid, FetchBlocked, files.UploadRejected) as exc:
                released = self._settle(item_id, 'error', str(exc), retryable=True)
            except Exception as exc:   # nothing about the failure may reach the browser beyond its type
                LOG.warning('research import failed unexpectedly (%s)', type(exc).__name__)
                released = self._settle(item_id, 'error', 'Reading did not complete. No research was imported.', retryable=True)
        finally:
            try:
                self._cleanup({staged, released} - {None}, workdir)
            finally:
                self._busy.release()

    def _cleanup(self, staged_paths, workdir):
        """Remove only the files created for this read. Each removal is independent and never raises."""
        for path in staged_paths:
            try:
                self.staging.remove(path)
            except Exception as exc:
                LOG.warning('research import could not remove a staged file (%s)', type(exc).__name__)   # swept later as an orphan
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    def _acquire(self, item, workdir):
        """The media to read. A direct link is downloaded first, through the SSRF guard, into the work folder."""
        if item['kind'] == 'upload':
            return Media('file', 'upload', path=item['staged_path'], family=media_family(item['filename']))
        if item['platform'] != 'direct':
            return Media('link', item['platform'], url=item['source_url'])
        ext = os.path.splitext(item['source_url'].split('?')[0].lower())[1]
        family, _, limit = files.TYPES[ext]
        target = workdir / f'direct{ext}'
        self.fetcher.download(item['source_url'], target, max_bytes=limit)
        try:
            files.verify_file(target, ext)
        except files.UploadRejected as exc:
            target.unlink(missing_ok=True)
            raise FetchBlocked('The downloaded file is not the media type its address claims.', 'signature') from exc
        return Media('file', 'direct', path=str(target), family=family)

    # ------------------------------------------------------------ review
    def review(self, item_id, state, note=None):
        return self.public(self.store.set_review(item_id, state, note), detail=True)

    def delete(self, item_id):
        path = self.store.delete(item_id)
        self.staging.remove(path)
        return {'deleted': True}
