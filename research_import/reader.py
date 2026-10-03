"""The boundary to the installed Gemini reader scripts.

This wraps two existing local tools instead of reimplementing them: `read_b.py` (video
reading, model discovery, quota handling) and `slide_reader.py` (validated Instagram image
fetching and multi-image reading). Both live in the user's local skills folder and are
imported or invoked unchanged. This module only builds fixed argument lists, applies the
same bounded process runner to every call and keeps the provider key away from everything
except the reader itself.

It is only ever called after the user approved one specific item. It reports setup
problems as ReaderUnavailable and failed reads as ReadFailure; both messages are written
for the user and never carry paths, tool output or keys.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil

from .process import PROVIDER_KEY_NAMES, child_environment, run_bounded
from .schema import READING_PROMPT, SLIDES_INSTRUCTION

HERE = Path(__file__).resolve().parent
READER_SECONDS = 12 * 60
DOWNLOAD_SECONDS = 10 * 60
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.webm', '.mkv'}
TERMINATIONS = {'timeout': 'exceeded its time limit and was stopped',
                'output_limit': 'produced more output than allowed and was stopped',
                'file_limit': 'downloaded more than the 80 MB limit and was stopped'}


class ReaderUnavailable(Exception):
    """The local reader is not set up. Nothing was sent anywhere."""


class ReadFailure(Exception):
    """A read did not complete. The message is safe to show the user."""


@dataclass(frozen=True)
class Media:
    kind: str                 # 'link' for youtube/instagram/tiktok, 'file' for a local path
    platform: str
    url: str = ''
    path: str = ''
    family: str = 'video'     # video | image (files only)


@dataclass(frozen=True)
class ReadOutput:
    text: str
    method: str               # video | slides


def default_paths(env):
    skills = Path(env['USERPROFILE']) / '.agents' / 'skills' if env.get('USERPROFILE') else None

    def under(*parts):
        return str(skills.joinpath(*parts)) if skills else ''
    return {'python': env.get('TRADING_RESEARCH_PYTHON') or 'python',
            'reader': env.get('TRADING_RESEARCH_READER') or under('video-to-skill', 'scripts', 'read_b.py'),
            'slide_reader': env.get('TRADING_RESEARCH_SLIDE_READER') or under('health-reference', 'scripts', 'slide_reader.py'),
            'downloader': env.get('TRADING_RESEARCH_DOWNLOADER') or 'yt-dlp'}


def _exists(path):
    return bool(path) and os.path.isfile(path)


class GeminiSkillReader:
    def __init__(self, env=None, run=run_bounded, which=shutil.which):
        self.env = dict(os.environ if env is None else env)
        self.run, self.which = run, which

    # ------------------------------------------------------------ setup
    def capabilities(self):
        """Booleans only. The key, its value and every local path stay on the server."""
        paths = default_paths(self.env)
        key = any(self.env.get(n) for n in PROVIDER_KEY_NAMES)
        reader = _exists(paths['reader'])
        slides = _exists(paths['slide_reader']) and _exists(str(HERE / 'slides_bridge.py'))
        downloader = bool(self.which(paths['downloader']))
        missing = []
        if not key:
            missing.append('a Gemini API key in the server environment')
        if not reader:
            missing.append('the local video reader script')
        return {'configured': key and reader, 'provider': 'Google Gemini', 'api_key_present': key, 'video_reader_found': reader,
                'image_reader_found': slides, 'downloader_found': downloader, 'missing': missing,
                'platforms': {'youtube': key and reader, 'instagram': key and reader and downloader,
                              'tiktok': key and reader and downloader, 'uploaded_video': key and reader,
                              'uploaded_image': key and slides}}

    def require(self, media):
        caps = self.capabilities()
        if not caps['configured']:
            raise ReaderUnavailable('Media reading is not set up on this server: missing ' + ' and '.join(caps['missing']) + '.')
        needs = {('link', 'instagram'): ('downloader_found', 'yt-dlp'), ('link', 'tiktok'): ('downloader_found', 'yt-dlp'),
                 ('file', 'image'): ('image_reader_found', 'the local slide reader')}
        flag, label = needs.get((media.kind, media.platform if media.kind == 'link' else media.family), (None, None))
        if flag and not caps[flag]:
            raise ReaderUnavailable(f'This kind of source needs {label}, which was not found on this server.')

    # ------------------------------------------------------------- reads
    def read(self, media, workdir):
        """One read of one approved item. Returns ReadOutput or raises ReaderUnavailable / ReadFailure."""
        self.require(media)
        workdir = Path(workdir)
        paths = default_paths(self.env)
        reading_prompt = workdir / 'reading-prompt.md'
        reading_prompt.write_text(READING_PROMPT, encoding='utf-8')
        if media.kind == 'file' and media.family == 'image':
            return self._slides(paths, workdir, {'mode': 'image', 'path': media.path}, reading_prompt)
        if media.kind == 'link' and media.platform == 'instagram':
            slides = self._slides(paths, workdir, {'mode': 'url', 'url': media.url}, reading_prompt, allow_video=True)
            if slides is not None:
                return slides
        source = media.path if media.kind == 'file' else media.url
        if media.kind == 'link' and media.platform in ('instagram', 'tiktok'):
            source = self._download(paths, workdir, media.url)
        return self._video(paths, workdir, source, reading_prompt)

    def _video(self, paths, workdir, source, prompt_file):
        result_file = workdir / 'reading.txt'
        outcome = self._run([paths['python'], paths['reader'], source, '--prompt-file', str(prompt_file), '--out', str(result_file)],
                            'video reader', workdir, READER_SECONDS, provider_key=True)
        if outcome['code'] != 0:
            raise ReadFailure('The video reader could not finish. Check its Gemini configuration and local dependencies.')
        try:
            return ReadOutput(result_file.read_text(encoding='utf-8'), 'video')
        except OSError as exc:
            raise ReadFailure('The video reader finished without writing a result.') from exc

    def _download(self, paths, workdir, url):
        media_dir = workdir / 'download'
        media_dir.mkdir()
        # No provider key, no cookies, no user or global downloader config, one file, bounded size.
        outcome = self._run([self.which(paths['downloader']) or paths['downloader'], '--ignore-config', '--no-cache-dir', '--no-playlist',
                             '--no-warnings', '--quiet', '--no-progress', '--max-filesize', '80M', '--socket-timeout', '20',
                             '--retries', '1', '--max-downloads', '1', '-f', 'mp4/best', '-o', str(media_dir / 'media.%(ext)s'), url],
                            'video download', media_dir, DOWNLOAD_SECONDS, provider_key=False, max_file_bytes=MAX_DOWNLOAD_BYTES)
        if outcome['code'] != 0:
            raise ReadFailure('The public video was unavailable to the downloader. No login or cookies were used.')
        files = [p for p in media_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= MAX_DOWNLOAD_BYTES:
            raise ReadFailure('Could not identify exactly one video of an allowed size in that post.')
        return str(files[0])

    def _slides(self, paths, workdir, request, prompt_file, allow_video=False):
        slides_prompt = workdir / 'slides-prompt.md'
        slides_prompt.write_text(READING_PROMPT + SLIDES_INSTRUCTION, encoding='utf-8')
        request = {**request, 'out_dir': str(workdir / 'slides'), 'slide_reader': paths['slide_reader'],
                   'read_b_dir': str(Path(paths['reader']).parent), 'prompt_file': str(slides_prompt)}
        outcome = self._run([paths['python'], str(HERE / 'slides_bridge.py')], 'image reader', workdir, READER_SECONDS,
                            provider_key=True, input_text=json.dumps(request))
        if outcome['code'] != 0:
            raise ReadFailure('The image reader could not finish. Check its local dependencies and Gemini configuration.')
        try:
            answer = json.loads(outcome['stdout'])
        except ValueError as exc:
            raise ReadFailure('The image reader returned an invalid result.') from exc
        kind = answer.get('kind') if isinstance(answer, dict) else None
        if kind == 'video' and allow_video:
            return None
        if kind == 'error':
            raise ReadFailure(str(answer.get('message') or 'Image reading failed.')[:200])
        if kind != 'slides':
            raise ReadFailure('The image reader returned an invalid result.')
        counts = [answer.get('expectedCount'), answer.get('observedCount'), answer.get('modelReportedSlides')]
        if (answer.get('status') != 'complete' or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in counts)
                or len(set(counts)) != 1):
            raise ReadFailure('The carousel read was incomplete (expected, fetched and read slide counts differ), so nothing was imported.')
        return ReadOutput(str(answer.get('body') or ''), 'slides')

    def _run(self, args, label, cwd, seconds, *, provider_key, input_text='', max_file_bytes=None):
        try:
            outcome = self.run(args, env=child_environment(self.env, provider_key), cwd=str(cwd), input_text=input_text,
                               timeout=seconds, max_file_bytes=max_file_bytes)
        except FileNotFoundError as exc:
            raise ReaderUnavailable('A local media tool (Python or the downloader) could not be started.') from exc
        if outcome.get('terminated'):
            raise ReadFailure(f"The {label} {TERMINATIONS.get(outcome['terminated'], 'was stopped')}. Nothing was imported.")
        return outcome
