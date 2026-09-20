"""Syntax-check the dashboard's inline script and its new research section.

Extracts the script block from trading_ui.html, hands it to node --check, and
verifies the element ids the research section renders into all exist in the
markup. Local files only; no server is started and no page is opened.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / 'trading_ui.html'


def main():
    text = HTML.read_text(encoding='utf-8')
    match = re.search(r'<script>(.*)</script>', text, re.S)
    if not match:
        raise SystemExit('No inline script found in trading_ui.html')
    script = match.group(1)
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'ui.js'
        # The page's token placeholder is server-substituted; keep it valid JS.
        path.write_text(script.replace('__TOKEN__', 'token-placeholder'), encoding='utf-8')
        result = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit('Inline dashboard script failed the syntax check')
    print('Inline dashboard script: syntax OK')

    ids = sorted(set(re.findall(r"el\('([a-z0-9-]+)'\)", script)))
    missing = [name for name in ids if f'id="{name}"' not in text]
    print(f'Element ids referenced: {len(ids)}; missing from the markup: {missing or "none"}')
    tabs = re.findall(r"data-tab=\"([a-z]+)\"", text)
    listed = re.search(r"\['portfolio'.*?\]\.forEach\(id=>el\(id\)", script)
    print(f'Tabs in markup: {tabs}')
    print(f'Tabs wired in the switcher: {listed.group(0) if listed else "not found"}')
    for tab in tabs:
        if f"'{tab}'" not in (listed.group(0) if listed else ''):
            raise SystemExit(f'Tab {tab} is not wired into the tab switcher')
    if f'id="helpers"' not in text:
        raise SystemExit('The research helpers section is missing')
    if missing:
        raise SystemExit('The script references element ids that do not exist')
    print('Research helpers section present and wired.')


if __name__ == '__main__':
    sys.exit(main())
