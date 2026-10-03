"""Uploaded and downloaded media: what is accepted, how big, and how it is held.

A file is judged by three independent things that must agree: its extension, the declared
content type and its leading bytes. The stored name is never derived from the upload, so a
hostile file name cannot reach the file system. Staged files live in one private folder,
are removed after processing, on discard and when they expire, and the folder is bounded.
"""
import hashlib
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import unquote

MB = 1024 * 1024
MAX_VIDEO_BYTES = 80 * MB
MAX_IMAGE_BYTES = 12 * MB
MAX_STAGED_FILES = 10
MAX_STAGED_BYTES = 400 * MB
STAGING_TTL_SECONDS = 6 * 3600
ORPHAN_AGE_SECONDS = 3600
UPLOAD_DEADLINE_SECONDS = 600

# extension -> (family, media type, size limit)
TYPES = {
    '.mp4': ('video', 'video/mp4', MAX_VIDEO_BYTES), '.m4v': ('video', 'video/x-m4v', MAX_VIDEO_BYTES),
    '.mov': ('video', 'video/quicktime', MAX_VIDEO_BYTES), '.webm': ('video', 'video/webm', MAX_VIDEO_BYTES),
    '.mkv': ('video', 'video/x-matroska', MAX_VIDEO_BYTES),
    '.jpg': ('image', 'image/jpeg', MAX_IMAGE_BYTES), '.jpeg': ('image', 'image/jpeg', MAX_IMAGE_BYTES),
    '.png': ('image', 'image/png', MAX_IMAGE_BYTES), '.webp': ('image', 'image/webp', MAX_IMAGE_BYTES),
}
GENERIC_TYPES = {'application/octet-stream', 'binary/octet-stream'}
SUPPORTED_TEXT = 'mp4, m4v, mov, webm or mkv video up to 80 MB, or jpg, png or webp image up to 12 MB'
_QT_ATOMS = {b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip'}


class UploadRejected(ValueError):
    """The file cannot be accepted. The message is written for the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def safe_display_name(raw):
    """The file's base name for display only: decoded, control-free, bounded. Never used as a path."""
    name = unquote(raw or '').replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '', name).strip(' .')
    return name[:120]


def classify_name(name, content_type):
    """(extension, family, media type, size limit) for an upload, or UploadRejected."""
    name = safe_display_name(name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in TYPES:
        raise UploadRejected(f'That file type is not supported. Use a {SUPPORTED_TEXT}.', 415)
    family, media, limit = TYPES[ext]
    declared = (content_type or '').split(';')[0].strip().lower()
    if declared not in GENERIC_TYPES and declared != media and not (declared.startswith(family + '/') and ext != '.mkv'):
        raise UploadRejected('The declared file type does not match the file extension.', 415)
    return ext, family, media, limit


def matches_signature(head, ext):
    """True when the leading bytes agree with the extension's format."""
    if ext in ('.mp4', '.m4v'):
        return head[4:8] == b'ftyp'
    if ext == '.mov':
        return head[4:8] in _QT_ATOMS
    if ext in ('.webm', '.mkv'):
        return head[:4] == b'\x1a\x45\xdf\xa3' and (b'webm' if ext == '.webm' else b'matroska') in head[:64]
    if ext in ('.jpg', '.jpeg'):
        return head[:3] == b'\xff\xd8\xff'
    if ext == '.png':
        return head[:8] == b'\x89PNG\r\n\x1a\n'
    if ext == '.webp':
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    return False


def verify_file(path, ext):
    with open(path, 'rb') as handle:
        if not matches_signature(handle.read(64), ext):
            raise UploadRejected('The file contents do not match its extension, so it was not accepted.', 415)


class Staging:
    """One private folder of files waiting for approval."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def path_for(self, item_id, ext):
        return self.root / f'{item_id}{ext}'

    def usage(self):
        files = [p for p in self.root.iterdir() if p.is_file()]
        return len(files), sum(p.stat().st_size for p in files)

    def check_room(self, size):
        count, total = self.usage()
        if count >= MAX_STAGED_FILES:
            raise UploadRejected(f'{count} files are already waiting for approval. Approve or discard one first.', 409)
        if total + size > MAX_STAGED_BYTES:
            raise UploadRejected('The waiting files use their space limit. Approve or discard one first.', 409)

    def receive(self, stream, length, ext, *, item_id=None, clock=time.monotonic):
        """Copy exactly `length` bytes from `stream` into a new private file.

        Returns (path, sha256, item_id). Any failure removes the partial file. A short body
        (the client stopped sending) is an error, never a truncated accepted file.
        """
        item_id = item_id or uuid.uuid4().hex
        part = self.root / f'{item_id}.part'
        final = self.path_for(item_id, ext)
        digest, remaining, started = hashlib.sha256(), length, clock()
        try:
            with open(part, 'wb') as out:
                while remaining:
                    if clock() - started > UPLOAD_DEADLINE_SECONDS:
                        raise UploadRejected('The upload took too long and was stopped.', 408)
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise UploadRejected('The upload ended before the whole file arrived.')
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
            verify_file(part, ext)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return final, digest.hexdigest(), item_id

    def remove(self, path):
        if path:
            Path(path).unlink(missing_ok=True)

    def sweep(self, known_paths, now=None, max_age=ORPHAN_AGE_SECONDS):
        """Delete files nothing refers to (crashed uploads, removed items). Returns how many."""
        now, removed = now or time.time(), 0
        known = {str(Path(p)) for p in known_paths if p}
        for path in self.root.iterdir():
            if path.is_file() and str(path) not in known and now - path.stat().st_mtime > max_age:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
