# Research imports (video, carousel and image sources)

A dashboard tab, **Research imports**, turns a public video or carousel link, or a video or image file from this computer, into source-attributed trading research you can read, search and review. It is modelled on the Recipe Book media import and reuses the same installed reader scripts, with its own prompt, schema and private store.

**An import is unverified source material.** It never changes a paper strategy, never places or queues an order, never adds a brokerage, and never becomes a validated finding. Nothing from a video can reach the dashboard's VALIDATED tier; at most it is watchlist material you may later turn into an experiment design of your own.

## What it extracts

Per source, as the source itself stated it (anything not stated is left empty and listed as missing):

| Field | Meaning |
|---|---|
| Described strategy | name, type (trend following, momentum, mean reversion, breakout, swing, scalping, options, arbitrage, long-term, other, unclear) and a neutral description |
| Instruments, timeframes | only those the source names |
| Entry, exit and risk rules | each with where in the source it was stated (`mm:ss` or `slide N`) |
| Stated numbers | label, value as stated, unit, context. Never converted or calculated |
| Claimed returns | every profit, return or win-rate figure. Always stamped **Source claim, unverified**, with how it was presented (backtest, live account, screenshot, spoken, unspecified) |
| Evidence shown | none, spoken claim, screenshot of results, chart examples, backtest shown, live trades shown |
| Missing details | what a tester would need that the source did not state (exit rule, sizing, test period, costs, instruments) |
| Commercial disclosures | affiliate links, courses, signal services or sponsorships visible in the source |

The **evidence tier** is computed by this app from what was extracted, not taken from the reader: E0 not testable as stated; E1 rules described, no results claimed; E2 results claimed, nothing reproducible shown; E3 results claimed with a period and some material shown. Every tier is labelled *not validated*, and E3 still only means "worth checking": the material may be cherry-picked or fabricated.

## Supported sources and limits

| Source | Accepted | How it is read |
|---|---|---|
| YouTube | `youtube.com/watch?v=`, `/shorts/`, `/live/`, `/embed/`, `youtu.be/…`, `m.` and `music.` hosts. Playlists and channels are refused | The link is given to the existing reader, which asks Gemini to fetch the public video |
| Instagram | public posts, reels and carousels (`/p/`, `/reel/`, `/reels/`, `/tv/`). Stories and profiles are refused | Carousels and photo posts: the installed slide reader (validated CDN fetch, all slides in one request, complete only if expected, fetched and reported slide counts are equal). A single video is downloaded with yt-dlp and read as a video |
| TikTok | a single video (`/@user/video/ID`, `vm.`/`vt.tiktok.com` short links). `/photo/` carousels are refused | yt-dlp download (no cookies), then the video reader |
| Direct media link | `https://` to a public host, path ending `.mp4 .m4v .mov .webm .mkv .jpg .jpeg .png .webp` | Downloaded only after approval through the SSRF-guarded fetcher, then read like an upload |
| Uploaded file | video `.mp4 .m4v .mov .webm .mkv` up to **80 MB**; image `.jpg .jpeg .png .webp` up to **12 MB** | Video: the video reader uploads the local file. Image: the slide reader's local-image path (decoded and size-checked) |

Not supported: X/Twitter, Facebook, Reddit, private or login-gated posts, playlists, live streams, Stories, TikTok photo posts, multi-image uploads, audio-only files, archives. Other limits: one item is read at a time; at most 10 staged uploads (400 MB) wait for approval; a staged file expires after 6 hours; 1,000 items in the library; a link is at most 2,048 characters; model output is capped (120 KB stored per item).

**Deduplication.** One source is one item. YouTube, Instagram and TikTok links reduce to their ids, so `youtu.be/ID?t=9`, `/watch?v=ID&si=…` and `/shorts/ID` are one item. Uploads are keyed by the SHA-256 of their content, so a renamed copy is one item. Direct links are keyed by the exact canonical URL including its query, so two signed URLs for the same file are two items. A TikTok short link and its long form are two items, because resolving a short link would need a network request before approval. A duplicate returns the existing item untouched and never re-reads it.

## Consent: per item, explicit, preserved

1. **Staging sends nothing.** Pasting a link or choosing a file only validates and records it as *waiting for your approval*. No Gemini call, no download, no provider request happens.
2. **The notice names the item.** The server writes the notice for that item: the exact link, or the file name, size and content fingerprint; the provider (Google Gemini); that the computer's own configured key is used and may consume allowance; and that the approval covers this one item only.
3. **An unticked box, then a button.** The page shows the server's notice, an approval box that starts unticked, and an *Approve and read this item* button that stays disabled until it is ticked (and unavailable while the reader is not set up).
4. **The request proves which item.** The approval carries the item id, the fingerprint the server issued for that item and notice version, and `acknowledged: true`. The server recomputes the fingerprint from the stored item. A different item, a replaced file, an older notice, `"true"`, `1`, a list of ids or extra fields are all refused, and no consent is recorded.
5. **One approval, one read.** Consent is recorded and the item marked *reading* in one database transaction; the read then runs once. A failed read, a restart or a retry needs a fresh approval (a new consent row). A finished item cannot be re-read by replaying an old request.
6. **Nothing is recorded if the read cannot start.** If the reader is not set up, or another item is being read, the approval is refused with a reason and no consent row is written.
7. **The record stays.** Each approval is stored with time, provider, notice version and outcome, shown on the item, and kept (without content) even if the item is deleted.

## What is stored, and where

`runtime/research-imports/` (next to the paper databases, under the already git-ignored `runtime/`; `/research-imports/` is ignored too in case the store is relocated):

