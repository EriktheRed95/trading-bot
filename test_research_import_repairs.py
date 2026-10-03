"""Review repair 1 (Codex review 2026-10-02): a failure while an approved read is being set up, run or
settled must never hold the read lock or leave an item 'processing' for good.

Failure injection only: a fake reader, temporary stores, no network, no Gemini, no yt-dlp. The
contract tests at the end run the parent's read-only copies of the installed reader scripts in-process
with the model call replaced by synthetic responses.
"""
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_book import PaperBook
from research_import import files, service as service_module
from research_import.reader import GeminiSkillReader
from research_import.service import ResearchImports
from research_import.store import StoreError
from test_research_import import FakeReader, GOOD, SECRET, approve, make_service, upload

ROOT = Path(__file__).resolve().parent
INSTALLED = ROOT / 'reference' / 'installed-readers'
YT_A, YT_B = 'https://www.youtube.com/watch?v=aaaaaaaaaaa', 'https://www.youtube.com/watch?v=bbbbbbbbbbb'


def in_worker():
    return threading.current_thread().name == 'research-import'


class Failing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reader = FakeReader()
        self.svc = make_service(self.dir, self.reader)

    # helpers ---------------------------------------------------------
    def link(self, url):
        return self.svc.add_link(url)['item']

    def assert_free(self):
        self.assertFalse(self.svc.status()['busy'], 'the read lock must be released')

    def assert_other_item_works(self, url=YT_B):
        """After a failure another item can be approved, read to completion and stored."""
        before = len(self.reader.calls)
        other = self.link(url)
        approve(self.svc, other)
        self.assertTrue(self.svc.wait())
        self.assertEqual(self.svc.item(other['id'])['status'], 'done')
        self.assertEqual(len(self.reader.calls), before + 1)
        self.assert_free()

    def consent_outcomes(self, item):
        return [c['outcome'] for c in self.svc.store.consents(item['id'])]

    def assert_no_leftovers(self):
        self.assertEqual(list(self.svc.work.iterdir()), [], 'work folders must be removed')

    @contextlib.contextmanager
    def worker_only(self, target, name, error, times=1):
        """Make `target.name` raise `error` when called from the read worker, `times` times."""
        original, fired = getattr(target, name), []

        def wrapper(*a, **k):
            if in_worker() and len(fired) < times:
                fired.append(1)
                raise error
            return original(*a, **k)
        with patch.object(target, name, wrapper):
            yield fired

    # 1. directory creation ------------------------------------------
    def test_mkdtemp_failure_releases_the_lock_and_settles_the_item(self):
        item = self.link(YT_A)
        with patch.object(service_module.tempfile, 'mkdtemp', side_effect=OSError('disk full')):
            with self.assertLogs('research_import', 'WARNING'):
                approve(self.svc, item)
                self.assertTrue(self.svc.wait())
        self.assert_free()
        failed = self.svc.item(item['id'])
        self.assertEqual((failed['status'], failed['retryable']), ('error', True))
        self.assertEqual(failed['message'], 'Reading did not complete. No research was imported.')
        self.assertNotIn('disk full', json.dumps(failed))
        self.assertEqual(self.reader.calls, [], 'a setup failure must not reach the reader')
        self.assertEqual(self.consent_outcomes(item), ['error'])
        self.assert_no_leftovers()
        self.assert_other_item_works()

    def test_a_failed_item_needs_a_fresh_approval_and_is_never_retried_by_itself(self):
        item = self.link(YT_A)
        with patch.object(service_module.tempfile, 'mkdtemp', side_effect=OSError('x')):
            with self.assertLogs('research_import', 'WARNING'):
                approve(self.svc, item)
                self.svc.wait()
        self.svc.items()
        self.svc.housekeeping()
        self.assertEqual(self.reader.calls, [])                              # no automatic retry, however often we look
        again = self.svc.item(item['id'])
        self.assertIn('consent', again)                                       # the notice is shown again
        approve(self.svc, again)
        self.svc.wait()
        self.assertEqual((len(self.reader.calls), self.svc.item(item['id'])['status']), (1, 'done'))
        self.assertEqual(self.consent_outcomes(item), ['error', 'done'])      # two approvals, two records

    def test_mkdtemp_failure_for_an_upload_drops_its_file_and_keeps_the_item_honest(self):
        item = upload(self.svc)['item']
        staged = Path(self.svc.store.get(item['id'])['staged_path'])
        with patch.object(service_module.tempfile, 'mkdtemp', side_effect=OSError('x')):
            with self.assertLogs('research_import', 'WARNING'):
                approve(self.svc, item)
                self.svc.wait()
        self.assert_free()
        self.assertEqual(self.svc.item(item['id'])['status'], 'error')
        self.assertFalse(staged.exists(), 'the upload was approved for this read and must not linger')
        self.assertNotIn('consent', self.svc.item(item['id']))                # re-upload needed, like any failed upload
        self.assertEqual(self.reader.calls, [])
        self.assert_other_item_works()

    # 2. first store lookup ------------------------------------------
    def test_first_lookup_failure_in_the_worker_still_settles_and_removes_the_staged_file(self):
        item = upload(self.svc)['item']
        staged = Path(self.svc.store.get(item['id'])['staged_path'])
        with self.worker_only(self.svc.store, 'get', OSError('database is locked')) as fired:
            with self.assertLogs('research_import', 'WARNING'):
                approve(self.svc, item)
                self.svc.wait()
        self.assertEqual(fired, [1])
        self.assert_free()
        self.assertEqual(self.svc.item(item['id'])['status'], 'error')
        self.assertFalse(staged.exists(), 'the path released by the settle call is the one removed')
        self.assertEqual(self.reader.calls, [])
        self.assert_no_leftovers()
        self.assert_other_item_works()

    # 3. settle (store.finish) ---------------------------------------
    def test_a_result_that_cannot_be_saved_becomes_a_plain_error(self):
        item = self.link(YT_A)
        original = self.svc.store.finish

        def fail_done(item_id, status, *a, **k):
            if in_worker() and status == 'done':
                raise OSError('database or disk error')
            return original(item_id, status, *a, **k)
        with patch.object(self.svc.store, 'finish', fail_done), self.assertLogs('research_import', 'WARNING'):
            approve(self.svc, item)
            self.svc.wait()
        self.assert_free()
        got = self.svc.item(item['id'])
        self.assertEqual(got['status'], 'error')
        self.assertIn('could not be saved', got['message'])
        self.assertNotIn('result', got)                                       # nothing half-stored
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(self.consent_outcomes(item), ['error'])
        self.assert_other_item_works()

    def test_when_nothing_can_be_saved_the_lock_is_still_released_and_the_item_heals(self):
        link = self.link(YT_A)
        file = upload(self.svc)['item']
        staged = Path(self.svc.store.get(file['id'])['staged_path'])
        for item in (link, file):
            with self.worker_only(self.svc.store, 'finish', OSError('store down'), times=99), self.assertLogs('research_import', 'WARNING'):
                approve(self.svc, item)
                self.assertTrue(self.svc.wait())
            self.assert_free()                                                # the lock never leaks
        self.assertEqual({self.svc.store.get(i['id'])['status'] for i in (link, file)}, {'processing'})   # unsettled for now
        self.svc.items()                                                      # the next listing settles them
        self.assertEqual(self.svc.item(link['id'])['status'], 'error')
        self.assertIn('Approve it again', self.svc.item(link['id'])['message'])
        self.assertEqual(self.svc.item(file['id'])['status'], 'expired')
        self.assertFalse(staged.exists())
        self.assertEqual(self.consent_outcomes(link), ['interrupted'])
        self.assertEqual(self.svc.status()['counts'].get('processing'), None)
        self.assert_other_item_works()

    def test_unsettled_item_does_not_block_approving_another_before_any_listing(self):
        item = self.link(YT_A)
        with self.worker_only(self.svc.store, 'finish', OSError('store down'), times=99), self.assertLogs('research_import', 'WARNING'):
            approve(self.svc, item)
            self.svc.wait()
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'processing')
        self.assert_other_item_works()                                        # no listing happened in between

    def test_settle_failing_twice_in_a_row_then_recovering_is_the_same_as_one_failure(self):
        item = self.link(YT_A)
        with self.worker_only(self.svc.store, 'finish', OSError('blip'), times=2), self.assertLogs('research_import', 'WARNING'):
            approve(self.svc, item)
            self.svc.wait()
        self.assert_free()
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'processing')
        self.svc.reap_stuck()
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'error')

    # 4. cleanup failures --------------------------------------------
    def test_cleanup_failures_do_not_hold_the_lock_or_change_the_result(self):
        item = upload(self.svc)['item']
        with patch.object(self.svc.staging, 'remove', side_effect=PermissionError('file in use')), self.assertLogs('research_import', 'WARNING') as logged:
            approve(self.svc, item)
            self.svc.wait()
        self.assert_free()
        self.assertEqual(self.svc.item(item['id'])['status'], 'done')
        self.assertIn('could not remove a staged file', ' '.join(logged.output))
        leftover = list(self.svc.staging.root.iterdir())
        self.assertEqual(len(leftover), 1)
        self.assertEqual(self.svc.staging.sweep(self.svc.store.staged_paths(), now=10 ** 12), 1)   # picked up as an orphan later
        self.assert_other_item_works()

    def test_a_reader_that_dies_with_a_base_exception_still_frees_the_lock(self):
        item = self.link(YT_A)
        self.reader.error = SystemExit(3)                                     # not an Exception subclass
        with patch.object(threading, 'excepthook', lambda args: None):
            approve(self.svc, item)
            self.svc.wait()
        self.reader.error = None
        self.assert_free()
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'processing')
        self.svc.reap_stuck()
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'error')
        self.assert_other_item_works()

    # 5. worker cannot start -----------------------------------------
    def thread_start_fails(self):
        original = threading.Thread.start

        def start(thread):
            if thread.name == 'research-import':
                raise RuntimeError("can't start new thread")
            return original(thread)
        return patch.object(threading.Thread, 'start', start)

    def test_worker_start_failure_for_a_link_puts_it_back_and_frees_the_lock(self):
        item = self.link(YT_A)
        with self.thread_start_fails(), self.assertLogs('research_import', 'WARNING'):
            with self.assertRaises(StoreError) as caught:
                approve(self.svc, item)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn('nothing was sent', str(caught.exception))
        self.assert_free()
        got = self.svc.item(item['id'])
        self.assertEqual(got['status'], 'pending_consent')
        self.assertIn('consent', got)                                         # needs a new, explicit approval
        self.assertEqual(self.consent_outcomes(item), ['not_started'])
        self.assertEqual(self.reader.calls, [])
        self.assert_other_item_works()
        approve(self.svc, got)                                                # and the failed item itself can be retried by choice
        self.svc.wait()
        self.assertEqual(self.svc.item(item['id'])['status'], 'done')
        self.assertEqual(self.consent_outcomes(item), ['not_started', 'done'])

    def test_worker_start_failure_for_an_upload_keeps_its_file_for_a_new_approval(self):
        item = upload(self.svc)['item']
        staged = Path(self.svc.store.get(item['id'])['staged_path'])
        with self.thread_start_fails(), self.assertLogs('research_import', 'WARNING'):
            with self.assertRaises(StoreError):
                approve(self.svc, item)
        self.assert_free()
        self.assertTrue(staged.exists(), 'nothing was read, so the staged file is kept (and still expires on schedule)')
        self.assertEqual(self.svc.item(item['id'])['status'], 'pending_consent')
        approve(self.svc, self.svc.item(item['id']))
        self.svc.wait()
        self.assertEqual(self.svc.item(item['id'])['status'], 'done')
        self.assertFalse(staged.exists())

    def test_worker_start_failure_when_restoring_also_fails_still_frees_the_lock(self):
        item = self.link(YT_A)
        with self.thread_start_fails(), patch.object(self.svc.store, 'abort_start', side_effect=OSError('store down')), \
                self.assertLogs('research_import', 'WARNING') as logged:
            with self.assertRaises(StoreError):
                approve(self.svc, item)
        self.assert_free()
        self.assertIn('could not restore an item', ' '.join(logged.output))
        self.assertEqual(self.svc.store.get(item['id'])['status'], 'processing')
        self.svc.items()
        self.assertEqual(self.svc.item(item['id'])['status'], 'error')
        self.assert_other_item_works()

    # 6. the backstop must not touch a live read ----------------------
    def test_listing_during_a_live_read_leaves_it_alone(self):
        self.reader.gate = threading.Event()
        item = self.link(YT_A)
        approve(self.svc, item)
        for _ in range(3):
            listed = self.svc.items()
            self.svc.housekeeping()
        self.assertEqual([i['status'] for i in listed], ['processing'])
        self.assertEqual(self.svc.reap_stuck(), 0)
        with self.assertRaises(StoreError) as caught:                         # the lock is genuinely held
            approve(self.svc, self.link(YT_B))
        self.assertEqual(caught.exception.status, 409)
        self.reader.gate.set()
        self.svc.wait()
        self.assertEqual((self.svc.item(item['id'])['status'], len(self.reader.calls)), ('done', 1))

    # 7. privacy and separation hold through failures ----------------
    def test_failures_keep_consent_privacy_and_the_paper_book_untouched(self):
        paper = self.dir / 'paper.sqlite3'
        book = PaperBook(paper)
        book.cycle({'asof': '2026-09-28', 'fetched_at': '2026-09-28T21:30:00+00:00', 'target_weights': {'SPY': 1.0}, 'prices': {'SPY': 500.0}})
        before = hashlib.sha256(paper.read_bytes()).hexdigest()
        holdings = book.status()['holdings']
        item = upload(self.svc)['item']
        staged = Path(self.svc.store.get(item['id'])['staged_path'])
        with self.worker_only(self.svc.store, 'finish', OSError('down'), times=99), self.assertLogs('research_import', 'WARNING') as logged:
            approve(self.svc, item)
            self.svc.wait()
        self.svc.items()
        self.assertEqual(self.reader.calls[0].path, str(staged))              # read exactly once, only the approved file
        self.assertEqual(len(self.svc.store.consents(item['id'])), 1)         # one approval, no phantom consents
        blob = json.dumps(self.svc.item(item['id'])) + ' '.join(logged.output)
        self.assertNotIn(str(self.dir), blob)
        self.assertNotIn(SECRET, blob)
        self.assertFalse(staged.exists())
        self.assertEqual(hashlib.sha256(paper.read_bytes()).hexdigest(), before)
        self.assertEqual(book.status()['holdings'], holdings)

    def test_restart_after_an_unsettled_read_matches_the_live_backstop(self):
        item = self.link(YT_A)
        with self.worker_only(self.svc.store, 'finish', OSError('down'), times=99), self.assertLogs('research_import', 'WARNING'):
            approve(self.svc, item)
            self.svc.wait()
        reopened = ResearchImports(self.dir / 'research-imports', reader=FakeReader(), fetcher=self.svc.fetcher)
        self.assertEqual(reopened.item(item['id'])['status'], 'error')
        self.assertEqual(self.consent_outcomes(item), ['interrupted'])


