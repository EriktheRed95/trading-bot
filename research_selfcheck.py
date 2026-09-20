"""Inspect a saved research-desk directory without fetching anything.

Checks the dashboard payload is strictly JSON-encodable, carries no score-like
language, and prints what each helper covered. Offline and read-only: it opens
the ledger and shadow books for reading only and makes no request.

    python research_selfcheck.py runtime/research-evidence/desk
"""
import argparse
import json
from pathlib import Path

BANNED = ('confidence', 'probability', 'forecast', 'predict', 'expected return')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path,
                        default=Path('runtime') / 'research-evidence' / 'desk')
    args = parser.parse_args()
    from research_desk import ResearchDesk
    desk = ResearchDesk(args.root, enable_books=False)
    payload = desk.status()
    blob = json.dumps(payload, allow_nan=False)
    print(f'Strict JSON payload: {len(blob):,} bytes')
    # Scan the READINGS only. The policy block deliberately contains the sentence
    # "No confidence score is produced", which is a disclaimer, not a score.
    digest = payload['digest'] or {}
    readings = {'advisories': payload['advisories'],
                'coverage': digest.get('coverage'), 'deferrals': digest.get('deferrals'),
                'disagreements': digest.get('disagreements'),
                'plain_language': digest.get('plain_language'),
                'shadow': digest.get('shadow')}
    lowered = json.dumps(readings, default=str).lower()
    found = [word for word in BANNED if word in lowered]
    print(f'Score-like language in the readings: {found or "none"}')
    print(f"Stated scoring policy: {digest.get('scoring_policy', 'not recorded')}")
    counts = payload['counts']
    print(f"Sessions {counts['sessions']}, advisories {counts['advisories']}, "
          f"abstentions {counts['abstentions']}, holds {counts['holds']}")
    for role, values in (digest.get('coverage') or {}).items():
        print(f"  {values['title']}: examined {values['examined']}, reported "
              f"{values['reported']}, silent {values['abstained']}")
        for reason in values['abstention_reasons']:
            print(f'      silent because: {reason}')
    shadow = (digest.get('shadow') or {}).get('books') or {}
    for name, values in shadow.items():
        print(f"  {name}: ${values['equity']:,.2f} equity, {values['observations']} "
              f"observation(s), {values['fills']} fill(s)")
    print('Deferrals:', digest.get('deferrals') or 'none')
    if found:
        raise SystemExit('Score-like language found in the advisory payload')


if __name__ == '__main__':
    main()