- `research-imports.sqlite3`: items (source, status, validated reading as JSON, your review state and note, search text) and the consent audit. The `validated` column is constrained to 0 by the schema itself.
- `staging/`: uploaded files awaiting approval. Removed after the read (success or failure), on discard or delete, after 6 hours, at startup if orphaned, and when the server restarts mid-read.
- `work/`: a per-read folder for downloads and the prompt, removed after every read.

Uploaded and downloaded media is never kept. Only the extracted text, provenance and your review remain. Delete an item in the page to remove its reading.

**Review states** are yours only: new, reviewed, shortlisted for experiment design, dismissed, plus a note. They do not change anything else. There is deliberately no state called validated.

## Safety design

| Risk | Control |
|---|---|
| Hostile links | `https` only; no credentials (`@`), backslashes, spaces, control or non-ASCII characters; port 443 only; exact host allowlists for the three platforms; IP literals in any notation, `localhost`, `.local`, `.internal` and similar refused |
| SSRF on direct links | Checked again at fetch time and on every redirect (at most 3): public DNS name, every resolved address must be globally routable (IPv4-mapped, 6to4, Teredo and NAT64 forms judged), connection pinned to the validated address so DNS rebinding cannot swap it, content-type allowlist, size cap, total time cap, file signature check, partial file deleted on any failure |
| Platform links | The link is allowlisted and canonicalised before any tool sees it. yt-dlp runs with no cookies, no config, no playlist, one file, 80 MB, and follows its platform's own CDN redirects; YouTube links are fetched by Gemini, not by this server |
| Hostile files | Extension, declared type and leading bytes must agree; size limit from the declared length is enforced before the body is read; exact length required; stored name is a random id, never the upload's name; one upload at a time; 10 minute upload deadline; private folder |
| Model output | Untrusted. Parsed as JSON, constants like NaN refused, only expected primitive types kept, all strings and lists capped, unknown keys (`validated`, `recommendation`, `place_order`…) dropped, return figures stamped as source claims, tier computed here |
| Text in the page | Everything from a link, file name or model is written with `textContent`; the page has no `innerHTML` and a test fails the build if one appears |
| Other websites and other local apps | Loopback peer and Host only; reads refuse cross-site fetches; writes need this app's own `Origin` **and** the per-process token; a refused body is consumed so the client sees the reason |
| Secrets | The key is read from the server's environment by the reader process only. The downloader gets no key. Status answers give booleans, not values or paths. The page contains only the per-process token that already existed |
| Processes | Fixed argument lists, no shell, allowlisted environment, time, output and download-size limits, whole process tree killed on any limit, termination always treated as failure |
| The trading engine | `research_import/` imports no trading, market-data or brokerage module, and no engine module imports it. The server only forwards requests to it. A test hashes a paper database before and after a complete import and review |

## Setup (manual integration steps)

The feature is built and tested with a fake reader. Real reads need the local tools the Recipe Book already uses, none of which are bundled or copied:

1. **Key.** Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the environment of the server process. Never put it in a file in this repository. The page never receives it.
2. **Reader scripts.** By default the server looks under `%USERPROFILE%\.agents\skills`: `video-to-skill\scripts\read_b.py` and `health-reference\scripts\slide_reader.py`. Override with `TRADING_RESEARCH_READER`, `TRADING_RESEARCH_SLIDE_READER`, `TRADING_RESEARCH_PYTHON` and `TRADING_RESEARCH_DOWNLOADER`.
3. **Dependencies those scripts need:** `google-genai`, Pillow (image decoding), and `yt-dlp` on `PATH` for Instagram and TikTok. YouTube, uploaded videos and direct media links need only the video reader.
4. **Restart the dashboard server** so it picks up the environment. Research import starts disabled from reading (staging and review always work) until the status line on the tab says *Reader ready*.

Start without the feature with `--no-research-import`. The server port can be set with `TRADING_APP_PORT` (default 8791, the live dashboard). For a trial run that cannot touch live data:

```
set TRADING_APP_PORT=18791
python trading_app.py --no-browser --no-scheduler --state %TEMP%\trading-trial\paper.sqlite3
```

The state folder holds its own paper book and research store, so nothing live is opened. Do not run this against port 8791 or the live runtime while testing.

## Tests

```
python -B -m unittest test_research_import -v
node test_research_import_ui.cjs
```

They use a fake reader, a fake resolver and transport, temporary stores and an ephemeral loopback port. They make no Gemini, yt-dlp or network call. They cover link and file safety, SSRF (private and rebinding addresses, redirects), consent (no early send, per-item fingerprint, one read per approval, refusal without a consent row), schema validation and labelling, deduplication including races, persistence and recovery, local request protection, key non-disclosure, bounded processes, temp-file lifecycle, UI text safety and consent gating, and separation from the trading engine.

## Known limits

- No real Gemini, yt-dlp or slide-reader call has been made in this build. The command lines, environment handling and answer checks are tested against fakes; the first real read is the real test of the installed tools' current behaviour, model quota and output quality.
- Model extraction can be wrong or incomplete even when it validates. The schema checks shape, not truth. Read the source before relying on any field.
- Platforms change. Instagram and TikTok extraction depends on yt-dlp working without login; it can fail at any time, and the item then shows an error and asks again.
- A direct media link's host sees this computer's address when it is downloaded after approval.
- Short-link and signed-URL spellings of one video can become separate items.
- The page polls while its tab is open; there is no background schedule, and nothing runs unless you approve an item.
