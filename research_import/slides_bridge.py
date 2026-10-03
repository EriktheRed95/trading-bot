#!/usr/bin/env python
"""Adapter from this app to the installed slide reader, run as a child process.

The request arrives as one JSON object on stdin, never in argv:
  {"mode": "url", "url": <public Instagram post>, "out_dir", "slide_reader", "read_b_dir", "prompt_file"}
  {"mode": "image", "path": <one local image>, "out_dir", "slide_reader", "read_b_dir", "prompt_file"}

The installed reader does the validated work: it lists a post's images from public
metadata, fetches them only from the Instagram CDN with redirects re-checked, decodes each
image, sends them to Gemini in one request and deletes them. This file only wires its
public functions together and shapes the answer. It never opens a paper book, reads the
provider key itself or writes outside `out_dir`. The parent only starts it after the user
approved this one item.

Output: one JSON line, {"kind": "slides"|"video"|"error", ...}.
"""
import json
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research_import.process import PROVIDER_KEY_NAMES, run_bounded   # noqa: E402

POST_URL = re.compile(r'^https://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$')
DISCOVERY_SECONDS = 90
DISCOVERY_BYTES = 8 * 1024 * 1024


def emit(data):
    print(json.dumps(data, ensure_ascii=False, separators=(',', ':')))


def discover(slide_reader, url, env):
    tool = shutil.which('yt-dlp')
    if not tool:
        raise RuntimeError('yt-dlp is not installed or not on PATH')
    # Provider key removed: the downloader never sees it. No cookies, no user or global config.
    result = run_bounded([tool, '-J', '--ignore-config', '--ignore-no-formats-error', '--no-warnings', '--no-progress',
                          '--no-cache-dir', url], env={k: v for k, v in env.items() if k not in PROVIDER_KEY_NAMES},
                         timeout=DISCOVERY_SECONDS, max_output=DISCOVERY_BYTES)
    if result['terminated'] or result['code'] != 0 or not result['stdout'].strip():
        raise RuntimeError('public post metadata was unavailable without login')
    try:
        info = json.loads(result['stdout'])
    except ValueError as exc:
        raise RuntimeError('the downloader returned invalid metadata') from exc
    return slide_reader.parse_ytdlp_info(info)


def shaped(record_or_read, expected, observed, status):
    return {'kind': 'slides', 'status': status, 'expectedCount': expected, 'observedCount': observed,
            'modelReportedSlides': record_or_read.get('model_reported_slides'), 'body': record_or_read.get('body', '')}


def main(request, env):
    out_dir = Path(request['out_dir']).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(request['slide_reader']).resolve(strict=True).parent))
    import slide_reader
    slide_reader.READ_B_DIR = Path(request['read_b_dir']).resolve(strict=True)
    prompt = Path(request['prompt_file']).resolve(strict=True).read_text(encoding='utf-8')

    if request['mode'] == 'image':
        slide = slide_reader.import_local_image(request['path'], out_dir, 'upload', 1, 'local:upload')
        read = slide_reader.gemini_read([slide], prompt)
        count = slide_reader._int_or_none(read.header.get('SLIDES_READ'))
        return shaped({'model_reported_slides': count, 'body': read.body}, 1, 1, 'complete')

    url = request.get('url', '')
    if not POST_URL.match(url):
        return {'kind': 'error', 'message': 'Only public Instagram post links are supported.'}
    kind, slides, evidence = discover(slide_reader, url, env)
    if len(slides) == 1 and slides[0].get('is_video'):
        return {'kind': 'video'}
    record = slide_reader.process_post(url, out_dir, discover=lambda _url: (kind, slides, evidence), prompt=prompt,
                                       tmp_root=out_dir)
    read = record.get('read') or {}
    return shaped({'model_reported_slides': read.get('model_reported_slides'), 'body': str(read.get('body') or '')},
                  record.get('expected_count'), record.get('observed_count'), record.get('status'))


if __name__ == '__main__':
    import os
    try:
        emit(main(json.loads(sys.stdin.read() or '{}'), dict(os.environ)))
    except Exception as exc:   # keep tool output and local paths out of anything the browser can see
        emit({'kind': 'error', 'message': f'Slide reading failed ({type(exc).__name__}).'})