# ============================================================ real reader contract (offline, synthetic model)


def load_installed(name):
    sys.path.insert(0, str(INSTALLED))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(INSTALLED))


@unittest.skipUnless((INSTALLED / 'read_b.py').exists() and (INSTALLED / 'slide_reader.py').exists(),
                     'the parent-provided read-only reader copies are not present')
class RealReaderContract(unittest.TestCase):
    """The installed read_b.py and slide_reader.py, with only the model call replaced by a synthetic reply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.read_b = load_installed('read_b')
        self.slides = load_installed('slide_reader')

    def run_read_b(self, args, reply):
        """Run the real read_b.main in-process. Returns (exit_code, stderr). `reply` is the model's text."""
        rb = self.read_b
        genai = type('G', (), {'types': type('T', (), {'Part': staticmethod(lambda **k: k), 'FileData': staticmethod(lambda **k: k)})})
        stderr = io.StringIO()
        code = 0
        with patch.object(rb, 'load_client', lambda: (object(), genai)), patch.object(rb, 'discover_models', lambda client: ['synthetic-model']), \
                patch.object(rb, 'sweep_stale_temps', lambda: None), patch.object(rb, 'load_skip_cache', lambda: {}), \
                patch.object(rb, 'generate_with_fallback', lambda *a, **k: (type('R', (), {'text': reply})(), 'synthetic-model', [])), \
                patch.object(sys, 'argv', ['read_b.py', *args]), contextlib.redirect_stderr(stderr):
            try:
                rb.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, stderr.getvalue()

    def fake_run_for(self, runs):
        def run(args, *, env, cwd, input_text='', timeout, max_file_bytes=None):
            runs.append(args)
            code, err = self.run_read_b(args[2:], self.reply)
            return {'code': code, 'terminated': None, 'stdout': '', 'stderr': err}
        return run

    def service(self, run, state='state'):
        env = {'GEMINI_API_KEY': SECRET, 'TRADING_RESEARCH_READER': str(INSTALLED / 'read_b.py'),
               'TRADING_RESEARCH_SLIDE_READER': str(INSTALLED / 'slide_reader.py')}
        return make_service(self.dir / state, GeminiSkillReader(env, run=run, which=lambda n: None))

    def test_real_read_b_output_file_is_accepted_end_to_end(self):
        runs, self.reply = [], '```json\n' + json.dumps(GOOD) + '\n```'
        svc = self.service(self.fake_run_for(runs))
        item = svc.add_link(YT_A)['item']
        approve(svc, item)
        svc.wait()
        got = svc.item(item['id'])
        self.assertEqual(got['status'], 'done', got['message'])
        self.assertEqual(got['result']['reading']['strategy']['name'], 'ORB')
        self.assertIs(got['result']['reading']['validated'], False)           # a model's own claim is still dropped
        self.assertEqual(runs[0][2], YT_A)
        self.assertIn('--prompt-file', runs[0])

    def test_real_read_b_header_and_chain_walk_comments_do_not_break_parsing(self):
        self.reply = json.dumps(GOOD)
        out = self.dir / 'o.txt'
        prompt = self.dir / 'p.md'
        prompt.write_text('p', encoding='utf-8')
        code, _ = self.run_read_b([YT_A, '--prompt-file', str(prompt), '--out', str(out)], self.reply)
        text = out.read_text(encoding='utf-8')
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith('<!-- read by synthetic-model -->'))
        self.assertEqual(self.read_json(text)['title'], GOOD['title'])
        with_walk = text.replace('<!-- read by synthetic-model -->\n', '<!-- read by m -->\n<!-- chain_walk: a=quota;b=busy -->\n')
        self.assertEqual(self.read_json(with_walk)['title'], GOOD['title'])

    @staticmethod
    def read_json(text):
        from research_import.schema import json_from_text
        return json_from_text(text)

    def test_real_read_b_refusal_is_a_clean_retryable_error_that_leaks_nothing(self):
        """The installed reader reports an empty or refused answer only as 'returned an empty response' on stderr, exit 1."""
        runs, self.reply = [], ''
        svc = self.service(self.fake_run_for(runs))
        item = svc.add_link(YT_A)['item']
        approve(svc, item)
        svc.wait()
        got = svc.item(item['id'])
        self.assertEqual((got['status'], got['retryable']), ('error', True))
        self.assertNotIn('empty response', got['message'])                    # tool output is not shown
        self.assertNotIn(str(self.dir), json.dumps(got))
        self.assertFalse(svc.status()['busy'])
        self.assertIn('consent', got)                                         # it can be approved again, explicitly
        self.assertEqual(len(runs), 1)                                        # and was not retried automatically
        code, err = self.run_read_b([YT_A, '--prompt-file', str(self.dir / 'missing.md')], '')
        self.assertEqual(code, 1)
        self.assertIn('prompt file not found', err)

    def test_real_slide_reader_with_a_synthetic_png_through_the_bridge_and_service(self):
        from PIL import Image
        png = self.dir / 'shot.png'
        Image.new('RGB', (8, 6), (10, 120, 200)).save(png)
        sr = self.slides
        reply = 'SLIDES_READ: 1\n---\n' + json.dumps(GOOD)
        header, body = sr.parse_read_text(reply)
        real_result = sr.ReadResult(text=reply, model='synthetic-model', header=header, body=body)
        bridge = importlib.import_module('research_import.slides_bridge')
        seen = {}

        def synthetic_read(files_, prompt, **k):
            seen['mime'], seen['n'], seen['prompt'] = [f.mime for f in files_], len(files_), prompt
            return real_result

        def run(args, *, env, cwd, input_text='', timeout, max_file_bytes=None):
            with patch.object(sr, 'gemini_read', synthetic_read):
                out = bridge.main(json.loads(input_text), {})
            return {'code': 0, 'terminated': None, 'stdout': json.dumps(out), 'stderr': ''}
        svc = self.service(run)
        data = png.read_bytes()
        item = svc.add_upload(io.BytesIO(data), len(data), 'shot.png', 'image/png')['item']
        approve(svc, item)
        svc.wait()
        got = svc.item(item['id'])
        self.assertEqual(got['status'], 'done', got['message'])
        self.assertEqual((seen['mime'], seen['n']), (['image/png'], 1))
        self.assertIn('SLIDES_READ', seen['prompt'])
        self.assertEqual(got['result']['read_method'], 'slides')
        self.assertFalse(svc.status()['busy'])
        self.assertEqual(list(svc.staging.root.iterdir()), [])

    def test_real_slide_reader_refusal_and_wrong_slide_count_fail_cleanly(self):
        from PIL import Image
        png = self.dir / 'shot.png'
        Image.new('RGB', (8, 6)).save(png)
        sr = self.slides
        bridge = importlib.import_module('research_import.slides_bridge')

        def run_with(read):
            def run(args, *, env, cwd, input_text='', timeout, max_file_bytes=None):
                with patch.object(sr, 'gemini_read', read):
                    try:
                        out = bridge.main(json.loads(input_text), {})
                    except Exception as exc:   # exactly what the bridge's __main__ does
                        out = {'kind': 'error', 'message': f'Slide reading failed ({type(exc).__name__}).'}
                return {'code': 0, 'terminated': None, 'stdout': json.dumps(out), 'stderr': ''}
            return run

        def refused(files_, prompt, **k):
            raise sr.ReadFailed('synthetic-model returned an empty response')

        def miscounted(files_, prompt, **k):
            header, body = sr.parse_read_text('SLIDES_READ: 3\n---\n' + json.dumps(GOOD))
            return sr.ReadResult(text='x', model='m', header=header, body=body)
        data = png.read_bytes()
        for number, read in enumerate((refused, miscounted)):
            svc = self.service(run_with(read), state=f'case{number}')
            item = svc.add_upload(io.BytesIO(data), len(data), 'shot.png', 'image/png')['item']
            approve(svc, item)
            svc.wait()
            got = svc.item(item['id'])
            self.assertEqual(got['status'], 'error', read.__name__)
            self.assertNotIn('result', got)
            self.assertFalse(svc.status()['busy'])
            self.assertEqual(list(svc.staging.root.iterdir()), [], 'the approved image is removed even when the read fails')

    def test_installed_reader_surfaces_this_app_relies_on_still_exist(self):
        rb_text = (INSTALLED / 'read_b.py').read_text(encoding='utf-8')
        for needle in ('--prompt-file', '--out', '<!-- read by', 'GEMINI_API_KEY', 'returned an empty response'):
            self.assertIn(needle, rb_text, needle)
        for name in ('import_local_image', 'gemini_read', 'parse_ytdlp_info', 'process_post', 'parse_read_text', 'ReadResult', 'ReadFailed',
                     '_int_or_none', 'READ_B_DIR'):
            self.assertTrue(hasattr(self.slides, name), name)


if __name__ == '__main__':
    unittest.main()
